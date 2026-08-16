"""The assembly: plugin + dispatcher + tools + memory + the desktop orb.

Every piece below already existed and was already tested. What was missing was
something that constructs them together, which is why 「说一句 → 直接读文件」was
present in the code and absent from the product. This module is that
constructor, and ``scripts/run_desktop.py`` is its command line.

Three decisions worth stating, because each has a plausible-looking opposite:

- **The runtime is the event sink, and it forwards to the orb.** Not the plugin
  holding a bridge, not the bridge subscribing to the plugin: one object owns the
  fan-out, so `tool.*` from the runner, `task.*` from the dispatcher and
  `state.changed` from the machine reach the orb through the same line, in order.

- **``tool.confirm_required`` is the one event that does not go down that line.**
  It is the only one with an answer, so it travels the request/response path
  (``await_confirmation``) instead. Sending it both ways would show the card
  twice -- and the second card would be the one nobody is listening to.

- **The runtime re-submits with ``confirmed=True``; the dispatcher never does.**
  That property has a test pinning it in P6, and it stays true here: the value
  passed on is what the user clicked, and refusal is what everything else means.

``VoicePlugin`` remains opt-in about memory and tools. This runtime opts in
explicitly, so a caller that wants neither builds a plugin instead of this.
"""

from __future__ import annotations

import queue
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from core.agents.contract import AgentDescriptor, Task
from core.agents.registry import load_agents_config, open_agents
from core.desktop_bridge import DesktopBridge, DesktopBridgeError, find_desktop_binary
from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher, DispatchResult
from core.dispatch.breaker import CircuitBreaker
from core.dispatch.contract import Intent
from core.dispatch.intent import RuleBasedIntentResolver
from core.memory import open_memory
from core.state import VoiceState
from core.tools import open_tools
from evox_plugin.plugin import VoicePlugin

#: Event types the orb answers rather than merely displays.
_ANSWERED = frozenset({"tool.confirm_required"})


@dataclass
class RuntimeReport:
    """What actually got wired, for the caller to print rather than assume."""

    desktop: bool = False
    tools: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    memory: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class VoiceRuntime:
    """One turn in, one answer out, with the orb watching.

    Nothing is built in ``__init__``; ``start()`` is where files are read,
    databases are opened and the orb is spawned. That split is what lets a test
    construct this object and assert on it without a desktop build present.
    """

    #: Verified speaker name from the voiceprint gate. ``None`` is the honest
    #: default and it means ``shell.run`` is refused: this object must never
    #: assert a verification it did not receive.
    speaker: str | None = None
    with_desktop: bool = True
    with_memory: bool = True
    visible: bool = True
    plugin: VoicePlugin = field(default_factory=VoicePlugin)
    bridge: DesktopBridge | None = None
    dispatcher: Dispatcher | None = None
    tool_runner: Any = None
    memory_writer: Any = None
    memory_recaller: Any = None
    adapters: dict[str, Any] = field(default_factory=dict)
    #: Recognised utterances waiting for a turn. The microphone callback only
    #: ever *enqueues*: running a turn there would block the audio device for
    #: the whole dispatch plus TTS playback, and a blocked audio callback drops
    #: frames -- which shows up as a recognizer that mishears, not as a hang.
    utterances: "queue.Queue[str]" = field(default_factory=queue.Queue)
    report: RuntimeReport = field(default_factory=RuntimeReport)
    #: Envelopes seen by the sink, newest last. Bounded so a long session does
    #: not grow without limit; the orb and the memory layer are the durable ones.
    seen: list[dict[str, Any]] = field(default_factory=list)
    max_seen: int = 500
    #: Session id, carried on every ``Task`` so the memory layer can group a run.
    session_id: str = field(default_factory=lambda: f"s-{uuid.uuid4().hex[:12]}")
    turns: int = field(default=0, init=False)
    _pending_confirm: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------- wiring

    def on_event(self, event: Mapping[str, Any]) -> None:
        """The single sink. Five producers, one shape, one line to the orb."""
        envelope = dict(event)
        self.seen.append(envelope)
        if len(self.seen) > self.max_seen:
            del self.seen[: len(self.seen) - self.max_seen]
        if envelope.get("type") in _ANSWERED:
            # Stashed, not forwarded: the confirm path sends it and waits.
            self._pending_confirm = envelope
            return
        if self.bridge is not None:
            self.bridge.send(envelope)

    def start(self) -> RuntimeReport:
        """Build everything and spawn the orb. Idempotent."""
        if self._started:
            return self.report
        self._started = True
        warnings: list[str] = []

        if self.with_memory:
            try:
                _store, self.memory_writer, self.memory_recaller = open_memory(
                    on_event=self.on_event, session_id=self.session_id
                )
            except Exception as exc:  # noqa: BLE001 - memory is an enhancement
                warnings.append(f"memory is off: {type(exc).__name__}: {exc}")
                self.memory_writer = self.memory_recaller = None

        self.tool_runner = open_tools(
            on_event=self.on_event, memory_writer=self.memory_writer
        )

        descriptors, self.adapters, agent_warnings = self._open_agents()
        warnings.extend(agent_warnings)

        self.dispatcher = Dispatcher(
            router=DefaultRouter(
                descriptors,
                breaker=CircuitBreaker(on_event=self.on_event),
                memory_recaller=self.memory_recaller,
                memory_writer=self.memory_writer,
            ),
            aggregator=DefaultAggregator(),
            resolver=RuleBasedIntentResolver(),
            tool_runner=self.tool_runner,
            memory_recaller=self.memory_recaller,
            on_event=self.on_event,
        )

        self.plugin.on_event = self.on_event
        self.plugin.attach_tools(self.tool_runner)
        self.plugin.attach_memory(self.memory_writer, self.memory_recaller)

        if self.with_desktop:
            warnings.extend(self._open_desktop())

        self.report = RuntimeReport(
            desktop=self.bridge is not None and self.bridge.alive,
            tools=tuple(sorted(self.tool_runner.tools)),
            agents=tuple(sorted(self.adapters)),
            memory=self.memory_writer is not None,
            warnings=tuple(warnings),
        )
        self.plugin.start()
        return self.report

    def _open_agents(self) -> tuple[tuple[AgentDescriptor, ...], dict[str, Any], list[str]]:
        """Adapters from config. A bad config is a warning, not a dead start.

        The platform's own tools still answer with zero agents configured, so
        refusing to start over an agent entry would take away more than it
        protects.
        """
        try:
            config = load_agents_config()
            adapters = open_agents(config)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            return (), {}, [f"agents are off: {type(exc).__name__}: {exc}"]
        opened: dict[str, Any] = {}
        descriptors: list[AgentDescriptor] = []
        warnings: list[str] = []
        for adapter in adapters:
            descriptor = adapter.describe()
            descriptors.append(descriptor)
            opened[descriptor.name] = adapter
            status = self._availability(adapter)
            if status is not None:
                warnings.append(status)
        return tuple(descriptors), opened, warnings

    @staticmethod
    def _availability(adapter: Any) -> str | None:
        """``check()`` is where availability is reported; a missing command is
        a warning about that agent, never a reason to drop the entry."""
        try:
            status = adapter.check()
        except Exception as exc:  # noqa: BLE001
            return f"{adapter.describe().name}: check failed: {type(exc).__name__}"
        if isinstance(status, Mapping) and not status.get("available", True):
            reason = status.get("reason") or "unavailable"
            return f"{adapter.describe().name}: {reason}"
        return None

    def _open_desktop(self) -> list[str]:
        """Spawn the orb, or say why not. Headless is a supported way to run."""
        if find_desktop_binary() is None:
            return [
                "the orb is not built (desktop/src-tauri/target/... missing); "
                "running headless -- build it with `npm run tauri build` in desktop/"
            ]
        bridge = DesktopBridge(visible=self.visible)
        try:
            bridge.start()
        except DesktopBridgeError as exc:
            return [f"the orb did not start: {exc}"]
        # Waiting for ``ready`` rather than sleeping: the first line the orb
        # prints is the proof its pipe is open.
        if not bridge.ready.wait(10.0):
            bridge.close()
            return ["the orb started but never reported ready; running headless"]
        self.bridge = bridge
        return []

    # -------------------------------------------------------------------- turns

    def say(self, text: str) -> DispatchResult:
        """One full turn: recognised text in, dispatched answer out, orb updated.

        This is the line that was missing. ``submit_text`` moves the state
        machine and remembers the user's turn; the dispatcher decides tool or
        agent; ``complete_turn`` speaks the answer and returns to listening.
        """
        if not self._started:
            self.start()
        assert self.dispatcher is not None
        self._reach_listening()
        self.plugin.submit_text(text)
        self.turns += 1
        task = Task(id=f"t-{self.turns}", text=text, session_id=self.session_id)
        result = self.dispatcher.dispatch(task, self.adapters, speaker=self.speaker)
        if result.needs_confirmation:
            result = self._confirm_and_retry(task, result)
        self.plugin.complete_turn(self._spoken(result))
        return result

    def attach_microphone(self, capture: Any) -> dict[str, Any]:
        """Point a capture at this runtime so spoken requests drive real turns.

        The capture callback runs on the audio device thread, so it only puts the
        recognised text on ``utterances``. ``pump()`` is what actually runs the
        turn, on the caller thread. Doing the turn inline would hold the audio
        callback for the whole dispatch plus TTS playback -- and a held callback
        drops frames, which is indistinguishable from a bad recognizer.
        """
        report = self.plugin.attach_capture(capture, on_recognized=self.utterances.put)
        return report

    def pump(self, *, timeout: float | None = None) -> DispatchResult | None:
        """Run one queued utterance as a full turn. ``None`` when none arrived.

        Blocking belongs here rather than in the callback, so a long answer
        delays only the next turn and never the microphone.
        """
        try:
            text = self.utterances.get(timeout=timeout) if timeout else self.utterances.get_nowait()
        except queue.Empty:
            return None
        return self.say(text)

    def _confirm_and_retry(self, task: Task, result: DispatchResult) -> DispatchResult:
        """Show the command on the orb, and re-submit only on a real approval.

        The dispatcher does not retry itself and must not: a dispatcher that can
        confirm on the user's behalf makes all four of P4's layers decorative.
        Every non-approval -- no orb, no answer, timeout, denial -- leaves the
        original ``needs_confirmation`` result standing.
        """
        request = self._pending_confirm
        self._pending_confirm = None
        if request is None or self.bridge is None:
            return result
        if not self.bridge.await_confirmation(request):
            return result
        assert self.dispatcher is not None
        payload = request.get("payload") or {}
        command = str(payload.get("command", ""))
        if not command:
            return result
        confirmed = replace(
            task,
            id=f"{task.id}-c",
            mode="single",
            context=(*task.context, "[user confirmed the previous action]"),
        )
        return self.dispatcher.run_intent(
            confirmed,
            # ``confirmed`` is the user's click carried forward, never a default.
            Intent(kind="tool", tool=result.tool, arguments={"command": command, "confirmed": True}),
            speaker=self.speaker,
        )

    def _reach_listening(self) -> None:
        """Get the machine to LISTENING from wherever it is, honestly.

        ``say`` is a whole-turn entry point that does not require a wake event,
        so the machine may still be IDLE. The transition is legal from IDLE,
        SPEAKING, CANCELLED and ERROR alike; from LISTENING it is a no-op, and
        from THINKING it is deliberately *not* legal -- a turn in flight cannot
        be silently replaced, and the caller should not be here.

        The transition is made through the plugin's own ``_state_event`` so the
        ``state.changed`` envelope reaches the same sink every other transition
        uses, rather than moving the machine out from under the plugin.
        """
        target = VoiceState.LISTENING
        if self.plugin.machine.state != target:
            self.plugin._state_event(target, "runtime.say")

    @staticmethod
    def _spoken(result: DispatchResult) -> str:
        """What the orb and the TTS layer get. A refusal is spoken as a refusal."""
        if result.text:
            return result.text
        if result.needs_confirmation:
            return "需要你确认后才能执行。"
        return result.reason or "这一轮没有结果。"

    # ------------------------------------------------------------------ shutdown

    def close(self) -> None:
        """Cancel adapters, then close the orb. Idempotent."""
        for adapter in self.adapters.values():
            cancel = getattr(adapter, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # noqa: BLE001 - shutting down anyway
                    pass
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None
        self._started = False

    def __enter__(self) -> VoiceRuntime:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        """Counts and readiness. No file contents, no command text, no vectors."""
        return {
            "started": self._started,
            "speaker_verified": self.speaker is not None,
            "desktop": self.bridge.describe() if self.bridge is not None else None,
            "tools": sorted(self.tool_runner.tools) if self.tool_runner else [],
            "agents": sorted(self.adapters),
            "memory_attached": self.memory_writer is not None,
            "events_seen": len(self.seen),
            "sink_failures": self.plugin.sink_failures,
            "warnings": list(self.report.warnings),
        }


__all__ = ["RuntimeReport", "VoiceRuntime"]
