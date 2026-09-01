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
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from core.agents.contract import AgentDescriptor, Task
from core.agents.registry import load_agents_config, open_agents
from core.audio.acks import ACK_MUTE_CAP_S, ACK_MUTE_TAIL_S
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

#: 唤醒漏斗留最近多少次尝试。**只在内存里**，不落盘 —— 它带关键词和相似度，那是
#: 「谁在什么时候试图唤醒」的记录，和运行日志同一个姿态（环形、进程内、不进磁盘）。
#: 30 条约等于一次调声纹的完整过程，够回答「刚才那几次为什么被拒」。
WAKE_LOG_MAX = 30


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
    #: 控制台地址（带令牌），托盘的「设置」用它。``None`` = 托盘上点了只记一条日志。
    #: 由启动方注入而不是在这里拼：令牌属于控制台那一侧，运行时不该去猜端口。
    settings_url: str | None = None
    #: 收球的定时器。每次回合结束重置，所以连着说话时球不会中途消失。
    _hide_timer: Any = field(default=None, init=False, repr=False)
    #: 唤醒漏斗的计数与最近几次尝试。**给控制台看的**，不进事件（事件扇出到每个通道，
    #: 而「谁被拒了、分数多少」是个人数据的边缘）。
    #:
    #: 为什么要分三层数：一次被声纹拒绝的唤醒和一次根本没命中的唤醒在用户眼里长得一模
    #: 一样（都是「喊了没反应」），而根因完全不同 —— 前者要重注册声纹，后者要看麦克风
    #: 和词表。把 kws / accepted / rejected 分开报，这个问题在页面上就是一眼的事。
    wake_stats: dict[str, int] = field(
        default_factory=lambda: {
            "kws": 0,
            "accepted": 0,
            "rejected": 0,
            "listen_refused": 0,
            "listen_expired": 0,
        }
    )
    #: 最近的唤醒尝试（新的在前），最多 ``WAKE_LOG_MAX`` 条。带关键词、判定、分数、原因。
    wake_recent: list[dict[str, Any]] = field(default_factory=list)
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
            # 托盘上那一行「当前状态」跟着状态机走。只在 `state.changed` 上推，不是每条
            # 事件都推 —— 一个每条事件都重写菜单文字的托盘会在一轮里闪十几次。
            if envelope.get("type") == "state.changed":
                self._sync_tray()

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
        bridge = DesktopBridge(visible=self.visible, on_incoming=self._from_desktop)
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

        ``on_input_silent`` 也在这里接上：一个全零的输入设备**不会报错**（见
        ``core/audio/capture.py`` 的死麦克风检测），所以它必须主动进日志，否则症状是
        「唤醒词唤不醒」而每一层都报告自己健康。

        ``on_kws_hit`` 与 ``on_reject`` 同理：唤醒漏斗的三层（命中 / 接受 / 拒绝）都要
        能被看见，否则「喊了没反应」分不清是麦克风、词表，还是声纹。
        """
        capture.on_input_silent = self._input_silent
        capture.on_kws_hit = self._kws_hit
        # 「唤醒接受了但识别器没开起来」。**这个 sink 必须接上**：capture 里那个计数器
        # 2026-08-29 就加了，但没有任何人接它、也没有任何界面读它 —— 于是它增长的时候
        # 一个字都不会出现在日志里，和它不存在没有区别。
        capture.on_listen_refused = self._listen_refused
        # 「唤醒之后一直没人说话」。不接它的后果不是少一条日志：状态机会停在 LISTENING，
        # 唤醒球一直显示「在听」，而采集早就回到唤醒模式了 —— 一个说谎的状态。
        capture.on_listen_expired = self._listen_expired
        report = self.plugin.attach_capture(
            capture,
            on_recognized=self.utterances.put,
            on_wake=self._woken,
            on_reject=self._wake_rejected,
        )
        return report

    # ------------------------------------------------------------------ 托盘

    def _from_desktop(self, message: Mapping[str, Any]) -> None:
        """桌面侧传上来的消息。确认答复由桥自己处理，这里只接托盘的控制指令。

        **托盘不承担任何业务逻辑**（P4 的要求）：Rust 侧只发一个动作名，怎么做在这里。
        一个把「主动唤醒」实现成 Rust 直接开麦的托盘会绕过状态机、声纹门和事件流三样。
        """
        if not isinstance(message, Mapping) or message.get("kind") != "control":
            return
        action = str(message.get("action") or "")
        try:
            if action == "wake":
                self.wake_manually()
            elif action == "pause":
                self.pause_wake(True)
            elif action == "resume":
                self.pause_wake(False)
            elif action == "settings":
                self.open_settings()
        except Exception as exc:  # noqa: BLE001 - 托盘点一下不该让运行时倒下
            self.log("tray", f"{action} 失败：{type(exc).__name__}: {exc}", level="error")

    def wake_manually(self) -> bool:
        """主动唤醒：绕过唤醒词，直接进聆听。开起来了返回 ``True``。

        **绕过的是唤醒词，不是声纹门。** 点托盘的那一刻还没有人说话，所以不存在一段音频
        可以拿去比对 —— ``capture.begin_listening()`` 明确把已验证说话人清成 ``None``，
        于是 ``shell.run`` 这类要求身份的工具照旧被拒。这条不是妥协：一个能从菜单点出
        「已验证身份」的入口比没有声纹门更糟。

        没接麦克风时仍然进 LISTENING 并弹球 —— 那是「打字对话」那条路的正常形态。
        """
        capture = getattr(self.plugin, "audio_capture", None)
        if capture is not None and getattr(capture, "wake_paused", False):
            self.log("tray", "主动唤醒被拒：唤醒处于暂停中", level="warn")
            return False
        self._woken("tray", None)
        if capture is None:
            return True
        opened = bool(capture.begin_listening("tray"))
        if not opened:
            self.log("tray", "主动唤醒：识别器没开起来（见上一条 wake 日志）", level="warn")
        return opened

    def pause_wake(self, paused: bool) -> bool:
        """暂停 / 恢复唤醒词判定。返回当前是否处于暂停。

        麦克风不关 —— 见 ``core/audio/capture.py`` 的 ``pause_wake``：关掉设备再重开是个
        可能失败的动作，而「恢复」不该有失败的可能。
        """
        capture = getattr(self.plugin, "audio_capture", None)
        if capture is not None:
            if paused:
                capture.pause_wake()
            else:
                capture.resume_wake()
        self.log("tray", "唤醒已暂停" if paused else "唤醒已恢复", paused=paused)
        self._sync_tray()
        return paused

    @property
    def wake_paused(self) -> bool:
        return bool(getattr(getattr(self.plugin, "audio_capture", None), "wake_paused", False))

    def open_settings(self) -> bool:
        """打开控制台。``settings_url`` 没设就只记一条日志。

        URL 由启动方注入而不是在这里拼：它带令牌，而令牌属于控制台那一侧。
        """
        url = (self.settings_url or "").strip()
        if not url:
            self.log("tray", "没有控制台地址可开（settings_url 没设）", level="warn")
            return False
        import webbrowser

        self.log("tray", "打开控制台")
        return bool(webbrowser.open(url))

    def _sync_tray(self) -> None:
        """把状态与暂停开关推给托盘菜单。桥没接就什么都不做。"""
        if self.bridge is None:
            return
        try:
            self.bridge.set_tray(
                state=self.plugin.machine.state.value, paused=self.wake_paused
            )
        except Exception:  # noqa: BLE001 - 托盘是显示增强，不是回合的前提
            pass

    def _record_wake(self, **fields: Any) -> None:
        """往唤醒漏斗记一条。只记计数与判定，不记音频、不记向量。"""
        entry = {"at": time.time(), **fields}
        self.wake_recent.insert(0, entry)
        del self.wake_recent[WAKE_LOG_MAX:]

    def _kws_hit(self, keyword: str) -> None:
        """KWS 命中，声纹之前。这一条是「第 2 层通过了」的唯一证据。"""
        self.wake_stats["kws"] += 1
        self._record_wake(keyword=keyword, verdict="kws")
        self.log("kws", f"唤醒词命中「{keyword}」（还没过声纹）", keyword=keyword)

    def _listen_refused(self, reason: str) -> None:
        """唤醒被接受了，但识别器没开起来。**error 级，而且它就是「没有后文」本身。**

        球弹出来了、确认音也响了（那两件事走 ``on_wake``），可是没有一个字会被转写。
        这个状态此前在任何地方都看不见 —— capture 里的计数器没人接，界面也不读，
        于是它和「我说的话它听不懂」长得完全一样。
        """
        self.wake_stats["listen_refused"] = self.wake_stats.get("listen_refused", 0) + 1
        self._record_wake(verdict="listen_refused", reason=reason)
        self.log("wake", f"唤醒接受了但没进聆听：{reason}", level="error", reason=reason)

    def _listen_expired(self, seconds: float) -> None:
        """唤醒之后一直没人开口，聆听到点结束 —— **把状态退回待机并说出来。**

        不做这一步的后果不是少一条日志：状态机会停在 LISTENING，唤醒球一直显示「在听」，
        而采集其实已经回到唤醒模式了。使用者看到的是「球卡在在听，而且之后说话也不识别」。

        info 级而不是 warn：这不是故障，是正常的超时。但它必须留痕，否则「它怎么不听了」
        在日志里查不到。
        """
        self.wake_stats["listen_expired"] = self.wake_stats.get("listen_expired", 0) + 1
        self._record_wake(verdict="listen_expired", reason=f"{seconds:g}s 内没有语音")
        self.log("wake", f"聆听结束：{seconds:g} 秒内没听到说话，退回待机", seconds=seconds)
        for event in self.plugin.listening_expired(seconds):
            self.on_event(event)
        self._schedule_hide()

    def _wake_rejected(self, keyword: str, reason: str = "", score: float = 0.0) -> Any:
        """声纹拒了一次唤醒。**warn 级** —— 它不是错误（门在正常工作），但它是
        「我喊了它没反应」这句话在日志里的样子，所以必须显眼。
        """
        self.wake_stats["rejected"] += 1
        self._record_wake(
            keyword=keyword, verdict="rejected", score=round(float(score), 3), reason=reason
        )
        self.log(
            "wake",
            f"声纹拒绝「{keyword}」：{reason}",
            level="warn",
            keyword=keyword,
            score=round(float(score), 3),
            reason=reason,
        )
        return self.plugin.wake_rejected(keyword, reason, score)

    def _input_silent(self, details: Any) -> None:
        """输入设备在出零 —— 这是 error 级，不是 warn。

        它意味着**唤醒功能整体不工作**，而且从外面看不出来。用 error 级是为了让控制台
        「只看错误」那一档也能抓到它：一个人打开日志正是因为「喊了没反应」。
        """
        try:
            fields = dict(details) if isinstance(details, Mapping) else {"detail": details}
        except Exception:  # noqa: BLE001 - 诊断路径不能自己炸
            fields = {}
        self.log(
            "input",
            "输入设备没有声音（全零样本）—— 唤醒不可能命中，检查麦克风是否被静音或被系统隐私设置拒绝",
            level="error",
            **fields,
        )

    def _woken(self, keyword: str, score: float | None = None) -> Any:
        """唤醒命中：状态机先走，然后弹球 + 应一声。

        返回值仍然是 ``wake_detected`` 的事件列表 —— capture 靠它判断这次唤醒被接受了
        没有（被拒绝的唤醒走的是 ``on_reject``，根本不到这里）。
        """
        events = self.plugin.wake_detected(keyword, score)
        self.wake_stats["accepted"] += 1
        self._record_wake(
            keyword=keyword,
            verdict="accepted",
            score=None if score is None else round(float(score), 3),
        )
        self.log("wake", f"命中「{keyword}」", keyword=keyword, score=score)
        # 静音窗要在**这个线程上**开，不能留给 _greet：这里跑在音频回调上，而
        # `_authorise` 紧接着就会 `_start_listening()` 开识别器。晚一步开窗，识别器就已经
        # 吃到了确认音的开头。见 core/audio/capture.py 的 `_mute_until` 那段注释。
        if self.acks is not None:
            self._mute_input(ACK_MUTE_CAP_S)
        threading.Thread(target=self._greet, daemon=True, name="vox-greet").start()
        return events

    def _mute_input(self, seconds: float) -> None:
        """让采集在这段时间里丢弃输入。没接麦克风时什么也不做。"""
        capture = getattr(self.plugin, "audio_capture", None)
        muter = getattr(capture, "mute_for", None)
        if not callable(muter):
            return
        try:
            muter(seconds)
        except Exception:  # noqa: BLE001 - 静音窗失败不该让唤醒失败
            pass

    def _greet(self) -> None:
        """把球显示出来，再应一声。两个都吞异常：欢迎动作失败绝不能让唤醒失败。

        应答音是从扬声器出来的，而识别器此刻已经开着 —— 所以播放期间采集侧挂着一个
        **静音窗**（``_woken`` 里开、这里收）。没有它的话，那 0.8–1.6 秒会被采进转写：
        要么确认音自己变成了这一轮的请求，要么端点在人开口之前就触发，两种都表现为
        「唤醒了但没有后文」。窗口的准确长度由这里决定 —— ``play()`` 是阻塞的，它返回
        就是真的放完了，那时再压一个 ``ACK_MUTE_TAIL_S`` 的尾巴收尾。
        """
        self._cancel_hide()
        if self.bridge is not None:
            try:
                self.bridge.set_visible(True)
            except Exception:  # noqa: BLE001 - 球是增强，不是前提
                pass
        if self.acks is None:
            return
        try:
            self.acks.play()
        finally:
            # 无论播成没播成都要收窗：一次播放失败不该让麦克风聋满 ACK_MUTE_CAP_S。
            self._mute_input(ACK_MUTE_TAIL_S)

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
