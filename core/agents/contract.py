"""Agent-facing contracts.

This is the seam the platform layer is built on. ``ConversationTransport`` in
``core.session_bridge`` stays untouched for backwards compatibility, but it
cannot express streaming increments, parallel dispatch, or tool callbacks --
``AgentAdapter`` can.

Design red line 2 is enforced here by construction: every type below is built
from ``str``, ``int``, ``float``, ``frozenset``, ``tuple``, and ``Mapping``. No
agent SDK type, subprocess handle, or transport object may appear in these
signatures or in any event payload derived from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

#: How an adapter reaches its agent. ``cli`` spawns a headless subprocess,
#: ``acp`` speaks JSON-RPC 2.0 over stdio, ``http`` calls an OpenAI-compatible
#: endpoint, ``evox`` wraps the existing local session bridge.
AGENT_KINDS = frozenset({"cli", "acp", "http", "evox"})

#: Chunk kinds an adapter may yield. ``tool_call`` is the agent asking the
#: platform to run one of its own tools; ``done`` closes the stream.
CHUNK_KINDS = frozenset({"text", "tool_call", "done"})


@dataclass(frozen=True)
class AgentDescriptor:
    """What an agent claims it can do -- the router's only input about it."""

    name: str
    kind: str
    capabilities: frozenset[str] = frozenset()
    cost: int = 3
    latency_ms: int = 2000
    timeout_s: float = 120.0


@dataclass(frozen=True)
class Task:
    """One unit of dispatchable work.

    ``context`` carries recalled memory as plain text. Audio never reaches this
    type -- design red line 1 holds at the capture boundary, and nothing
    downstream is given a way to violate it.
    """

    id: str
    text: str
    capabilities: frozenset[str] = frozenset()
    session_id: str | None = None
    context: tuple[str, ...] = ()
    mode: str = "single"


@dataclass(frozen=True)
class AgentChunk:
    """One streamed increment from an agent."""

    kind: str
    text: str = ""
    tool: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    elapsed_ms: int | None = None
    tokens: int | None = None
    error: str | None = None


@runtime_checkable
class AgentAdapter(Protocol):
    """The single interface every backend implements."""

    def describe(self) -> AgentDescriptor:
        """Declare capabilities, cost, and expected latency for routing."""

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        """Yield increments until a ``done`` chunk. Must not block on the whole
        reply before yielding its first ``text`` chunk."""

    def cancel(self, turn_id: str) -> None:
        """Abort an in-flight turn. Must be safe to call after completion."""
