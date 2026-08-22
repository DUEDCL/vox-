"""ACP (Agent Client Protocol) agents over stdio (ADR 003, P7).

JSON-RPC 2.0, one object per line, stdin/stdout. This is the standard path:
implement the handshake once and every conforming agent connects without further
adapter code. The shapes this adapter speaks are the pinned ACP surface:

- ``initialize`` (client -> agent): ``{protocolVersion, clientCapabilities}``,
- ``session/new``: ``{cwd, mcpServers}`` -> ``{sessionId}``,
- ``session/prompt``: ``{sessionId, prompt}`` -> ``{stopReason}``,
- ``session/update`` notifications stream the reply as text chunks.

Failure, cancellation and abandonment keep the same three invariants as
``cli.py``: failure is a chunk, exactly one ``done`` per stream, and abandoning
the stream kills the process. Cancellation terminates the session process rather
than sending a graceful ``session/cancel`` -- the turn is over, and the process
is the only thing this side owns.

**Encoding is the protocol's, not the host's.** ACP frames are UTF-8, so this
adapter writes UTF-8 and reads UTF-8, and forces ``PYTHONUTF8``/
``PYTHONIOENCODING`` on the child (see ``_UTF8_ENV``). This is not cosmetic: on
Windows a Python child defaults its stdio codec to the ANSI code page, and
because the read side uses ``errors="replace"`` the resulting mojibake arrives
as U+FFFD inside an otherwise valid reply -- silently wrong rather than failed.
**Remaining gap:** a non-Python agent that writes the local code page is still
mangled the same way, and nothing in the environment can tell it not to.

**Evidence level: SIM.** The tests drive a Python snippet that speaks these
shapes; no real ACP agent has completed a turn through this adapter yet.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from core.tools.policy import scrubbed_env

from .cli import spawn_target, which
from .contract import AgentChunk, AgentDescriptor, Task, render_prompt

#: How long a terminated child gets to exit before it is killed.
_GRACE_S = 2.0

#: The protocol version we request and accept.
_PROTOCOL_VERSION = 1

#: ``session/update`` kinds that carry streaming text.
_TEXT_UPDATE_KINDS = frozenset({"agent_message_chunk", "user_message_chunk"})

#: ``session/update`` kinds that carry an agent-requested tool call.
_TOOL_UPDATE_KINDS = frozenset({"tool_call", "tool_call_update"})

#: Forced on the child because ACP frames are UTF-8 *by protocol*, while a
#: Python child on Windows picks its stdio codec from the ANSI code page
#: (cp936 here) and would emit mojibake this side cannot distinguish from a
#: legitimate reply -- ``errors="replace"`` turns it into U+FFFD, never an
#: error. Only Python children read these; a non-Python agent that writes the
#: local code page is a known remaining gap, recorded in the module docstring.
_UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class AcpAgentError(RuntimeError):
    """The adapter is misconfigured. Runtime failures are chunks, not raises."""


@dataclass
class AcpAgentAdapter:
    """One ACP-compatible process, behind ``AgentAdapter``."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    #: Variable names -- not values -- the child is allowed to inherit.
    env_passthrough: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    cost: int = 3
    latency_ms: int = 2000
    timeout_s: float = 120.0
    max_output_bytes: int = 200_000
    _live: dict[str, subprocess.Popen[str]] = field(default_factory=dict, repr=False)
    _cancelled: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.command:
            raise AcpAgentError(f"agent {self.name!r}: command is required")
        self.args = tuple(self.args)
        self.capabilities = frozenset(self.capabilities)
        self.env_passthrough = tuple(self.env_passthrough)

    # -- contract ---------------------------------------------------------

    def describe(self) -> AgentDescriptor:
        return AgentDescriptor(
            name=self.name,
            kind="acp",
            capabilities=self.capabilities,
            cost=self.cost,
            latency_ms=self.latency_ms,
            timeout_s=self.timeout_s,
        )

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        """Spawn, handshake, prompt, then yield increments until ``done``."""
        started = time.perf_counter()
        with self._lock:
            if task.id in self._cancelled:
                self._cancelled.discard(task.id)
                yield self._done(started, error="cancelled")
                return
        command, problem = spawn_target([self.command, *self.args])
        if problem is not None:
            yield self._done(started, error=problem)
            return
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=self._child_env(),
                shell=False,
            )
        except (OSError, ValueError) as exc:
            yield self._done(started, error=f"cannot start {self.command!r}: {exc}")
            return
        with self._lock:
            self._live[task.id] = process
        try:
            yield from self._session(task, process, started)
        finally:
            self._reap(task.id, process)

    def cancel(self, turn_id: str) -> None:
        """Terminate an in-flight turn. Safe after completion, and idempotent."""
        with self._lock:
            self._cancelled.add(turn_id)
            process = self._live.get(turn_id)
        if process is not None and process.poll() is None:
            _terminate(process)

    def check(self) -> dict[str, Any]:
        """Is the command on PATH? Availability is host state, not a capability."""
        resolved = which(self.command)
        return {
            "name": self.name,
            "kind": "acp",
            "available": resolved is not None,
            **({"path": resolved} if resolved else {"reason": f"{self.command!r} is not on PATH"}),
        }

    # -- session ----------------------------------------------------------

    def _session(
        self, task: Task, process: subprocess.Popen[str], started: float
    ) -> Iterator[AgentChunk]:
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threads = [
            threading.Thread(
                target=_drain, args=(stream, tag, events), daemon=True, name=f"acp-{tag}"
            )
            for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
            if stream is not None
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout_s
        open_streams = len(threads)
        stderr_tail = ""
        emitted = 0
        saw_terminal = False
        reported_tokens: int | None = None

        if not self._write(
            process,
            self._rpc(
                0, "initialize",
                {"protocolVersion": _PROTOCOL_VERSION, "clientCapabilities": {}},
            ),
        ):
            yield self._done(started, error="agent closed stdin before the handshake")
            return

        while open_streams and not saw_terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                yield self._done(started, error=f"timed out after {self.timeout_s:g}s")
                return
            try:
                tag, line = events.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            if tag == "err":
                stderr_tail = (stderr_tail + line)[-2000:]
                continue
            message = self._parse(line)
            if message is None:
                continue
            if "id" in message:
                rid = message["id"]
                if rid == 0:
                    error = self._rpc_error(message)
                    if error is not None:
                        yield self._done(started, error=error)
                        return
                    if not self._write(
                        process,
                        self._rpc(1, "session/new", {"cwd": self.cwd or os.getcwd(), "mcpServers": []}),
                    ):
                        yield self._done(started, error="agent closed stdin during the handshake")
                        return
                elif rid == 1:
                    error = self._rpc_error(message)
                    if error is not None:
                        yield self._done(started, error=error)
                        return
                    session_id = _session_id(message)
                    if session_id is None:
                        yield self._done(started, error="session/new returned no sessionId")
                        return
                    if not self._write(
                        process,
                        self._rpc(
                            2, "session/prompt",
                            {"sessionId": session_id, "prompt": [{"type": "user", "content": render_prompt(task)}]},
                        ),
                    ):
                        yield self._done(started, error="agent closed stdin before the prompt")
                        return
                elif rid == 2:
                    error = self._rpc_error(message)
                    if error is not None:
                        yield self._done(started, error=error)
                        return
                    reported_tokens = _usage_tokens(message)
                    saw_terminal = True
            elif message.get("method") == "session/update":
                for chunk in self._update_chunks(message):
                    if chunk.kind == "text":
                        emitted += len(chunk.text)
                        if emitted > self.max_output_bytes:
                            _terminate(process)
                            yield self._done(
                                started, error=f"output exceeded {self.max_output_bytes} characters"
                            )
                            return
                    yield chunk

        with self._lock:
            cancelled = task.id in self._cancelled
        if cancelled:
            error = "cancelled"
        elif not saw_terminal:
            code = process.wait()
            if code == 0:
                error = "stream ended without a prompt response"
            else:
                detail = stderr_tail.strip().splitlines()
                error = f"exit {code}" + (f": {detail[-1]}" if detail else "")
        else:
            error = None
        yield self._done(started, error=error, tokens=reported_tokens)

    def _update_chunks(self, message: Mapping[str, Any]) -> Iterator[AgentChunk]:
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        update = params.get("update")
        if not isinstance(update, Mapping):
            return
        kind = update.get("sessionUpdate") or update.get("type") or ""
        content = update.get("content")
        if kind in _TEXT_UPDATE_KINDS:
            text = _content_text(content)
            if text:
                yield AgentChunk(kind="text", text=text)
            return
        if kind in _TOOL_UPDATE_KINDS:
            name, arguments = _tool_call(content)
            if name is not None:
                yield AgentChunk(kind="tool_call", tool=name, arguments=arguments)

    def _child_env(self) -> dict[str, str]:
        env = scrubbed_env()
        env.update(_UTF8_ENV)
        for name in self.env_passthrough:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    def _reap(self, turn_id: str, process: subprocess.Popen[str]) -> None:
        """Runs on normal completion *and* on ``GeneratorExit`` from a lost race."""
        if process.poll() is None:
            _terminate(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        with self._lock:
            self._live.pop(turn_id, None)
            self._cancelled.discard(turn_id)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _rpc(rid: int, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params)}

    @staticmethod
    def _write(process: subprocess.Popen[str], message: Mapping[str, Any]) -> bool:
        if process.stdin is None or process.poll() is not None:
            return False
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _parse(line: str) -> Mapping[str, Any] | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _rpc_error(message: Mapping[str, Any]) -> str | None:
        error = message.get("error")
        if isinstance(error, Mapping):
            message_text = error.get("message")
            if isinstance(message_text, str) and message_text:
                return message_text
            return "JSON-RPC error"
        if error:
            return str(error)
        return None

    @staticmethod
    def _done(
        started: float, *, error: str | None = None, tokens: int | None = None
    ) -> AgentChunk:
        return AgentChunk(
            kind="done",
            error=error,
            tokens=tokens,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def _drain(
    stream: Any, tag: str, events: queue.Queue[tuple[str, str | None]]
) -> None:
    """Line-by-line into the queue; ``None`` marks end of stream."""
    try:
        for line in stream:
            events.put((tag, line))
    except (OSError, ValueError):
        pass
    finally:
        events.put((tag, None))


def _terminate(process: subprocess.Popen[str]) -> None:
    """Best-effort termination that never masks the stream's own outcome."""
    try:
        process.terminate()
    except (OSError, ValueError):
        return
    try:
        process.wait(timeout=_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    except (OSError, ValueError):
        return
    try:
        process.kill()
    except (OSError, ValueError):
        return
    try:
        process.wait(timeout=_GRACE_S)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # A stubborn child is still a cleanup failure, not a new agent error.
        pass


def _session_id(message: Mapping[str, Any]) -> str | None:
    result = message.get("result")
    if isinstance(result, Mapping):
        session_id = result.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _usage_tokens(message: Mapping[str, Any]) -> int | None:
    result = message.get("result")
    if not isinstance(result, Mapping):
        return None
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        return None
    for key in ("output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def _tool_call(content: Any) -> tuple[str | None, Mapping[str, Any]]:
    """Best-effort tool-call extraction. The ACP tool-call content shape has
    moved between spec revisions, so a few spellings are tried and anything
    unrecognised falls through to the agent path."""
    if not isinstance(content, Mapping):
        return None, {}
    call = content.get("toolCall") if isinstance(content.get("toolCall"), Mapping) else content
    name = call.get("name")
    arguments = call.get("arguments") if call.get("arguments") is not None else call.get("input")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, Mapping):
        arguments = {}
    return (str(name) if isinstance(name, str) and name else None), arguments


__all__ = ["AcpAgentAdapter", "AcpAgentError"]
