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

from core.agents.contract import AgentChunk, AgentDescriptor, Task
from core.agents.registry import LLM_AGENT, apply_llm_profile, load_agents_config, open_agents
from core.audio.acks import ACK_MUTE_CAP_S, ACK_MUTE_TAIL_S
from core.desktop_bridge import DesktopBridge, DesktopBridgeError, find_desktop_binary
from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher, DispatchResult
from core.dispatch.breaker import CircuitBreaker
from core.dispatch.contract import Intent
from core.dispatch.intent import RuleBasedIntentResolver, is_dismissal, is_progress_query
from core.events import AGENT_SCHEMA_PATH, build_event, validate_event
from core.memory import open_memory
from core.models_config import active_llm, load_models_config
from core.state import VoiceState
from core.tools import open_tools
from vox_plugin.plugin import VoicePlugin

#: Event types the orb answers rather than merely displays.
_ANSWERED = frozenset({"tool.confirm_required"})

#: 唤醒漏斗留最近多少次尝试。**只在内存里**，不落盘 —— 它带关键词和相似度，那是
#: 「谁在什么时候试图唤醒」的记录，和运行日志同一个姿态（环形、进程内、不进磁盘）。
#: 30 条约等于一次调声纹的完整过程，够回答「刚才那几次为什么被拒」。
WAKE_LOG_MAX = 30

#: 「退下吧」之后回的那一句。**短**：它出现在使用者已经决定结束的时刻，多一个字都是挽留。
#:
#: 它同时是「怎么回来」的说明 —— 连续对话的窗口不会再开，下一句必须重新喊唤醒词。一个
#: 只回「好的」的助手把这件事留给使用者自己去发现，而发现的方式通常是对着一颗已经收起来
#: 的球说话。
FAREWELL = "好，随时叫我"

#: 麦克风峰值 -> 球的振幅的放大倍数。
#:
#: 正常说话时一块 100 ms 的峰值大约落在 0.05–0.4（这台机器实测），而球的振幅是 0–1。
#: 不放大的话球几乎不动 —— 那和没接这条线看起来一模一样，是最难查的那种「功能不生效」。
#: 3.0 让普通音量说话大致占满行程，喊起来会被上限截住（那是对的：再大的声音也只是满）。
LEVEL_GAIN = 3.0


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
    #: **连续对话**：回答说完之后不收话筒，直接等下一句（不用再喊唤醒词）。
    #:
    #: 窗口长度不在这里 —— 它是采集侧的 ``listen_grace_s``（见 ``follow_up_seconds``）。
    #: 关掉它就退回「每一句都要先喊唤醒词」，那是此前的行为。
    follow_up: bool = True
    #: 现在是不是挂着一个连续对话窗口。挂着的时候**不起收球倒计时** —— 窗口结束时才起。
    _following_up: bool = field(default=False, init=False, repr=False)
    #: 这一次唤醒是不是打断了正在朗读的回答。由 ``_woken`` 在状态机之前置位，
    #: ``_speak_and_follow_up`` 的收尾读它 —— 打断之后**不能**压尾巴静音窗、也不能
    #: 重开聆听窗，因为人已经在说话了，而 `_authorise` 已经把识别器开好了。
    _barged_in: bool = field(default=False, init=False, repr=False)
    #: 控制台地址（带令牌），托盘的「设置」用它。``None`` = 托盘上点了只记一条日志。
    #: 由启动方注入而不是在这里拼：令牌属于控制台那一侧，运行时不该去猜端口。
    settings_url: str | None = None
    #: 交给球那个子进程的环境变量 —— 它的外观（渲染层 / 尺寸 / 出不出文字）就是这么传的。
    #: 由启动方从 ``config/voice.toml`` 的 ``[orb]`` 翻译过来
    #: （``core/audio/config.py::orb_environment``），运行时不读配置文件。
    orb_env: dict[str, str] = field(default_factory=dict)
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
    #: 隐式记忆的晋升器（``core/memory/promote.py``）。``None`` = 不自动记 ——
    #: 记忆关掉时它也不该存在，否则会往一个不存在的 store 里写。
    promoter: Any = None
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
    #: 现在手上那件活（``None`` = 空闲）。「进度怎么样了」读它。
    #: 在派发**之前**写：派发是阻塞的，写在之后就永远等不到那一行。
    _inflight: dict[str, Any] | None = field(default=None, init=False, repr=False)
    #: 上一件做完（或被打断）的活的快照。被打断之后问「刚才那个怎么了」是最自然的追问，
    #: 而那时 ``_inflight`` 已经空了 —— 没有这一份，答案只能是「现在没事在做」。
    _last_done: dict[str, Any] | None = field(default=None, init=False, repr=False)

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

            router = DefaultRouter(
                descriptors,
                breaker=CircuitBreaker(on_event=self.on_event),
                memory_recaller=self.memory_recaller,
                memory_writer=self.memory_writer,
            )
            # **把「这台机器上没装」告诉路由。** `config/agents.toml` 刻意保留命令不在 PATH
            # 上的条目（丢掉它会让「少一个 agent」和「配错一个 agent」无法区分），代价是
            # 路由会选中一个跑不起来的后端 —— 实测 `claude` 装在 `%APPDATA%\npm` 而那个
            # 目录不在 PATH 上，于是能力闸门把「帮我改这个函数」正确地送给它，然后整轮失败。
            # 清单里仍然列出它和原因，只是这一轮不会被派到它身上。
            for name, reason in self._unavailable(self.adapters).items():
                router.mark_unavailable(name, reason)

            self.dispatcher = Dispatcher(
                router=router,
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
            # 隐式记忆：不用说「记住」也记得住。记忆没开时它是 None —— 一个往不存在的
            # store 里写的晋升器只会每轮记一条警告。
            if self.memory_writer is not None and self.promoter is None:
                from core.memory.promote import MemoryPromoter  # noqa: PLC0415

                self.promoter = MemoryPromoter(
                    writer=self.memory_writer,
                    recaller=self.memory_recaller,
                    store=self.memory_store,
                )

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

        ``config/models.toml`` 的当前方案在这里盖到 ``relay`` 上 —— **在此之前那个文件
        只有控制台在读**，于是「模型配置」那一栏是个能编辑、能保存、什么都不影响的面板，
        而对话实际走的是 `agents.toml` 里的端点与凭据。套用与没套用都会留一句话：这一层
        存在的全部理由就是「配了但没生效」。
        """
        notes: list[str] = []
        try:
            config = load_agents_config()
            config, notes = apply_llm_profile(config, active_llm(load_models_config()))
            # 工具清单交给 http 后端（它的 system prompt 会带上）。**只报注册过的那些**，
            # 而且只报**真的装了**的 —— 印一个不存在的工具，模型会去调它然后拿回「没装」，
            # 那一轮就白花了 2–20 秒。`open_tools` 在这之前跑（见 `start`），所以这里拿得到。
            installed = tuple(sorted(getattr(self.tool_runner, "tools", ()) or ()))
            adapters = open_agents(config, tools=installed)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            return (), {}, [f"agents are off: {type(exc).__name__}: {exc}"]
        opened: dict[str, Any] = {}
        descriptors: list[AgentDescriptor] = []
        warnings: list[str] = list(notes)
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
    def _unavailable(adapters: Mapping[str, Any]) -> dict[str, str]:
        """哪些后端这台机器上跑不起来，以及原因。``check()`` 不打网络。

        和 ``_availability`` 的分工：那一个把结果变成给人看的启动警告，这一个把它变成
        路由的输入。同一次 ``check()`` 调两遍是刻意的 —— 它很便宜（``shutil.which`` 或
        读几个字段），而把两个消费者耦合到一个缓存上会让「先算警告还是先算路由」变成
        一个顺序依赖。
        """
        blocked: dict[str, str] = {}
        for name, adapter in adapters.items():
            try:
                status = adapter.check()
            except Exception as exc:  # noqa: BLE001 - 探测失败按「不可用」处理
                blocked[name] = f"check failed: {type(exc).__name__}"
                continue
            if isinstance(status, Mapping) and status.get("available") is False:
                blocked[name] = str(status.get("reason") or "unavailable")
        return blocked

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
        bridge = DesktopBridge(
            visible=self.visible,
            on_incoming=self._from_desktop,
            # 球的外观（渲染层 / 尺寸 / 出不出文字）走子进程环境变量。**这三项以前只能
            # 靠人手工设**，控制台那一栏只是生成一行让人复制 —— 而一项只能靠环境变量传的
            # 配置在使用者的路径里等于不存在。现在由 `config/voice.toml` 的 `[orb]` 填。
            environment=self.orb_env,
        )
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

    def say(self, text: str, *, speak: bool = True) -> DispatchResult:
        """Run one turn and recover to ``LISTENING`` on unexpected failures.

        The audio callback only queues text; nevertheless this method is also
        callable by a worker or UI thread, so dispatcher and confirmation errors
        are contained here rather than escaping into the capture loop.

        ``speak=False`` 走完同样的一轮但不出声。两个调用方要它：控制台的打字聊天
        （`speak_segments` 是阻塞的，不给这个开关一句 40 字的回答要等音频播完才出现在
        聊天框里）和消息通道（回复要发到微信，不该同时从本机音箱放出来）。
        """
        if not self._started:
            self.start()
        if self.dispatcher is None:
            raise RuntimeError("voice runtime dispatcher is not available")
        self._reach_listening()
        # 「退下吧」「结束本次对话」—— 这一句不派给任何后端，应一声就收。
        # 判定在 core/dispatch/intent.py 的 is_dismissal（纯函数、整句锚定）。
        if is_dismissal(text):
            return self._dismiss(text)
        # 「进度怎么样了」—— 问的是**手上这件事**，本机答，不派发。
        #
        # 把「你在干什么」发给云端 agent 是荒谬的：它不知道，而且答它还要再等一轮 ——
        # 而这句话的全部意义就是「我不想再等了，先告诉我情况」。
        if is_progress_query(text):
            return self._report_progress(text)
        self.plugin.submit_text(text)
        self.turns += 1
        task = Task(id=f"t-{self.turns}", text=text, session_id=self.session_id)
        self.log("turn", f"第 {self.turns} 轮：{text[:120]}", turn=self.turns, text=text)
        self._remember_facts(text)
        self._active_task_id = task.id
        # 「进度怎么样了」要答得出来，所以现在手上是什么活得被记下来。**在派发之前记**：
        # 派发是阻塞的，记在之后就永远等不到那一行。
        self._inflight = {"task": task.id, "text": text, "started": time.monotonic()}
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
            # 手上这件事结束了。**留一份快照**（`_last_done`）给「进度怎么样」用 ——
            # 被打断之后问「刚才那个怎么了」是最自然的一次追问，而那时 `_inflight` 已经空了。
            if self._inflight is not None:
                self._last_done = {
                    **self._inflight,
                    "elapsed_s": round(time.monotonic() - float(self._inflight["started"]), 1),
                }
                self._inflight = None

        try:
            self._speak_and_follow_up(self._spoken(result), speak=speak)
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
        #
        # **连续对话开着的时候不在这里起倒计时。** 那个窗口结束时（``on_listen_expired``）
        # 会自己起 —— 在这里也起一个的话，两个倒计时里短的那个会在人正要接着说的时候
        # 把球收走。
        if not self._following_up:
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
        # 真实电平 -> 唤醒球的振幅。在这之前球只在换状态时收到一个固定值，所以「在听」
        # 那一态是个匀速的呼吸 —— 球一直在动，但它动的不是你说的话。限流在桥接那一层
        # （`set_level`），这里只是把回调接上。
        capture.on_level = self._level_seen
        report = self.plugin.attach_capture(
            capture,
            on_recognized=self.utterances.put,
            on_wake=self._woken,
            on_reject=self._wake_rejected,
        )
        return report

    def _speak_and_follow_up(self, reply: str, *, speak: bool = True) -> None:
        """说出这一轮的回答，然后**留着话筒**等下一句 —— 连续对话。

        三件事按这个顺序，每一条都有它必须在这个位置的理由：

        1. **播放前开半双工窗。** ``complete_turn`` 里的 ``speak_segments`` 是阻塞的，播放
           期间麦克风照常在录 —— 而连续对话把识别器留着开，于是不压窗的后果不是「多录一点
           环境声」，是**助手把自己的回答转写成下一句请求**。上限只是保险丝，真正决定窗口
           长度的是播放返回之后那一下（和确认音同一个做法，见 ``core/audio/acks.py``）。
        2. **播放返回之后立刻收窗**，再压一个短尾巴收余响与房间反射。
        3. **再开聆听。** 顺序反过来（先开聆听再播放）就等于把回答喂给识别器。

        **朗读期间可以随时打断（2026-09-03）。** 这里用的是 ``_duck_input`` 而不是硬静音：
        半双工窗只关转写、电平和增益适应，**唤醒判定与环形缓冲继续跑**。所以朗读到一半喊
        「你好小沃」会命中 KWS → 走声纹门 → ``wake_detected`` 在 SPEAKING 时 ``cancel()``
        停掉 TTS 并进 LISTENING。

        此前这里是硬静音窗，而硬静音在音频回调**最前面**就 ``return`` —— 播放期间 KWS
        一块音频都收不到，想打断的那句话落在一个聋掉的麦克风上。那就是「必须等它读完或者
        重新喊唤醒词」的全部成因，不是别的哪一层不灵。

        两个仍然存在的代价：戴音箱且音量大时缓冲里是「人声 + 串音」，声纹相似度会掉，
        表现是打断偶尔要说两次（戴耳机几乎没有）；助手自己的声音触发 KWS 时会被声纹门
        拒掉 —— 那是**门在后面**才敢开这个窗的原因。
        """
        capture = getattr(self.plugin, "audio_capture", None)
        self._barged_in = False
        if speak and (self.acks is not None or self.plugin.tts is not None):
            self._duck_input(ACK_MUTE_CAP_S)
        try:
            self._complete_and_watch_tts(reply, speak=speak)
        finally:
            # 播放返回就是真的放完了（或者被打断了）：先收掉半双工窗，再压一个短尾巴。
            # 不收的话上限那一段会继续挂着，而它是保险丝不是窗口长度。
            self._unduck_input()
            # **被打断时不压尾巴。** 那 0.25 秒是给扬声器余响留的，而打断的时候扬声器
            # 已经停了、人正在说话 —— 压下去等于把他那句话的头 0.25 秒吃掉。
            if not self._barged_in:
                self._mute_input(ACK_MUTE_TAIL_S)
        self._following_up = False
        if self._barged_in:
            # 打断之后识别器已经由 `_authorise` 开好了（那是唤醒的正常路径），这里再
            # `resume_listening()` 会看到 `_listening` 已经是 True 而返回 False，然后
            # 「连续对话没开起来」被记成一条警告 —— 那是个假故障。直接让位。
            self._barged_in = False
            return
        if not self.follow_up or capture is None:
            return
        resume = getattr(capture, "resume_listening", None)
        if not callable(resume):
            return
        try:
            opened = bool(resume("follow-up"))
        except Exception as exc:  # noqa: BLE001 - 连续对话失败不该让这一轮失败
            self.log("turn", f"连续对话没开起来：{type(exc).__name__}: {exc}", level="warn")
            return
        self._following_up = opened
        if opened:
            self.log(
                "turn",
                f"接着听下一句（{self.follow_up_seconds():g} 秒内没人说话就收）",
                follow_up_s=self.follow_up_seconds(),
            )

    def _complete_and_watch_tts(self, reply: str, *, speak: bool = True) -> None:
        """说出这一轮的回答，**并且把合成失败写进日志**。

        `VoicePlugin.complete_turn` 刻意吞掉合成异常（一次合成失败不该结束回合），
        所以「有没有出声」这件事只能在这里看：比一次 `tts_failures`。不这么做的后果
        2026-09-02 在真机上出现过 —— 云端合成每轮回 HTTP 401，而使用者看到的是
        「助手一句话都不出声」，日志里一个字都没有。

        error 级不是夸张：对一个语音助手来说不出声与没听见、崩了、网断了在使用者那一侧
        完全同形，而控制台「只看错误」那一档正是他会打开的地方。
        """
        before = int(getattr(self.plugin, "tts_failures", 0) or 0)
        try:
            self.plugin.complete_turn(reply, speak=speak)
        finally:
            after = int(getattr(self.plugin, "tts_failures", 0) or 0)
            if after > before:
                self.log(
                    "tts",
                    f"合成失败，这一轮没出声：{getattr(self.plugin, 'last_tts_error', '') or '未说明原因'}",
                    level="error",
                    failures=after,
                )

    def switch_llm(
        self, model: str, *, agent: str = LLM_AGENT, persist: bool = True
    ) -> dict[str, Any]:
        """**不重启就换模型。** 借的是 Hermes 那条 `/model provider:model`。

        为什么这一条值得单独做：模型是这个产品里唯一「想试一下就换、换错了马上换回去」的
        配置。它和唤醒词、麦克风、球不一样 —— 那几个换一次要重建一个常驻对象（模型文件、
        音频流、子进程），而 LLM 后端只是一个 URL 加一个名字。让它跟着「重启生效」走，
        等于把一次两秒的动作变成一次十几秒的停顿。

        做的事只有三件：换掉适配器、把新的描述子注册回路由、（可选）写回
        `config/models.toml`。**熔断与历史成绩不动** —— `router.register` 明写它不碰那些，
        而换个模型名不该把这个后端过去的失败记录清空。

        `persist=False` 用于「只想试这一句」：进程内生效，重启回到文件里那个。
        """
        if self.dispatcher is None or not self.adapters:
            raise RuntimeError("agent 还没起来，没法换模型")
        wanted = str(model or "").strip()
        if not wanted:
            raise ValueError("要一个模型名")
        # 收「provider:model」这种写法（Hermes 的形状），冒号左边就是 agent 名。
        if ":" in wanted:
            head, _, tail = wanted.partition(":")
            if head.strip():
                agent = head.strip()
            wanted = tail.strip()
        if not wanted:
            raise ValueError("冒号右边是空的")
        current = self.adapters.get(agent)
        if current is None:
            raise ValueError(f"没有名为 {agent!r} 的后端（有的是：{', '.join(sorted(self.adapters))}）")
        url = getattr(current, "url", "")
        if not url:
            raise ValueError(f"{agent} 不是 http 后端，换模型这条路只对 http 后端成立")
        from core.agents.http import HttpAgentAdapter

        descriptor = current.describe()
        replacement = HttpAgentAdapter(
            url=url,
            name=descriptor.name,
            capabilities=descriptor.capabilities,
            cost=descriptor.cost,
            latency_ms=descriptor.latency_ms,
            timeout_s=descriptor.timeout_s,
            model=wanted,
            key_env=getattr(current, "key_env", ""),
        )
        previous = getattr(current, "model", "")
        self.adapters[agent] = replacement
        self.dispatcher.router.register(replacement.describe())
        self._close_resource(current)
        wrote = ""
        if persist:
            wrote = self._persist_llm_model(wanted)
        self.log(
            "model",
            f"换模型：{agent} {previous or '(未设)'} → {wanted}"
            + (f"，已写回 {wrote}" if wrote else "，仅本次运行"),
            agent=agent,
            model=wanted,
            persisted=bool(wrote),
        )
        return {"agent": agent, "model": wanted, "previous": previous, "persisted": wrote}

    def _persist_llm_model(self, model: str) -> str:
        """写回 `config/models.toml` 的当前方案。失败不抛 —— 换模型已经生效了。"""
        try:
            from core.models_config import (
                load_models_config,
                models_config_path,
                write_profile_kind,
            )

            config = load_models_config()
            active = str(config.get("active") or "")
            if not active:
                return ""
            write_profile_kind(active, "llm", {"model": model}, path=models_config_path())
            return "config/models.toml"
        except Exception as exc:  # noqa: BLE001 - 写不回去不该让换模型失败
            self.log("model", f"换模型生效了，但写回配置失败：{type(exc).__name__}: {exc}", level="warn")
            return ""

    def follow_up_seconds(self) -> float:
        """连续对话的窗口有多长。**由采集侧的 ``listen_grace_s`` 决定，不另设一个。**

        两个数字各自可调的话它们一定会分岔，而分岔之后「球什么时候收」和「话筒什么时候关」
        就不是同一件事了 —— 那正是使用者会看到「球还在但它已经不听了」的成因。
        """
        capture = getattr(self.plugin, "audio_capture", None)
        return float(getattr(capture, "listen_grace_s", 0.0) or 0.0)

    def _report_progress(self, text: str) -> DispatchResult:
        """「进度怎么样了」：本机答，不派发。

        ## 为什么这一句不能派出去

        把「你在干什么」发给云端 agent 有两个问题，而第二个是致命的：它**不知道**（在流程
        里它只看到自己那一轮），以及答它**还要再等一轮**。而这句话的全部意义就是「我不想
        再等了，先告诉我情况」—— 用一次 2–20 秒的出网去回答它，正好把它问的那件事变得更糟。

        ## 报什么

        三种情况三句话，每一句都带**数字**：

        * 手上有活 —— 报它是什么、跑了多少秒。
        * 刚做完 / 刚被打断 —— 报那件事和它花了多久（`_last_done` 的快照）。
        * 从来没有过 —— 如实说空闲，不编一个「正在处理」。

        朗读期间可以打断（见 `capture.duck_for`），所以「打断 → 问进度」是一条真实路径，
        而它现在有一个 0 秒、不出网的答案。
        """
        self.plugin.submit_text(text)
        self.turns += 1
        inflight = self._inflight
        if inflight is not None:
            waited = round(time.monotonic() - float(inflight.get("started", 0.0)), 1)
            answer = f"还在做「{str(inflight.get('text', ''))[:24]}」，已经 {waited:g} 秒了"
        elif self._last_done is not None:
            done = self._last_done
            answer = (
                f"刚才那件「{str(done.get('text', ''))[:24]}」用了 "
                f"{float(done.get('elapsed_s', 0.0)):g} 秒，现在没事在做"
            )
        else:
            answer = "现在没有在做的事"
        self.log(
            "turn",
            f"第 {self.turns} 轮：{text[:60]}（问进度，本机答，不派发）",
            turn=self.turns,
            text=text,
            route="progress",
        )
        self._following_up = False
        self._complete_and_watch_tts(answer)
        # 问完进度**要接着听** —— 他问这一句多半是为了决定下一步做什么，
        # 而让他为了说下一句再喊一次唤醒词，正是这个功能想解决的那种摩擦。
        capture = getattr(self.plugin, "audio_capture", None)
        resume = getattr(capture, "resume_listening", None)
        if self.follow_up and callable(resume):
            try:
                self._following_up = bool(resume("progress"))
            except Exception:  # noqa: BLE001 - 开不起来就退回「要再喊一次唤醒词」
                self._following_up = False
        if not self._following_up:
            self._schedule_hide()
        return DispatchResult(
            route="progress",
            # 和 `_dismiss` 同一个做法：答案走 chunks，因为 `DispatchResult.text` 是从
            # chunks 派生的属性而不是字段 —— 直接传 `text=` 会是 TypeError。
            chunks=(AgentChunk(kind="text", text=answer),),
            reason="local progress report",
            ok=True,
        )

    def _dismiss(self, text: str) -> DispatchResult:
        """「退下吧」：应一句，然后**真的结束** —— 不派发、不再开窗口、球立刻收。

        ## 为什么不让后端来答这一句

        一次派发是 2–20 秒和一次出网，而这句话的正确回答是固定的一句。更要紧的是它要的
        **不是一句话而是一个动作**：连续对话的窗口必须不再打开。让 agent 来答的话它会礼貌
        地说「好的再见」，然后 ``_speak_and_follow_up`` 照旧把话筒开着等 8 秒 —— 那正是
        使用者刚刚要关掉的东西。

        ## 为什么仍然走 ``submit_text`` / ``complete_turn``

        事件序列一条不少（turn.started → asr.final → THINKING → llm.delta → tts.chunk →
        SPEAKING → turn.done），记忆里的这一轮也照旧写。少的只有中间那次派发。绕开状态机
        自己发几个事件的「快路径」会让这一轮在唤醒球和日志里凭空消失，而「我说了它没反应」
        与「它听见了并且结束了」必须能分开。

        静音窗和 ``_speak_and_follow_up`` 同一个做法：播放前压、返回后补一个短尾巴 ——
        这一句是从扬声器出来的，而采集此刻已经回到唤醒模式，不压窗就是让它听自己说话。
        """
        self.plugin.submit_text(text)
        self.turns += 1
        self.log(
            "turn",
            f"第 {self.turns} 轮：{text[:120]}（听成「结束对话」，不派发）",
            turn=self.turns,
            text=text,
            route="dismiss",
        )
        # 先清标记再说话：``complete_turn`` 会阻塞到播完，而这中间若有别的线程读到
        # ``_following_up`` 还是 True，它会以为窗口还开着。
        self._following_up = False
        if self.acks is not None or self.plugin.tts is not None:
            self._mute_input(ACK_MUTE_CAP_S)
        try:
            self._complete_and_watch_tts(FAREWELL)
        finally:
            self._mute_input(ACK_MUTE_TAIL_S)
        # ``complete_turn`` 的最后一步是回 LISTENING（连续对话的落点），所以这一步**必须
        # 在它之后**：结束的意思正是「不再等下一句」。两个状态事件背靠背同步发出，球看到的
        # 是 SPEAKING → LISTENING → IDLE，中间那一下不到一毫秒，不会被看见。
        #
        # 不用把返回的事件再转一遍：``VoicePlugin._emit`` 已经扇出到 ``self.on_event`` 了。
        self.plugin.end_conversation()
        self._hide_now()
        self.log(
            "turn",
            f"第 {self.turns} 轮完成：route=dismiss ok=True（本次对话结束）",
            turn=self.turns,
            route="dismiss",
            ok=True,
            answer=FAREWELL,
        )
        return DispatchResult(
            route="dismiss",
            chunks=(AgentChunk(kind="text", text=FAREWELL),),
            reason="使用者结束了本次对话",
            ok=True,
        )

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
        """KWS 命中，声纹之前。这一条是「第 2 层通过了」的唯一证据。

        **同时：正在朗读的话立刻停嘴。** 这是「朗读时打断不了」的真正修法。

        上一版把停 TTS 放在 ``wake_detected`` 里，也就是**声纹门之后**。可是打断发生在扬声器
        正在响的时候，而那时环形缓冲里是「人声 + 扬声器串音」—— 使用者用音箱不戴耳机（他自己
        实测那样唤醒率最好），串音把余弦相似度压到门槛以下，于是这次唤醒被拒，TTS 一个字都
        没停。半双工窗让 KWS **听得见**了，可听得见之后那一步仍然卡在门上。

        **停自己的嘴不需要授权。** 那不是一个安全动作，是一个界面动作 —— 它不读文件、不跑
        命令、不动状态机。所以它挪到门前面。声纹门保留它真正的职责：决定**要不要开识别器**
        （要不要听你接下来说什么）。

        于是两种结果都是可用的：门过了 → 正常打断并开始听；门没过 → 声音停了，你在安静里
        再喊一次，而这一次没有串音，门会过。而在这之前，第二种情况是「它继续读完，你无能为力」。
        """
        self.wake_stats["kws"] += 1
        self._record_wake(keyword=keyword, verdict="kws")
        speaking = self.plugin.machine.state in {VoiceState.THINKING, VoiceState.SPEAKING}
        if speaking:
            self._hush()
            self.log(
                "kws",
                f"唤醒词命中「{keyword}」—— 先停嘴，再过声纹",
                keyword=keyword,
                interrupted=True,
            )
            return
        self.log("kws", f"唤醒词命中「{keyword}」（还没过声纹）", keyword=keyword)

    def _hush(self) -> None:
        """立刻停掉正在播的回答，并收掉半双工窗。**跑在音频回调线程上，所以要便宜。**

        只碰播放，不碰状态机：状态由 ``wake_detected``（过了声纹）或者这一轮自己的收尾来改。
        一个在这里改状态的实现会让「声纹没过」变成「状态说在听但没人在听」。
        """
        tts = getattr(self.plugin, "tts", None)
        stopper = getattr(tts, "stop", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:  # noqa: BLE001 - 停不掉也不能带走音频线程
                pass
        self._unduck_input()

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
        # 连续对话的窗口就是靠这一条收口的：说完回答之后开的那个窗口没人接话，到点结束。
        # 措辞跟着分：一个刚回答完的助手说「聆听结束」读起来像出错了。
        following = self._following_up
        self._following_up = False
        # **云端识别失败会走到这一条上，而它和「真的没人说话」长得一样。** 云端那条路的
        # 失败以「空文本 + 端点」到达 capture（那是刻意的：一次网络失败不该结束整轮），
        # 于是宽限期走完就落在这里。没有下面这两行的话，一个 401 在使用者那一侧就是
        # 「我说了话它说我没说话」—— 2026-09-01 的 TTS 401 已经演过一遍这种失败。
        asr_error = ""
        capture = getattr(self.plugin, "audio_capture", None)
        taker = getattr(getattr(capture, "asr_provider", None), "take_error", None)
        if callable(taker):
            try:
                asr_error = str(taker() or "")
            except Exception:  # noqa: BLE001 - 报错这件事本身不该失败一轮
                asr_error = ""
        if asr_error:
            self.log(
                "asr",
                f"云端识别失败，所以这一轮没有文本：{asr_error}",
                level="error",
                reason=asr_error,
            )
        self.log(
            "wake",
            f"{'这一轮聊完了' if following else '聆听结束'}：{seconds:g} 秒内没听到说话，退回待机",
            seconds=seconds,
            follow_up=following,
        )
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
        # 打断标记要在状态机之前读：`wake_detected` 的第一件事就是把 SPEAKING 打断掉，
        # 读完之后状态已经是 LISTENING，分不出这次唤醒是「打断了正在朗读的回答」还是
        # 「从待机里叫起来的」。这两者的收尾动作不同 —— 见 `_speak_and_follow_up`。
        self._barged_in = self.plugin.machine.state in {VoiceState.THINKING, VoiceState.SPEAKING}
        events = self.plugin.wake_detected(keyword, score)
        self.wake_stats["accepted"] += 1
        self._record_wake(
            keyword=keyword,
            verdict="accepted",
            score=None if score is None else round(float(score), 3),
            barge_in=self._barged_in or None,
        )
        if self._barged_in:
            self.log("wake", f"打断了正在朗读的回答（「{keyword}」）", keyword=keyword, score=score)
        else:
            self.log("wake", f"命中「{keyword}」", keyword=keyword, score=score)
        # 静音窗要在**这个线程上**开，不能留给 _greet：这里跑在音频回调上，而
        # `_authorise` 紧接着就会 `_start_listening()` 开识别器。晚一步开窗，识别器就已经
        # 吃到了确认音的开头。见 core/audio/capture.py 的 `_mute_until` 那段注释。
        #
        # **打断的时候不应答音、不压窗。** 人正在说话，压 5 秒静音窗等于把他要说的整句
        # 都丢掉；而再应一声「你说吧」会盖在他嘴上。打断本身就是最好的确认 —— 声音停了。
        if self.acks is not None and not self._barged_in:
            self._mute_input(ACK_MUTE_CAP_S)
        if self._barged_in:
            # 朗读被打断：把半双工窗立刻收掉，否则它还挂着，而挂着的那段正是人在说的话。
            self._unduck_input()
            self._cancel_hide()
            return events
        threading.Thread(target=self._greet, daemon=True, name="vox-greet").start()
        return events

    def _remember_facts(self, text: str) -> None:
        """不用说「记住」它也记得住 —— 但**只记够证据的那些**。

        使用者的要求：「我把我的个人网站告诉他，下次对话他就能直接记住，而不是我说了
        『给我记住我的个人网站』他才会记住。」

        抽取与晋升闸门在 ``core/memory/promote.py``：一条第一人称自述（「我的网站是 X」）
        本身就是显式陈述，直接进长期层；只出现过一次又不是自述的留在候选层等下一次证据。
        闸门不是洁癖 —— Hermes 引的测量说例行记忆保存把短期污染固化成长期记忆最高到 91%。

        **吞掉异常**：记忆是增强不是对话的前提，和 ``_recall_context`` 同一个立场。
        跑在派发**之前**是因为这一轮的召回就该看见它 —— 使用者说完「我的网站是 X」紧接着
        问「我的网站是什么」是最自然的一次验证。
        """
        if self.promoter is None:
            return
        try:
            promoted = self.promoter.observe(text, session_id=self.session_id)
        except Exception as exc:  # noqa: BLE001 - 记忆失败不该让这一轮失败
            self.log("memory", f"记忆抽取失败：{type(exc).__name__}: {exc}", level="warn")
            return
        for candidate in promoted:
            self.log(
                "memory",
                f"记住了：{candidate.statement}（凭据：{candidate.channel}）",
                channel=candidate.channel,
                about=candidate.key,
            )

    def _level_seen(self, peak: float) -> None:
        """一块音频的峰值 -> 唤醒球的振幅。**跑在音频回调线程上，所以它必须便宜。**

        限流与「球看不见就不发」都在 ``DesktopBridge.set_level`` 里，这一层只做一次转发和
        一次异常吞。放大到 0–1：麦克风的峰值在正常说话时大约落在 0.05–0.4，直接送过去的球
        几乎不动 —— 那和不接这条线看起来一样。
        """
        if self.bridge is None:
            return
        try:
            self.bridge.set_level(min(1.0, float(peak) * LEVEL_GAIN))
        except Exception:  # noqa: BLE001 - 可视化失败绝不能带走音频线程
            pass

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

    def _duck_input(self, seconds: float) -> None:
        """扬声器要响这么久：**停转写，但唤醒词照听** —— 这是「随时打断」的开关。

        回答的播放走这条而不是 ``_mute_input``：硬静音窗在音频回调最前面就 ``return``，
        于是朗读期间 KWS 一块音频都收不到，想打断的那句话落在一个聋掉的麦克风上。那正是
        「必须等它读完或者重新喊唤醒词」的成因。

        应答音仍然走硬静音（``_greet``）：它只有 0.8–1.6 秒，而那一秒里人还没来得及决定
        要不要打断；更重要的是那时识别器**已经开着**，半双工窗关不掉它那一路的风险。

        采集侧不支持 ``duck_for``（老的替身、无麦克风运行）时退回硬静音 —— 宁可不能打断，
        也不能把回答转写成下一句请求。
        """
        capture = getattr(self.plugin, "audio_capture", None)
        ducker = getattr(capture, "duck_for", None)
        if not callable(ducker):
            self._mute_input(seconds)
            return
        try:
            ducker(seconds)
        except Exception:  # noqa: BLE001 - 半双工窗失败不该让这一轮失败
            self._mute_input(seconds)

    def _unduck_input(self) -> None:
        """立刻收掉半双工窗。播放结束或被打断时调用。"""
        capture = getattr(self.plugin, "audio_capture", None)
        for name in ("unduck", "unmute"):
            action = getattr(capture, name, None)
            if callable(action):
                try:
                    action()
                except Exception:  # noqa: BLE001
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

    def _hide_now(self) -> None:
        """立刻收球 —— 「结束本次对话」看得见的那一半。

        不走 ``_schedule_hide()``：那是十秒倒计时，为的是连续对话里球要留着当「我还在听」
        的信号。而这里人刚刚明确说了结束，一颗还在桌面上待十秒的球说的是反话。
        """
        self._cancel_hide()
        if self.bridge is None:
            return
        try:
            self.bridge.set_visible(False)
        except Exception:  # noqa: BLE001 - 球是增强，不是前提
            pass

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
