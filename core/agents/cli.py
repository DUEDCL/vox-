"""Headless CLI agents as subprocesses.

This is the widest net with the least protocol risk (ADR 003): a command, a
prompt argument, and stdout. It reaches `claude -p`, `codex exec`, `opencode run`
and every agent that has committed to no protocol at all.

Three properties hold for every adapter, and this file is where they are first
implemented:

- **Failure is a chunk, never an exception.** A missing command, a non-zero exit,
  a timeout and a cancellation all arrive as a final ``done`` chunk carrying
  ``error``. The dispatcher must be able to race two agents without wrapping each
  in a ``try``, and one broken agent must not take the turn down with it.
- **The credential in this process's environment is not inherited.** The child
  gets ``scrubbed_env()`` -- the same marker-based scrub the shell tool uses --
  plus whatever variable names the user named in ``env_passthrough``. An agent
  that needs a key must be told which one by name, so the token it receives is a
  decision rather than an accident.
- **Abandoning the stream kills the process.** ``race`` mode (P6) drops the loser
  mid-answer; the generator's ``finally`` reaps the child, so a lost race does not
  leave a subprocess running.

Streaming granularity is **one line**, because that is the finest unit a pipe
can be read at without blocking on a fixed-size buffer. A CLI that prints tokens
without newlines and flushes once at the end therefore streams as a single
chunk -- through no fault of this adapter. That is what ``output = "jsonl"`` is
for: the CLI's own streaming mode is newline-delimited by construction.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from core.tools.policy import scrubbed_env

from .contract import AgentChunk, AgentDescriptor, Task, render_prompt

#: Substituted into ``args``. Absent, the prompt is appended as the last argument
#: -- which is what ``claude -p``, ``codex exec`` and ``opencode run`` all want.
PROMPT_PLACEHOLDER = "{prompt}"

#: Output modes. ``text`` yields each stdout line verbatim, so joining the text
#: chunks reproduces stdout exactly. ``jsonl`` parses each line as JSON and pulls
#: increments out of the shapes listed in ``_from_json``.
OUTPUT_MODES = frozenset({"text", "jsonl"})

#: How long a terminated child gets to exit before it is killed.
_GRACE_S = 2.0

#: Windows shims are batch files -- npm installs ``claude`` as ``claude.cmd`` --
#: and CreateProcess runs those through ``cmd.exe /c``. Python quotes arguments
#: for the C runtime, not for cmd.exe, so an argument carrying a double quote can
#: close the quoting and start a second command: the hazard catalogued as
#: BatBadBut. Refusing batch targets outright is not an option, because on
#: Windows the shim is frequently the only thing on PATH.
_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})

#: Characters that cannot be made safe inside a cmd.exe command line. ``"`` ends
#: the quoting for cmd.exe whatever the C runtime thinks of the backslash before
#: it, and ``%VAR%`` still expands inside quotes. Both are refused rather than
#: escaped -- an escape that works for one of the two parsers and not the other
#: is worse than a flat no.
_CMD_UNSAFE = ('"', "%")


class CliAgentError(RuntimeError):
    """The adapter is misconfigured. Runtime failures are chunks, not raises."""


@dataclass
class CliAgentAdapter:
    """One headless CLI, behind ``AgentAdapter``."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    cost: int = 3
    latency_ms: int = 2000
    timeout_s: float = 120.0
    output: str = "text"
    cwd: str | None = None
    #: Variable names -- not values -- the child is allowed to inherit.
    env_passthrough: tuple[str, ...] = ()
    #: 把 prompt 从 stdin 送进去，而不是当命令行参数。
    #:
    #: Windows 上 npm 装的 CLI 在 PATH 里是一个 ``.cmd`` shim，而 shim 走 cmd.exe ——
    #: **cmd.exe 的命令行不能跨行**，所以一个带换行的 prompt 会在第一个换行处被截断。
    #: 记忆召回一接上就有换行（``render_prompt`` 的 ``Context:`` 那几行），于是
    #: ``claude -p`` 收到的只剩 ``Context:`` 一行，它按一个空请求去回答，而这一回合
    #: 照样报成功 —— **静默错，不是失败**。第一轮对话（还没有记忆）正常，第二轮起坏，
    #: 这是最难查的那种形状。
    #:
    #: ``_CMD_UNSAFE`` 拒的是 ``"`` 和 ``%``，换行是同一类缺口的漏项；把它也加进拒绝表
    #: 的结果是「有记忆之后就不能对话」，那不是修复。stdin 绕开整条命令行，换行在管道
    #: 里没有任何特殊含义。
    prompt_stdin: bool = False
    max_output_bytes: int = 200_000
    #: Lines that were not valid JSON in ``jsonl`` mode. Banners and progress
    #: noise are expected; counted so diagnostics can show the adapter is being
    #: fed a shape it does not understand.
    unparsed: int = 0
    _live: dict[str, subprocess.Popen[str]] = field(default_factory=dict, repr=False)
    _cancelled: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.command:
            raise CliAgentError(f"agent {self.name!r}: command is required")
        if self.output not in OUTPUT_MODES:
            raise CliAgentError(
                f"agent {self.name!r}: output must be one of {sorted(OUTPUT_MODES)}"
            )
        self.args = tuple(self.args)
        self.capabilities = frozenset(self.capabilities)
        self.env_passthrough = tuple(self.env_passthrough)
        if self.prompt_stdin and any(PROMPT_PLACEHOLDER in arg for arg in self.args):
            # 两处都放 prompt 等于放了两遍,或者一处是空的 —— 配置错误报出来,
            # 不要留给运行时去表现成"agent 好像没听懂"。
            raise CliAgentError(
                f"agent {self.name!r}: prompt_stdin 与 {PROMPT_PLACEHOLDER} 占位符互斥"
            )

    # -- contract ---------------------------------------------------------

    def describe(self) -> AgentDescriptor:
        return AgentDescriptor(
            name=self.name,
            kind="cli",
            capabilities=self.capabilities,
            cost=self.cost,
            latency_ms=self.latency_ms,
            timeout_s=self.timeout_s,
        )

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        """Spawn, then yield increments until a ``done`` chunk."""
        started = time.perf_counter()
        with self._lock:
            # Cancelled before the first ``next()``: the turn is over, so do not
            # start a process that nobody is waiting for.
            if task.id in self._cancelled:
                self._cancelled.discard(task.id)
                yield AgentChunk(
                    kind="done", error="cancelled", elapsed_ms=self._ms(started)
                )
                return
        prompt = render_prompt(task)
        command, problem = spawn_target(self.build_argv(prompt))
        if problem is not None:
            yield AgentChunk(kind="done", error=problem, elapsed_ms=self._ms(started))
            return
        try:
            process = subprocess.Popen(  # noqa: S603 - resolved target, never shell=True
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # DEVNULL 仍是默认：一个不需要输入的子进程不该有一根等着它的管道。
                stdin=subprocess.PIPE if self.prompt_stdin else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=self._child_env(),
                shell=False,
            )
        except (OSError, ValueError) as exc:
            yield AgentChunk(
                kind="done",
                error=f"cannot start {self.command!r}: {exc}",
                elapsed_ms=self._ms(started),
            )
            return
        with self._lock:
            self._live[task.id] = process
        if self.prompt_stdin:
            self._feed_prompt(process, prompt)
        try:
            yield from self._pump(task, process, started)
        finally:
            self._reap(task.id, process)

    def cancel(self, turn_id: str) -> None:
        """Terminate an in-flight turn. Safe after completion, and idempotent."""
        with self._lock:
            self._cancelled.add(turn_id)
            process = self._live.get(turn_id)
        if process is not None and process.poll() is None:
            _terminate(process)

    @staticmethod
    def _feed_prompt(process: subprocess.Popen[str], prompt: str) -> None:
        """把 prompt 写进 stdin，然后**立刻关掉**。

        不关的话子进程会一直等更多输入 —— 一个读到 EOF 才开工的 CLI 会挂到超时，
        而超时会被报成「这个 agent 很慢」，不是「我们没关管道」。

        写失败静默吞掉：子进程可能已经退出了，那条失败会以带 ``error`` 的终结 chunk
        到达（这个适配器的失败一律是 chunk 不是异常）；在这里再抛一次只会让同一个故障
        有两种形状。
        """
        stream = process.stdin
        if stream is None:
            return
        try:
            stream.write(prompt)
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    # -- argv and environment ---------------------------------------------

    def build_argv(self, prompt: str) -> list[str]:
        if self.prompt_stdin:
            # prompt 走管道,命令行里就不该再有它的副本。
            return [self.command, *self.args]
        if any(PROMPT_PLACEHOLDER in arg for arg in self.args):
            return [
                self.command,
                *(arg.replace(PROMPT_PLACEHOLDER, prompt) for arg in self.args),
            ]
        return [self.command, *self.args, prompt]

    def _child_env(self) -> dict[str, str]:
        env = scrubbed_env()
        for name in self.env_passthrough:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    def check(self) -> dict[str, Any]:
        """Is the command on PATH? Deliberately outside ``AgentAdapter``.

        Availability is host state, not a capability declaration, so it stays out
        of ``AgentDescriptor`` where red line 2 restricts the field types.
        """
        resolved = which(self.command)
        return {
            "name": self.name,
            "kind": "cli",
            "available": resolved is not None,
            "output": self.output,
            "unparsed_lines": self.unparsed,
            **({"path": resolved} if resolved else {"reason": f"{self.command!r} is not on PATH"}),
        }

    # -- streaming --------------------------------------------------------

    def _pump(
        self, task: Task, process: subprocess.Popen[str], started: float
    ) -> Iterator[AgentChunk]:
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threads = [
            threading.Thread(
                target=_drain, args=(stream, events, tag), daemon=True, name=f"cli-{tag}"
            )
            for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
            if stream is not None
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.timeout_s
        stderr_tail = ""
        emitted = 0
        open_streams = len(threads)
        overflow = False
        # A ``done`` line inside the JSON stream is folded into the single
        # terminal chunk rather than yielded: exactly one ``done`` per stream is
        # a property the dispatcher relies on to know a turn is over.
        reported_error: str | None = None
        reported_tokens: int | None = None
        while open_streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                yield AgentChunk(
                    kind="done",
                    error=f"timed out after {self.timeout_s:g}s",
                    elapsed_ms=self._ms(started),
                )
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
            for chunk in self._parse(line):
                if chunk.kind == "done":
                    reported_error = reported_error or chunk.error
                    reported_tokens = reported_tokens or chunk.tokens
                    continue
                if chunk.kind == "text":
                    emitted += len(chunk.text)
                    if emitted > self.max_output_bytes:
                        overflow = True
                        break
                yield chunk
            if overflow:
                _terminate(process)
                yield AgentChunk(
                    kind="done",
                    error=f"output exceeded {self.max_output_bytes} characters",
                    elapsed_ms=self._ms(started),
                )
                return
        code = process.wait()
        with self._lock:
            cancelled = task.id in self._cancelled
        if cancelled:
            error: str | None = "cancelled"
        elif reported_error:
            # The agent's own message beats "exit 1", and a JSONL agent that
            # reports a failure and still exits 0 must not read as success.
            error = reported_error
        elif code == 0:
            error = None
        else:
            detail = stderr_tail.strip().splitlines()
            error = f"exit {code}" + (f": {detail[-1]}" if detail else "")
        yield AgentChunk(
            kind="done",
            error=error,
            tokens=reported_tokens,
            elapsed_ms=self._ms(started),
        )

    def _parse(self, line: str) -> Iterator[AgentChunk]:
        if self.output == "text":
            yield AgentChunk(kind="text", text=line)
            return
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            self.unparsed += 1
            return
        if isinstance(payload, dict):
            yield from _from_json(payload)

    def _reap(self, turn_id: str, process: subprocess.Popen[str]) -> None:
        """Runs on normal completion *and* on ``GeneratorExit`` from a lost race."""
        if process.poll() is None:
            _terminate(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        with self._lock:
            self._live.pop(turn_id, None)
            self._cancelled.discard(turn_id)

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


def _drain(stream: Any, events: queue.Queue[tuple[str, str | None]], tag: str) -> None:
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


def which(command: str) -> str | None:
    """The absolute target ``command`` names, or ``None``.

    Resolution happens before spawning rather than being left to the OS, because
    on Windows ``shutil.which`` is what applies ``PATHEXT`` -- without it, a
    ``claude`` that exists only as ``claude.cmd`` is reported available by
    ``check()`` and then fails to start.
    """
    return shutil.which(command, path=os.environ.get("PATH"))


def spawn_target(argv: Sequence[str]) -> tuple[list[str] | str, str | None]:
    """What ``Popen`` should be handed, or a refusal reason.

    A normal executable comes back as an argv list with element 0 resolved. A
    batch shim comes back as a **command line string** with every argument
    quoted, which is the only form both cmd.exe and the child's C runtime parse
    the same way: inside quotes cmd.exe stops treating ``&``, ``|``, ``<`` and
    ``>`` as operators, and Python's own list quoting does not put those quotes
    there for an argument that merely contains ``&``.
    """
    resolved = which(argv[0])
    if resolved is None:
        return list(argv), f"{argv[0]!r} is not on PATH"
    rest = list(argv[1:])
    if Path(resolved).suffix.casefold() not in _BATCH_SUFFIXES:
        return [resolved, *rest], None
    for argument in rest:
        found = next((char for char in _CMD_UNSAFE if char in argument), None)
        if found is not None:
            return list(argv), (
                f"{Path(resolved).name} is a batch shim and {found!r} in an "
                "argument cannot be passed through cmd.exe safely"
            )
    return " ".join(_cmd_quote(part) for part in (resolved, *rest)), None


def _cmd_quote(value: str) -> str:
    """Wrap in double quotes, doubling the backslash run that would otherwise
    escape the closing quote. Safe only because ``"`` was refused above."""
    trailing = len(value) - len(value.rstrip("\\"))
    return '"' + value + "\\" * trailing + '"'


#: Keys that have carried an incremental string in the CLIs surveyed. Extraction
#: is shape-driven rather than agent-driven so a new CLI needs config, not code.
_TEXT_KEYS = ("text", "content", "delta", "response", "message")


def _from_json(payload: Mapping[str, Any]) -> Iterator[AgentChunk]:
    kind = str(payload.get("type") or payload.get("event") or "")
    if kind in {"tool_use", "tool_call"}:
        tool = payload.get("name") or payload.get("tool")
        arguments = payload.get("input") or payload.get("arguments") or {}
        yield AgentChunk(
            kind="tool_call",
            tool=str(tool) if tool else None,
            arguments=arguments if isinstance(arguments, Mapping) else {},
        )
        return
    if kind in {"error", "failure"}:
        message = _first_string(payload, ("error", "message", "detail")) or "agent error"
        yield AgentChunk(kind="done", error=message)
        return
    text = _extract_text(payload)
    if text:
        yield AgentChunk(kind="text", text=text)
    if kind in {"result", "done", "message_stop", "final"}:
        yield AgentChunk(kind="done", tokens=_tokens(payload))


def _extract_text(payload: Mapping[str, Any]) -> str:
    for key in _TEXT_KEYS:
        if key not in payload:
            continue
        found = _stringify(payload[key])
        if found:
            return found
    return ""


def _stringify(value: Any) -> str:
    """Text out of a string, a ``{"text": ...}`` object, or a list of either."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return _extract_text(value)
    if isinstance(value, Sequence):
        return "".join(_stringify(item) for item in value)
    return ""


def _first_string(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Mapping):
            nested = _first_string(value, ("message", "detail", "text"))
            if nested:
                return nested
    return ""


def _tokens(payload: Mapping[str, Any]) -> int | None:
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        for key in ("output_tokens", "completion_tokens", "total_tokens", "tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                return value
    value = payload.get("tokens")
    return value if isinstance(value, int) else None
