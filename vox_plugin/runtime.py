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
import threading
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
from core.events import AGENT_SCHEMA_PATH, build_event, validate_event
from core.memory import open_memory
from core.state import VoiceState
from core.tools import open_tools
from vox_plugin.plugin import VoicePlugin

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
    #: 唤醒确认音库。``None`` = 不应答（``attach_acks`` 没被调用，或者配置里清空了）。
    #: 和 TTS、工具、记忆同款：opt-in，不装就没有这个行为。
    acks: Any = None
    #: 运行日志（``core/console/logbook.Logbook``）。``None`` = 不记。
    #:
    #: 和事件流分工在**扇出面**：事件到球、到传输、到每个消费者，所以不带参数；这份只到
    #: 本机控制台的日志视图，所以带 —— 而「工具收到的 path 是什么」只有带参数才答得出。
    logbook: Any = None
    #: 回合结束到收回唤醒球之间等多久（秒）。0 或负数 = 不自动收。
    hide_after_s: float = 10.0
    #: 收球的定时器。每次回合结束重置，所以连着说话时球不会中途消失。
    _hide_timer: Any = field(default=None, init=False, repr=False)
    plugin: VoicePlugin = field(default_factory=VoicePlugin)
    bridge: DesktopBridge | None = None
    dispatcher: Dispatcher | None = None
    tool_runner: Any = None
    memory_store: Any = None
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
    _closed: bool = field(default=False, init=False, repr=False)
    _active_task_id: str | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------- wiring

    @property
    def effective_speaker(self) -> str | None:
        """Who this turn is authorised as. The gate wins whenever there is one.

        With a microphone attached, ``plugin.verified_speaker`` is the only answer
        -- including when it is ``None``, which is what a rejected or ungated wake
        leaves standing. The constructor's ``speaker`` is *not* consulted there:
        that is exactly the substitution that made ``shell.run``'s credential a
        string literal in the acceptance script.

        Without a microphone the constructor value stands. That path is a caller
        asserting it verified the user some other way -- a typed session on a
        machine its owner is already logged into -- and it is the pre-existing
        behaviour of this class, unchanged.
        """
        if self.plugin.audio_capture is not None:
            return self.plugin.verified_speaker
        return self.speaker

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
            try:
                self.bridge.send(envelope)
            except Exception:
                # The bridge is a display/confirmation enhancement. A broken
                # display must not abort the producer currently emitting.
                self.plugin.sink_failures += 1

    def start(self) -> RuntimeReport:
        """Build everything and spawn the orb, rolling back a partial start.

        ``start`` is a transaction: callers either get a fully running runtime or
        an exception with every resource created so far closed. This matters for
        desktop and microphone use because a failed agent/configuration step must
        not leave a child process, SQLite handle, or adapter alive for the retry.
        """
        if self._started:
            return self.report
        self._closed = False
        warnings: list[str] = []
        try:
            if self.with_memory:
                try:
                    (
                        self.memory_store,
                        self.memory_writer,
                        self.memory_recaller,
                    ) = open_memory(on_event=self.on_event, session_id=self.session_id)
                except Exception as exc:  # noqa: BLE001 - memory is an enhancement
                    warnings.append(f"memory is off: {type(exc).__name__}: {exc}")
                    self.memory_store = self.memory_writer = self.memory_recaller = None

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
                on_detail=self.log,
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
            self._started = True
            return self.report
        except Exception:
            self._cleanup_resources()
            self._started = False
            self._closed = True
            raise

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
        try:
            for adapter in adapters:
                descriptor = adapter.describe()
                descriptors.append(descriptor)
                opened[descriptor.name] = adapter
                status = self._availability(adapter)
                if status is not None:
                    warnings.append(status)
        except Exception:
            # ``open_agents`` may have already created subprocess-backed
            # adapters before a later descriptor/check fails. Do not lose those
            # handles just because the registry is being treated as optional.
            for adapter in adapters:
                self._close_resource(adapter)
            raise
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
            # Waiting for ``ready`` rather than sleeping: the first line the orb
            # prints is the proof its pipe is open.
            if not bridge.ready.wait(10.0):
                self._call_safely(bridge, "close")
                return ["the orb started but never reported ready; running headless"]
        except DesktopBridgeError as exc:
            self._call_safely(bridge, "close")
            return [f"the orb did not start: {exc}"]
        except Exception as exc:  # noqa: BLE001 - desktop is an enhancement
            self._call_safely(bridge, "close")
            return [f"the orb failed during startup: {type(exc).__name__}"]
        self.bridge = bridge
        return []

    # -------------------------------------------------------------------- turns

    def say(self, text: str) -> DispatchResult:
        """Run one turn and recover to ``LISTENING`` on unexpected failures.

        The audio callback only queues text; nevertheless this method is also
        callable by a worker or UI thread, so dispatcher and confirmation errors
        are contained here rather than escaping into the capture loop.
        """
        if not self._started:
            self.start()
        if self.dispatcher is None:
            raise RuntimeError("voice runtime dispatcher is not available")
        self._reach_listening()
        self.plugin.submit_text(text)
        self.turns += 1
        task = Task(id=f"t-{self.turns}", text=text, session_id=self.session_id)
        self.log("turn", f"第 {self.turns} 轮：{text[:120]}", turn=self.turns, text=text)
        self._active_task_id = task.id
        speaker = self.effective_speaker
        try:
            try:
                result = self.dispatcher.dispatch(task, self.adapters, speaker=speaker)
                if result.needs_confirmation:
                    result = self._confirm_and_retry(task, result, speaker=speaker)
            except Exception as exc:  # noqa: BLE001 - the turn must be recoverable
                result = DispatchResult(
                    route="none",
                    reason=f"dispatch failed: {type(exc).__name__}",
                    ok=False,
                )
                self._emit_failure(task, exc)
                self._recover_turn()
                return result
        finally:
            self._active_task_id = None

        try:
            self.plugin.complete_turn(self._spoken(result))
        except Exception as exc:  # noqa: BLE001 - preserve a usable runtime
            self._emit_failure(task, exc)
            self._recover_turn()
            return DispatchResult(
                route=result.route,
                chunks=result.chunks,
                agents=result.agents,
                tool=result.tool,
                elapsed_ms=result.elapsed_ms,
                reason=f"turn completion failed: {type(exc).__name__}",
                ok=False,
                needs_confirmation=result.needs_confirmation,
            )
        # 回合走完了：起倒计时收球。失败路径不走这里 —— 那几条上面已经 return，
        # 而一个报错之后立刻消失的球会让人以为是它崩了。
        self._schedule_hide()
        self.log(
            "turn",
            f"第 {self.turns} 轮完成：route={result.route} ok={result.ok} {result.elapsed_ms}ms",
            level="info" if result.ok else "error",
            turn=self.turns,
            route=result.route,
            ok=result.ok,
            tool=result.tool or "",
            agents=list(result.agents or ()),
            reason=result.reason or "",
            elapsed_ms=result.elapsed_ms,
            answer=(result.text or "")[:200],
        )
        return result

    def log(self, source: str, message: str, **fields: Any) -> None:
        """往运行日志写一条。没装 logbook 就什么都不做。

        吞掉一切异常：这是个调试通道，它失败绝不能改变一轮的结果。
        """
        if self.logbook is None:
            return
        try:
            self.logbook.write(source, message, **fields)
        except Exception:  # noqa: BLE001 - a log sink is never load-bearing
            pass

    def attach_acks(self, library: Any) -> dict[str, Any]:
        """装上唤醒确认音。opt-in：不调用就不会有任何声音从唤醒这一步发出来。

        预生成在这里做（``ensure()``），不留到唤醒那一刻 —— 本机合成一句要 500–900 ms，
        而那一刻正是最不该等的时候。
        """
        self.acks = library
        if library is None:
            return {"acks_attached": False}
        ready = library.ensure()
        return {"acks_attached": True, "cached": len(ready), "failed": dict(library.failed)}

    def attach_microphone(self, capture: Any) -> dict[str, Any]:
        """Point a capture at this runtime so spoken requests drive real turns.

        The capture callback runs on the audio device thread, so it only puts the
        recognised text on ``utterances``. ``pump()`` is what actually runs the
        turn, on the caller thread. Doing the turn inline would hold the audio
        callback for the whole dispatch plus TTS playback -- and a held callback
        drops frames, which is indistinguishable from a bad recognizer.

        ``on_wake`` 走一层包装（``_woken``）：状态机那一步照旧，额外做的两件事是弹出唤醒球
        和应一声 —— 两件都在另一个线程上，理由和上面同一条（音频回调不能被占住）。
        """
        report = self.plugin.attach_capture(
            capture, on_recognized=self.utterances.put, on_wake=self._woken
        )
        return report

    def _woken(self, keyword: str, score: float | None = None) -> Any:
        """唤醒命中：状态机先走，然后弹球 + 应一声。

        返回值仍然是 ``wake_detected`` 的事件列表 —— capture 靠它判断这次唤醒被接受了
        没有（被拒绝的唤醒走的是 ``on_reject``，根本不到这里）。
        """
        events = self.plugin.wake_detected(keyword, score)
        self.log("wake", f"命中「{keyword}」", keyword=keyword, score=score)
        threading.Thread(target=self._greet, daemon=True, name="vox-greet").start()
        return events

    def _greet(self) -> None:
        """把球显示出来，再应一声。两个都吞异常：欢迎动作失败绝不能让唤醒失败。

        **已知缺口**：应答音是从扬声器出来的，而识别器此刻已经开着，所以「嗯哼」可能被采进
        转写的开头。正确的修法是让 capture 在播放期间不喂识别器（一个静音窗口），那要改
        采集层；现在的取舍是宁可多一句噪声，也不要让人以为没听见而重复喊 —— 重复喊的第二遍
        同样会进转写，而且更长。
        """
        self._cancel_hide()
        if self.bridge is not None:
            try:
                self.bridge.set_visible(True)
            except Exception:  # noqa: BLE001 - 球是增强，不是前提
                pass
        if self.acks is not None:
            self.acks.play()

    def _cancel_hide(self) -> None:
        timer = self._hide_timer
        self._hide_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _schedule_hide(self) -> None:
        """回合结束后过一会儿把球收回去。

        不立刻收：连续对话模式下说完会回 LISTENING 等下一句，球在那几秒里是「我还在听」的
        唯一可见信号。也不永远留着：待机时桌面上不该有个常驻的球。每次新回合重置倒计时，
        所以连着说话时它不会中途消失。
        """
        self._cancel_hide()
        if self.bridge is None or self.hide_after_s <= 0:
            return

        def hide() -> None:
            try:
                self.bridge.set_visible(False)
            except Exception:  # noqa: BLE001
                pass

        timer = threading.Timer(self.hide_after_s, hide)
        timer.daemon = True
        self._hide_timer = timer
        timer.start()

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

    def _confirm_and_retry(
        self, task: Task, result: DispatchResult, *, speaker: str | None = None
    ) -> DispatchResult:
        """Show the command on the orb, and re-submit only on a real approval.

        The dispatcher does not retry itself and must not: a dispatcher that can
        confirm on the user's behalf makes all four of P4's layers decorative.
        Every non-approval -- no orb, no answer, timeout, denial -- leaves the
        original ``needs_confirmation`` result standing.

        ``speaker`` is passed in rather than re-read so the retry authorises as the
        same identity the original attempt did. Re-reading could pick up a
        different verdict from a wake that landed while the card was on screen.
        """
        request = self._pending_confirm
        self._pending_confirm = None
        if request is None or self.bridge is None:
            return result
        try:
            approved = self.bridge.await_confirmation(request)
        except Exception:
            return result
        if approved is not True:
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
            speaker=speaker,
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

    def _emit_failure(self, task: Task, exc: Exception) -> None:
        """Record a safe failure event without including user text or secrets."""
        try:
            self.on_event(
                validate_event(
                    build_event(
                        "task.failed",
                        {"task_id": task.id, "error": type(exc).__name__},
                    ),
                    AGENT_SCHEMA_PATH,
                )
            )
        except Exception:
            # Event delivery is telemetry; it must not prevent recovery.
            pass

    def _recover_turn(self) -> None:
        """Leave an interrupted turn in a state where the next one can run."""
        try:
            if self.plugin.machine.state in {VoiceState.THINKING, VoiceState.SPEAKING}:
                self.plugin._state_event(VoiceState.ERROR, "runtime turn failed")
            if self.plugin.machine.state in {VoiceState.ERROR, VoiceState.CANCELLED}:
                self.plugin._state_event(VoiceState.LISTENING, "runtime recovered")
        except Exception:
            # A malformed injected plugin should not make the capture callback
            # fail; the next explicit start/close still has a chance to recover it.
            pass

    @staticmethod
    def _call_safely(obj: Any, method: str, *args: Any) -> None:
        callback = getattr(obj, method, None)
        if callable(callback):
            try:
                callback(*args)
            except Exception:
                pass

    @classmethod
    def _close_resource(cls, obj: Any) -> None:
        """Invoke one supported teardown hook without duplicating side effects."""
        if obj is None:
            return
        for method in ("stop", "close", "shutdown"):
            callback = getattr(obj, method, None)
            if callable(callback):
                cls._call_safely(obj, method)
                return

    def _cleanup_resources(self) -> None:
        """Best-effort cleanup for failed start and explicit close.

        The microphone and TTS objects are injected by the caller, so cleanup
        stops/closes them but keeps the attachments. A later ``start()`` can
        therefore restart the same configured runtime instead of silently losing
        its microphone after the first ``close()``.
        """
        active_id = self._active_task_id
        for adapter in tuple(self.adapters.values()):
            if active_id:
                self._call_safely(adapter, "cancel", active_id)
            self._close_resource(adapter)

        self._close_resource(self.dispatcher)
        self._close_resource(self.tool_runner)
        self._close_resource(self.memory_writer)
        self._close_resource(self.memory_recaller)
        self._close_resource(self.plugin.audio_capture)
        self._close_resource(self.plugin.tts)
        if self.plugin.last_turn_id:
            self._call_safely(
                self.plugin.transport, "cancel", self.plugin.last_turn_id
            )

        if self.bridge is not None:
            self._call_safely(self.bridge, "close")
            self.bridge = None
        if self.memory_store is not None:
            self._call_safely(self.memory_store, "close")

        self.dispatcher = None
        self.tool_runner = None
        self.memory_store = None
        self.memory_writer = None
        self.memory_recaller = None
        self.adapters = {}
        self.plugin.attach_tools(None)
        self.plugin.attach_memory(None, None)
        self.plugin.running = False
        self.plugin.paused = False
        self.plugin.last_turn_id = None
        self.plugin.last_reply = None
        # A closed runtime holds no verified identity. The capture attachment is
        # kept (a later start() reuses the same configured microphone), but the
        # verdict it produced does not survive the shutdown -- the next wake is
        # what re-establishes it.
        self.plugin.verified_speaker = None
        self.plugin.machine.state = VoiceState.IDLE
        self._pending_confirm = None
        self._active_task_id = None
        while True:
            try:
                self.utterances.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        """Stop all owned resources and settle the runtime exactly once."""
        if self._closed:
            return
        self._cleanup_resources()
        self._started = False
        self._closed = True

    def __enter__(self) -> VoiceRuntime:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        """Counts and readiness. No file contents, no command text, no vectors."""
        return {
            "started": self._started,
            "speaker_verified": self.effective_speaker is not None,
            "gate_source": "microphone" if self.plugin.audio_capture is not None else "caller",
            "desktop": self.bridge.describe() if self.bridge is not None else None,
            "tools": sorted(self.tool_runner.tools) if self.tool_runner else [],
            "agents": sorted(self.adapters),
            "memory_attached": self.memory_writer is not None,
            "events_seen": len(self.seen),
            "sink_failures": self.plugin.sink_failures,
            "warnings": list(self.report.warnings),
        }


__all__ = ["RuntimeReport", "VoiceRuntime"]
