"""The existing EvoX session bridge, behind ``AgentAdapter``.

``core.session_bridge`` keeps its module and its three tests unchanged: this file
*wraps* ``LocalEvoXTransport``, it does not re-implement it. Every check the
transport makes therefore still runs on every turn -- bearer token required,
plain HTTP only to a loopback address, credentials in the URL refused, turn ids
percent-encoded -- and none of them can be weakened from here without editing
``session_bridge.py`` itself. That is the point of wrapping rather than porting.

**This adapter does not stream, and cannot.** The bridge is a single blocking
POST that returns a finished reply; there is no incremental endpoint to read. So
``stream`` yields one ``text`` chunk and then ``done``, and first-token latency
equals whole-turn latency. That is a property of the endpoint rather than a
shortcut taken here, and it is stated instead of hidden: an interface that looks
incremental while blocking would make the router's latency numbers a fiction.

Cancellation has a matching consequence. The bridge assigns its own turn id and
only reveals it when ``send`` returns, so a cancel arriving mid-request cannot
reach the server at the moment it is made. It is remembered instead, and applied
the instant the turn id exists -- the turn ends as ``cancelled`` and the server
is told, one round trip later than a streaming transport would manage.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

from core.session_bridge import BridgeError, ConversationTransport, LocalEvoXTransport

from .contract import AgentChunk, AgentDescriptor, Task, render_prompt

#: Keys the bridge has been observed to answer with. ``reply`` is what
#: ``evox_plugin.plugin`` already reads, so the two agree by construction.
_REPLY_KEYS = ("reply", "text", "message")


@dataclass
class EvoXAgentAdapter:
    """The local EvoX conversation bridge, behind ``AgentAdapter``."""

    transport: ConversationTransport
    name: str = "evox"
    capabilities: frozenset[str] = frozenset({"chat"})
    cost: int = 1
    latency_ms: int = 1500
    timeout_s: float = 30.0
    #: Cancels the bridge refused, for diagnostics. A failed cancel is never
    #: raised -- ``cancel`` must be safe to call after a turn has finished.
    cancel_failures: int = 0
    #: Platform task id -> the bridge's own turn id.
    _turns: dict[str, str] = field(default_factory=dict, repr=False)
    _cancelled: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.capabilities = frozenset(self.capabilities)

    @classmethod
    def from_env(cls, **overrides: Any) -> "EvoXAgentAdapter":
        """URL and token from the environment; nothing is contacted here."""
        return cls(LocalEvoXTransport.from_env(), **overrides)

    # -- contract ---------------------------------------------------------

    def describe(self) -> AgentDescriptor:
        return AgentDescriptor(
            name=self.name,
            kind="evox",
            capabilities=self.capabilities,
            cost=self.cost,
            latency_ms=self.latency_ms,
            timeout_s=self.timeout_s,
        )

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        """One ``text`` chunk, then ``done``. Failure is a chunk, never a raise."""
        started = time.perf_counter()
        with self._lock:
            if task.id in self._cancelled:
                self._cancelled.discard(task.id)
                yield self._done(started, error="cancelled")
                return
        try:
            result = self.transport.send(
                render_prompt(task), session_id=task.session_id
            )
        except BridgeError as exc:
            yield self._done(started, error=str(exc))
            return
        try:
            remote = _turn_id(result)
            with self._lock:
                if remote is not None:
                    self._turns[task.id] = remote
                cancelled = task.id in self._cancelled
            if cancelled:
                if remote is not None:
                    self._cancel_remote(remote)
                yield self._done(started, error="cancelled")
                return
            reply = _reply_text(result)
            if reply:
                yield AgentChunk(kind="text", text=reply)
            yield self._done(started)
        finally:
            with self._lock:
                self._turns.pop(task.id, None)
                self._cancelled.discard(task.id)

    def cancel(self, turn_id: str) -> None:
        """``turn_id`` is the platform's task id, not the bridge's.

        An unknown id means the turn is already over, and a cancel for a turn the
        server never heard of is a request worth not making.
        """
        with self._lock:
            self._cancelled.add(turn_id)
            remote = self._turns.get(turn_id)
        if remote is not None:
            self._cancel_remote(remote)

    # -- diagnostics ------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Whether the bridge is *configured*, not whether it answers.

        Reachability is deliberately not probed: the only endpoint available
        starts a real conversation turn. The token is reported as present or
        absent and never echoed.
        """
        token = getattr(self.transport, "token", "")
        configured = bool(token)
        return {
            "name": self.name,
            "kind": "evox",
            "available": configured,
            "endpoint": _safe_endpoint(getattr(self.transport, "base_url", "")),
            "cancel_failures": self.cancel_failures,
            **(
                {}
                if configured
                else {"reason": "EVOX_VOICE_BRIDGE_TOKEN is not set"}
            ),
        }

    # -- internals --------------------------------------------------------

    def _cancel_remote(self, remote: str) -> None:
        try:
            self.transport.cancel(remote)
        except BridgeError:
            self.cancel_failures += 1

    def _done(self, started: float, *, error: str | None = None) -> AgentChunk:
        return AgentChunk(
            kind="done",
            error=error,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _turn_id(result: Any) -> str | None:
    if isinstance(result, Mapping):
        value = result.get("turn_id")
        if isinstance(value, str) and value:
            return value
    return None


def _reply_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    for key in _REPLY_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _safe_endpoint(url: Any) -> str:
    """Scheme, host and port only. A URL carrying ``user:pass@`` is refused by the
    transport at send time, but diagnostics must not print it in the meantime."""
    if not isinstance(url, str) or not url:
        return ""
    parsed = urlparse(url)
    if not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"
