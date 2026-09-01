"""Python -> desktop orb, over the child process's own pipes.

The four events the orb needs (state, reply text, tool confirmation, visibility)
were produced by ``core/events.py`` from the beginning and consumed by nothing.
This module is the wire between them.

**A pipe, not a loopback socket.** The orb's confirm card answers
``tool.confirm_required``, and that answer is what lets ``shell.run`` execute. A
local HTTP server would make that answer reachable by every process on the
machine -- including whatever the user is protecting themselves from by requiring
a confirmation at all. A pipe to a child this process spawned is reachable by the
parent and nobody else. There is also no port to bind, no token to mint, and no
loopback check to get wrong.

Wire format, one JSON object per line, both directions:

- **out** (here -> desktop): a validated envelope, exactly as
  ``core/events.py`` built it. ``validate_any_event`` runs at this boundary
  because the stream is mixed -- voice events and platform events share the
  envelope, and this is the confluence point that function was written for.
- **in** (desktop -> here): ``{"kind": "confirm", "id": ..., "approved": bool}``
  or ``{"kind": "ready"}``. ``id`` is the envelope id of the
  ``tool.confirm_required`` that asked, so no new field is needed -- the
  contract's ``additionalProperties: false`` would not have allowed one.

**Events are best-effort; confirmations are fail-closed.** A broken pipe must not
take down a conversation turn, so ``send`` swallows write errors and counts them.
``await_confirmation`` does the opposite: no reply, a dead bridge, a timeout, or
a malformed answer all return ``False``. An approval this side invents is
indistinguishable to ``core/tools/policy.py`` from one the user clicked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.events import EventContractError, validate_any_event

#: How long a pending confirmation waits before it counts as refused. Long
#: enough to read the command on the card, short enough that a turn cannot hang
#: on a card the user walked away from.
DEFAULT_CONFIRM_TIMEOUT_S = 60.0

#: Set to make the child window visible from the start; the orb's own default is
#: hidden so that a launch nobody asked for does not paint on the desktop.
VISIBLE_ENV = "VOX_WAKE_VISIBLE"

#: Where a built desktop binary is looked for, relative to the workspace root.
#: Order is deliberate: a release build wins over a debug one, because a stale
#: debug binary is the likelier thing to be lying around.
_BINARY_CANDIDATES = (
    Path("desktop/src-tauri/target/release/vox.exe"),
    Path("desktop/src-tauri/target/release/vox"),
    Path("desktop/src-tauri/target/debug/vox.exe"),
    Path("desktop/src-tauri/target/debug/vox"),
)


class DesktopBridgeError(RuntimeError):
    """The desktop half cannot be started, or was asked for before it was."""


def find_desktop_binary(root: str | Path | None = None) -> Path | None:
    """The built orb binary, or ``None`` when the desktop half is not built.

    ``None`` rather than an exception: the platform runs headless perfectly well
    and the caller may prefer to say so and carry on. Only asking the bridge to
    ``start()`` without a binary is an error.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    override = os.getenv("VOX_DESKTOP_BINARY")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    for relative in _BINARY_CANDIDATES:
        candidate = base / relative
        if candidate.is_file():
            return candidate
    found = shutil.which("vox")
    return Path(found) if found else None


class DesktopBridge:
    """Own the child process and the two directions of its stdio.

    Constructing this starts nothing. ``start()`` spawns; ``send`` writes;
    ``await_confirmation`` blocks a caller until the orb answers or the timeout
    refuses for it. ``close()`` is idempotent and settles every pending
    confirmation as refused on the way out -- the same rule the frontend keeps
    when its card disappears, for the same reason: a caller left hanging is
    semantically "not refused", which is the wrong direction for a security gate.
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        visible: bool = True,
        confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
        on_incoming: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.command = tuple(command) if command is not None else None
        self.visible = visible
        self.confirm_timeout_s = confirm_timeout_s
        self.on_incoming = on_incoming
        self.process: subprocess.Popen[str] | None = None
        self.sent = 0
        #: Write failures and rejected envelopes, counted rather than raised.
        self.dropped = 0
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._reader: threading.Thread | None = None
        # 每次子进程会话都有独立代数，避免旧 reader 在重启后污染新会话。
        self._generation = 0
        self._closed = False

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Spawn the orb and begin reading its answers.

        ``start`` is idempotent while a child is alive, but a bridge may be
        restarted after ``close`` or after the child exits.  The generation
        token makes an old reader harmless if it finishes after that restart.
        """
        env = dict(os.environ)
        if self.visible:
            env[VISIBLE_ENV] = "1"
        else:
            env.pop(VISIBLE_ENV, None)

        old_process: subprocess.Popen[str] | None = None
        process: subprocess.Popen[str] | None = None
        start_error: BaseException | None = None
        stale_entries: list[tuple[threading.Event, list[bool]]] = []
        with self._lock:
            current = self.process
            if current is not None and current.poll() is None:
                return
            # A dead process may still have a reader blocked in the pipe.
            # Detach and invalidate that session before creating the next one.
            old_process = current
            self.process = None
            self._reader = None
            stale_entries = list(self._pending.values())
            self._pending.clear()
            self.ready.clear()
            self._generation += 1
            generation = self._generation
            self._closed = False
            try:
                command = self.command or self._default_command()
                process = subprocess.Popen(  # noqa: S603 - argv list, no shell
                    list(command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                reader = threading.Thread(
                    target=self._read_loop,
                    args=(process, generation),
                    name="vox-desktop-bridge",
                    daemon=True,
                )
                self.process = process
                self._reader = reader
                reader.start()
            except (DesktopBridgeError, OSError, RuntimeError) as exc:
                self.process = None
                self._reader = None
                self.ready.clear()
                self._generation += 1
                failed_process = process
                process = None
                start_error = exc
            else:
                failed_process = None

        self._release_pending(stale_entries, approved=False)
        # ``poll()`` already reaped an exited child.  Do not close its stdout
        # from this thread: a stale reader may still be blocked because a
        # descendant inherited the pipe, and TextIOWrapper.close() can wait for
        # that reader.  The reader owns and closes the stream in its finally.
        if old_process is not None and old_process.poll() is None:
            self._stop_process(old_process, close_stdout=False)
        if start_error is not None:
            if failed_process is not None:
                self._stop_process(failed_process)
            if isinstance(start_error, DesktopBridgeError):
                raise start_error
            if isinstance(start_error, OSError):
                raise DesktopBridgeError(
                    f"cannot start the desktop orb: {start_error}"
                ) from start_error
            raise DesktopBridgeError("cannot start the desktop orb reader") from start_error

    @staticmethod
    def _stop_process(
        process: subprocess.Popen[str], *, close_stdout: bool = True
    ) -> None:
        """Best-effort child cleanup used by both close and restart recovery.

        A reader thread owns stdout once it has started. Closing that stream
        from another thread can block when a descendant inherited the pipe, so
        active readers leave it for ``_read_loop``'s ``finally``.
        """
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                if close_stdout and process.stdout is not None:
                    process.stdout.close()
            except (OSError, ValueError):
                pass

    def _default_command(self) -> tuple[str, ...]:
        binary = find_desktop_binary()
        if binary is None:
            raise DesktopBridgeError(
                "the desktop orb is not built; run `npm run tauri build` in "
                "desktop/, or pass command= explicitly"
            )
        return (str(binary),)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        """Stop the child, then refuse every confirmation still waiting."""
        with self._lock:
            if self._closed and self.process is None and not self._pending:
                self.ready.clear()
                return
            self._closed = True
            process, self.process = self.process, None
            self._reader = None
            self._generation += 1
            self.ready.clear()
            entries = list(self._pending.values())
            self._pending.clear()
        self._release_pending(entries, approved=False)
        if process is not None:
            self._stop_process(process, close_stdout=False)

    @staticmethod
    def _release_pending(
        entries: list[tuple[threading.Event, list[bool]]], *, approved: bool
    ) -> None:
        for gate, slot in entries:
            if not slot:
                slot.append(approved is True)
            gate.set()

    def __enter__(self) -> DesktopBridge:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ outward

    def send(self, event: Mapping[str, Any]) -> bool:
        """One validated envelope down the pipe. Never raises.

        This is the five-sink shape (``on_event(event)``), so it can be handed
        straight to ``VoicePlugin``, the tool runner, the dispatcher, memory, or
        the breaker without an adapter.

        Validation happens here even though every producer validated already:
        this is the transport boundary, and a malformed envelope reaching the orb
        would be discovered as a UI that quietly stops updating. Counting the
        drop instead of raising keeps a contract slip from ending a turn.
        """
        try:
            validated = validate_any_event(dict(event))
        except EventContractError:
            self.dropped += 1
            return False
        return self._write({"kind": "event", "event": validated})

    def _write(self, message: Mapping[str, Any]) -> bool:
        with self._lock:
            process = self.process
            if process is None or process.stdin is None or process.poll() is not None:
                self.dropped += 1
                return False
            line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                # A closed orb is a normal end, not a failure of the turn.
                self.dropped += 1
                return False
            self.sent += 1
            return True

    def set_visible(self, visible: bool) -> bool:
        """Show or hide the orb. Hiding settles pending confirmations as refused.

        The frontend already refuses on its side when the card goes away; doing
        it here too means the caller is released even if the orb never answers.
        """
        if not visible:
            self._settle_all(False)
        return self._write({"kind": "visible", "visible": bool(visible)})

    def set_tray(self, *, state: str, paused: bool) -> bool:
        """把当前状态与暂停开关同步给托盘菜单。

        为什么不让 Rust 从事件流里自己读：那一侧**刻意不解析事件正文**（类型分派在前端，
        见 `desktop/src-tauri/src/main.rs` 的 `script_for_line`），而托盘要显示的是状态名。
        单独一种 ``kind`` 让 Rust 只认一个很小的、已知的形状，不必去理解平台契约。

        也不走前端：托盘菜单在 Rust 侧建（不扩大 IPC 面），前端根本够不到它。
        """
        return self._write(
            {"kind": "tray", "state": str(state), "paused": bool(paused)}
        )

    # ------------------------------------------------------------------- inward

    def await_confirmation(
        self, event: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> bool:
        """Show ``tool.confirm_required`` on the orb and wait for the answer.

        Returns ``True`` only when the orb reported an explicit approval. Every
        other outcome -- bridge not running, write failed, timeout, malformed
        answer, orb closed -- returns ``False``.

        The caller passes the whole envelope rather than a command string,
        because ``tool.confirm_required`` is the one event carrying the literal
        command (FR-6.13) and the orb must show exactly what will run.
        """
        event_id = str(event.get("id") or "")
        if not event_id:
            return False
        gate = threading.Event()
        slot: list[bool] = []
        with self._lock:
            self._pending[event_id] = (gate, slot)
        try:
            if not self.send(event):
                return False
            gate.wait(self.confirm_timeout_s if timeout_s is None else timeout_s)
            return bool(slot and slot[0] is True)
        finally:
            with self._lock:
                self._pending.pop(event_id, None)

    def _settle(self, event_id: str, approved: bool) -> None:
        with self._lock:
            entry = self._pending.get(event_id)
            if entry is None:
                return
            gate, slot = entry
            if slot:
                return
            slot.append(approved is True)
        gate.set()

    def _settle_all(self, approved: bool) -> None:
        with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
        self._release_pending(entries, approved=approved)

    def _finish_reader(
        self, process: subprocess.Popen[str], generation: int
    ) -> None:
        with self._lock:
            if self.process is not process or self._generation != generation:
                return
            self.process = None
            self._reader = None
            self.ready.clear()
            entries = list(self._pending.values())
            self._pending.clear()
        self._release_pending(entries, approved=False)
        self._stop_process(process, close_stdout=False)

    def _read_loop(
        self, process: subprocess.Popen[str], generation: int
    ) -> None:
        """Read one JSON object per line and fail closed when the pipe ends."""
        stream = process.stdout
        if stream is None:
            self._finish_reader(process, generation)
            return
        try:
            for line in stream:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, Mapping):
                    continue
                try:
                    self._handle(message, process, generation)
                except Exception:
                    # A malformed/hostile child message must not kill cleanup.
                    continue
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            self._finish_reader(process, generation)

    def _handle(
        self,
        message: Mapping[str, Any],
        process: subprocess.Popen[str],
        generation: int,
    ) -> None:
        with self._lock:
            if self.process is not process or self._generation != generation:
                return
            kind = message.get("kind")
            if kind == "ready":
                # Keep the identity check and Event.set() under one lock.  This
                # prevents close() from clearing ready and then losing a race
                # to a stale reader that was already handling a line.
                self.ready.set()
                should_settle = False
            else:
                should_settle = kind == "confirm"
        if should_settle:
            self._settle(str(message.get("id") or ""), message.get("approved") is True)
        if self.on_incoming is not None:
            try:
                self.on_incoming(message)
            except Exception:
                pass

    def describe(self) -> dict[str, Any]:
        """Counts and liveness. No event payloads, no command text."""
        with self._lock:
            pending = len(self._pending)
        return {
            "running": self.alive,
            "ready": self.ready.is_set(),
            "sent": self.sent,
            "dropped": self.dropped,
            "pending_confirmations": pending,
            "confirm_timeout_s": self.confirm_timeout_s,
        }


__all__ = [
    "DEFAULT_CONFIRM_TIMEOUT_S",
    "VISIBLE_ENV",
    "DesktopBridge",
    "DesktopBridgeError",
    "find_desktop_binary",
]
