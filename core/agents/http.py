"""OpenAI-compatible HTTP agents (ADR 003, P7).

``http.py`` talks to any endpoint that speaks the OpenAI Chat Completions shape --
the OpenClaw Gateway on :18789, a remote agent, an aggregator. It is the only
adapter that reaches across a network boundary, so three things hold here that
the subprocess adapters take for granted:

- **Credentials never come from config.** The registry schema has no key for a
  token; a bearer token is read from ``EVOX_AGENT_HTTP_TOKEN`` only when it is
  set. A local gateway on loopback typically needs none, and red line 1 says the
  default install talks to nobody it was not pointed at.
- **The URL is validated like the EvoX bridge checks its own.** Plain HTTP is
  accepted only for loopback addresses; a URL carrying ``user:pass@`` is refused
  outright; the check runs at construction so a bad entry fails at startup rather
  than on the first turn.
- **Failure is a chunk, never an exception**, exactly like ``cli.py``: a refused
  URL, a non-2xx, a timeout, a cancelled turn and an unparseable stream all
  arrive as a terminating ``done`` chunk. The dispatcher can race this adapter
  without wrapping it in a ``try``.

Streaming is SSE (``stream: true``). The reply is read line by line, so a
``text`` chunk is yielded per ``delta`` rather than after the whole answer. A
server that answers without streaming (one JSON object) is still handled: the
whole completion becomes one ``text`` chunk, and first-token latency equals
whole-turn latency -- a property of the endpoint, stated rather than hidden.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .contract import AgentChunk, AgentDescriptor, Task, render_prompt

#: Environment variable carrying the optional bearer token. Not in the config
#: schema -- a credential has no key there, exactly as ADR 003 decided.
TOKEN_ENV = "EVOX_AGENT_HTTP_TOKEN"

#: The OpenAI Chat Completions path, appended to the configured base URL. A URL
#: that already ends in it is used as-is, so ``/v1`` and the full endpoint both
#: work.
CHAT_PATH = "/chat/completions"


class HttpAgentError(RuntimeError):
    """The adapter is misconfigured. Runtime failures are chunks, not raises."""


@dataclass
class HttpAgentAdapter:
    """One OpenAI-compatible endpoint, behind ``AgentAdapter``."""

    name: str
    url: str
    capabilities: frozenset[str] = frozenset()
    cost: int = 3
    latency_ms: int = 2000
    timeout_s: float = 120.0
    #: Model name to send. ``default`` is the honest placeholder for a gateway
    #: that routes on its own; a host that needs a specific model sets it here.
    model: str = "default"
    #: ``None`` means "read ``EVOX_AGENT_HTTP_TOKEN``". An empty string means
    #: none -- the correct value for an unauthenticated local gateway.
    token: str | None = None
    _live: dict[str, Any] = field(default_factory=dict, repr=False)
    _cancelled: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.capabilities = frozenset(self.capabilities)
        self._validate_url()
        if self.token is None:
            self.token = os.getenv(TOKEN_ENV) or ""

    # -- contract ---------------------------------------------------------

    def describe(self) -> AgentDescriptor:
        return AgentDescriptor(
            name=self.name,
            kind="http",
            capabilities=self.capabilities,
            cost=self.cost,
            latency_ms=self.latency_ms,
            timeout_s=self.timeout_s,
        )

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        """POST ``/chat/completions`` and stream the reply as chunks."""
        started = time.perf_counter()
        with self._lock:
            if task.id in self._cancelled:
                self._cancelled.discard(task.id)
                yield self._done(started, error="cancelled")
                return
        try:
            response = urlopen(self._build_request(render_prompt(task)), timeout=self.timeout_s)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            yield self._done(started, error=self._describe_error(exc))
            return
        with self._lock:
            self._live[task.id] = response
        try:
            yield from self._pump(task, response, started)
        finally:
            self._reap(task.id, response)

    def cancel(self, turn_id: str) -> None:
        """Close the in-flight response. Safe after completion, and idempotent."""
        with self._lock:
            self._cancelled.add(turn_id)
            response = self._live.get(turn_id)
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def check(self) -> dict[str, Any]:
        """Whether the endpoint is *configured*, not whether it answers.

        Reachability is deliberately not probed: the only endpoint available
        starts a real turn. The token is reported as present or absent and never
        echoed.
        """
        return {
            "name": self.name,
            "kind": "http",
            "available": True,
            "endpoint": self._safe_endpoint(),
            "model": self.model,
            "token_configured": bool(self.token),
        }

    # -- internals --------------------------------------------------------

    def _validate_url(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HttpAgentError(f"agent {self.name!r}: url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise HttpAgentError(f"agent {self.name!r}: url must not contain credentials")
        if parsed.scheme == "https":
            return
        host = parsed.hostname.lower()
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise HttpAgentError(
            f"agent {self.name!r}: plain HTTP url must use a loopback address"
        )

    def _endpoint(self) -> str:
        base = self.url.rstrip("/")
        if base.endswith(CHAT_PATH):
            return base
        return base + CHAT_PATH

    def _safe_endpoint(self) -> str:
        """Scheme, host and port only. Never a credential or path."""
        parsed = urlparse(self.url)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    def _build_request(self, prompt: str) -> Request:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return Request(self._endpoint(), data=body, method="POST", headers=headers)

    def _pump(
        self, task: Task, response: Any, started: float
    ) -> Iterator[AgentChunk]:
        content_type = response.headers.get("content-type", "") if response.headers else ""
        if "text/event-stream" in content_type:
            yield from self._pump_sse(response, started)
        else:
            yield from self._pump_single(response, started)

    def _pump_sse(self, response: Any, started: float) -> Iterator[AgentChunk]:
        reported_error: str | None = None
        reported_tokens: int | None = None
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                continue
            for chunk in self._from_sse_payload(payload):
                if chunk.kind == "done":
                    reported_error = reported_error or chunk.error
                    reported_tokens = reported_tokens or chunk.tokens
                    continue
                yield chunk
        yield self._done(started, error=reported_error, tokens=reported_tokens)

    def _pump_single(self, response: Any, started: float) -> Iterator[AgentChunk]:
        try:
            data = json.loads(response.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            yield self._done(started, error="unparseable response")
            return
        if not isinstance(data, dict):
            yield self._done(started, error="non-object response")
            return
        error = _extract_error(data)
        if error:
            yield self._done(started, error=error)
            return
        text = _extract_completion(data)
        if text:
            yield AgentChunk(kind="text", text=text)
        yield self._done(started, tokens=_usage_tokens(data))

    def _from_sse_payload(self, payload: str) -> Iterator[AgentChunk]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        error = _extract_error(data)
        if error:
            yield AgentChunk(kind="done", error=error)
            return
        choices = data.get("choices") or []
        if not choices:
            yield from _finish_chunks(data)
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        text = _stringify(delta.get("content"))
        if text:
            yield AgentChunk(kind="text", text=text)
        for tool in delta.get("tool_calls") or ():
            name, arguments = _tool_call(tool)
            if name is not None:
                yield AgentChunk(kind="tool_call", tool=name, arguments=arguments)
        if choice.get("finish_reason"):
            yield AgentChunk(kind="done", tokens=_usage_tokens(data))

    def _reap(self, turn_id: str, response: Any) -> None:
        try:
            response.close()
        except OSError:
            pass
        with self._lock:
            self._live.pop(turn_id, None)
            self._cancelled.discard(turn_id)

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        if isinstance(exc, HTTPError):
            return f"HTTP {exc.code}"
        return f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _done(started: float, *, error: str | None = None, tokens: int | None = None) -> AgentChunk:
        return AgentChunk(
            kind="done",
            error=error,
            tokens=tokens,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _extract_error(data: Mapping[str, Any]) -> str | None:
    error = data.get("error")
    if isinstance(error, str) and error:
        return error
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return None


def _extract_completion(data: Mapping[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return _stringify(message.get("content"))


def _finish_chunks(data: Mapping[str, Any]) -> Iterator[AgentChunk]:
    """A frame carrying usage but no choices -- the terminal accounting frame."""
    tokens = _usage_tokens(data)
    if tokens is not None:
        yield AgentChunk(kind="done", tokens=tokens)


def _usage_tokens(data: Mapping[str, Any]) -> int | None:
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        for key in ("completion_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                return value
    return None


def _tool_call(tool: Any) -> tuple[str | None, Mapping[str, Any]]:
    if not isinstance(tool, Mapping):
        return None, {}
    fn = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
    name = fn.get("name")
    arguments = fn.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, Mapping):
        arguments = {}
    return (str(name) if isinstance(name, str) and name else None), arguments


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Content parts, e.g. [{"type": "text", "text": "..."}].
        return "".join(_stringify(item) for item in value)
    if isinstance(value, Mapping):
        return _stringify(value.get("text"))
    return ""


__all__ = ["TOKEN_ENV", "HttpAgentAdapter", "HttpAgentError"]
