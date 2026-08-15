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
VISIBLE_ENV = "EVOX_WAKE_VISIBLE"

#: Where a built desktop binary is looked for, relative to the workspace root.
#: Order is deliberate: a release build wins over a debug one, because a stale
#: debug binary is the likelier thing to be lying around.
_BINARY_CANDIDATES = (
    Path("desktop/src-tauri/target/release/evox_voice_wake.exe"),
    Path("desktop/src-tauri/target/release/evox_voice_wake"),
    Path("desktop/src-tauri/target/debug/evox_voice_wake.exe"),
    Path("desktop/src-tauri/target/debug/evox_voice_wake"),
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
    override = os.getenv("EVOX_DESKTOP_BINARY")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    for relative in _BINARY_CANDIDATES:
        candidate = base / relative
        if candidate.is_file():
            return candidate
    found = shutil.which("evox_voice_wake")
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
        self._closed = False

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Spawn the orb and begin reading its answers.

        ``EVOX_WAKE_VISIBLE`` is passed through the child's environment rather
        than a command-line flag, because that is the switch ``main.rs`` already
        reads -- adding a second way to say the same thing invites the two to
        disagree.
        """
        if self.process is not None:
            return
        command = self.command or self._default_command()
        env = dict(os.environ)
        if self.visible:
            env[VISIBLE_ENV] = "1"
        else:
            env.pop(VISIBLE_ENV, None)
        try:
            self.process = subprocess.Popen(  # noqa: S603 - argv list, no shell
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
        except OSError as exc:
            raise DesktopBridgeError(f"cannot start the desktop orb: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name="evox-desktop-bridge", daemon=True
        )
        self._reader.start()

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
        if self._closed:
            return
        self._closed = True
        process, self.process = self.process, None
        if process is not None:
            for closer in (process.stdin, process.stdout):
                try:
                    if closer is not None:
                        closer.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        self._settle_all(False)

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
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            self.dropped += 1
            return False
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
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
        slot.append(approved is True)
        gate.set()

    def _settle_all(self, approved: bool) -> None:
        with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for gate, slot in entries:
            slot.append(approved is True)
            gate.set()

    def _read_loop(self) -> None:
        """One JSON object per line from the orb. Unparseable lines are ignored.

        The loop ends when the pipe does, and closing settles the pending set --
        an orb that died with a card open must not leave the caller waiting for
        an answer that can no longer arrive.
        """
        process = self.process
        stream = process.stdout if process is not None else None
        if stream is None:
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
                self._handle(message)
        except (OSError, ValueError):
            pass
        finally:
            self._settle_all(False)

    def _handle(self, message: Mapping[str, Any]) -> None:
        kind = message.get("kind")
        if kind == "ready":
            self.ready.set()
        elif kind == "confirm":
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
