"""The runtime assembly -- the line that wires voice into dispatch.

``VoiceRuntime.say`` is the one place a recognised utterance becomes a
dispatched turn. Its constructor split (``start()`` reads config and spawns the
orb) is what lets these tests build a runtime headlessly and inject a fake
dispatcher, so the wiring is asserted without a desktop build or a database.

Evidence level: AUTO (fake dispatcher, no subprocess, no socket).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import vox_plugin.runtime as runtime_module
from core.agents.contract import AgentChunk
from core.audio.acks import ACK_MUTE_CAP_S, ACK_MUTE_TAIL_S
from core.audio.capture import SounddeviceWakeCapture
from core.dispatch.dispatcher import DispatchResult
from core.state import VoiceState
from vox_plugin.runtime import (
    DUCK_CEILING_S,
    DUCK_FLOOR_S,
    WAKE_LOG_MAX,
    VoiceRuntime,
    _duck_seconds,
)


class FakeDispatcher:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls = []

    def dispatch(self, task, adapters, *, speaker=None):
        self.calls.append((task.text, task.session_id, speaker))
        return DispatchResult(route="agent", chunks=tuple(self.chunks), ok=True)


def test_reach_listening_moves_the_real_plugin_machine():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)

    assert runtime.plugin.machine.state == VoiceState.IDLE
    runtime._reach_listening()

    assert runtime.plugin.machine.state == VoiceState.LISTENING


def test_reach_listening_is_a_noop_when_already_listening():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._reach_listening()
    before = runtime.plugin.machine.sequence

    runtime._reach_listening()

    assert runtime.plugin.machine.state == VoiceState.LISTENING
    assert runtime.plugin.machine.sequence == before


def test_say_routes_a_turn_and_returns_to_listening():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    dispatcher = FakeDispatcher([
        AgentChunk(kind="text", text="the answer"),
        AgentChunk(kind="done"),
    ])
    runtime.dispatcher = dispatcher
    runtime.adapters = {}

    result = runtime.say("read the readme")

    assert result.text == "the answer"
    assert result.ok is True
    # The recognised text reached the dispatcher as a task, in this session.
    assert dispatcher.calls[0][0] == "read the readme"
    assert dispatcher.calls[0][1] == runtime.session_id
    # The turn ran the whole state path and ended back in LISTENING.
    assert runtime.plugin.machine.state == VoiceState.LISTENING
    assert runtime.turns == 1


def test_attach_microphone_only_enqueues_on_the_audio_thread():
    """The callback must not run a turn: it would block the audio device."""

    class StubCapture:
        def __init__(self) -> None:
            self.on_wake = None
            self.on_reject = None
            self.on_recognized = None

    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    dispatcher = FakeDispatcher([AgentChunk(kind="text", text="ok"), AgentChunk(kind="done")])
    runtime.dispatcher = dispatcher
    runtime.adapters = {}
    capture = StubCapture()

    runtime.attach_microphone(capture)
    capture.on_recognized("读一下 README")

    # Enqueued, not dispatched: no turn ran on the callback thread.
    assert dispatcher.calls == []
    assert runtime.utterances.qsize() == 1
    # The wake callbacks are pointed at the plugin too, so barge-in still works.
    # 现在中间隔了一层 ``_woken``：状态机那一步照旧，额外做的是弹球 + 应一声。
    assert capture.on_wake == runtime._woken
    # ``on_reject`` 同样隔了一层（``_wake_rejected``）：多做的事是记进唤醒漏斗和运行日志。
    # 一次被声纹拒绝的唤醒是使用者最需要看见的事件 —— 它和「根本没命中」长得一样。
    assert capture.on_reject == runtime._wake_rejected
    # KWS 命中（声纹之前）也要能被看见，否则「喊了没反应」分不清是麦克风还是声纹。
    assert capture.on_kws_hit == runtime._kws_hit


def test_the_wake_funnel_counts_all_three_layers_separately():
    """命中 / 接受 / 拒绝必须分开数。

    这三个数字合并之后就回答不了那个真正的问题了：「喊了没反应」到底是麦克风没进声音、
    唤醒词没命中，还是声纹把它拒了。实机诊断正是靠这个分离读出「KWS 16/16、声纹 0/16」。
    """
    runtime = VoiceRuntime(with_desktop=False)
    runtime.plugin.start()

    runtime._kws_hit("你好小沃")
    runtime._wake_rejected("你好小沃", "below threshold 0.5", 0.482)
    runtime._kws_hit("你好小沃")
    runtime._woken("你好小沃", 0.71)

    assert runtime.wake_stats == {
        "kws": 2,
        "accepted": 1,
        "rejected": 1,
        "listen_refused": 0,
        "listen_expired": 0,
    }
    verdicts = [entry["verdict"] for entry in runtime.wake_recent]
    assert verdicts == ["accepted", "kws", "rejected", "kws"], "新的在前"
    rejected = next(e for e in runtime.wake_recent if e["verdict"] == "rejected")
    assert rejected["score"] == 0.482
    assert rejected["reason"] == "below threshold 0.5"


def test_the_fourth_layer_of_the_funnel_is_wired_and_not_just_counted():
    """「唤醒接受了但识别器没开起来」这一层。

    capture 里那个计数器 2026-08-29 就加了，但 ``attach_microphone`` 没有接它的回调，
    也没有任何界面读它 —— 于是它增长的时候日志里一个字都不出现，和它不存在没有区别。
    这条断言钉的是**接线**，不是计数器本身：漏接是这一类缺陷的实际形态。
    """
    capture = MutableCapture()
    runtime = _wired(capture)
    written: list[dict] = []
    runtime.logbook = type(
        "Book", (), {"write": lambda _self, source, message, **f: written.append({**f, "source": source})}
    )()

    assert callable(capture.on_listen_refused), "sink 必须被接上，否则计数器是死的"
    capture.on_listen_refused("没有 ASR provider —— 唤醒之后不会转写任何东西")

    assert runtime.wake_stats["listen_refused"] == 1
    entry = runtime.wake_recent[0]
    assert entry["verdict"] == "listen_refused"
    assert "ASR" in entry["reason"]
    # error 级而不是 warn：它就是「唤醒了却没有后文」本身，不是一次正常的拒绝。
    assert written and written[0]["level"] == "error"


def test_the_wake_funnel_is_capped_and_never_grows_without_bound():
    runtime = VoiceRuntime(with_desktop=False)
    for _ in range(WAKE_LOG_MAX * 2):
        runtime._kws_hit("你好小沃")
    assert len(runtime.wake_recent) == WAKE_LOG_MAX
    assert runtime.wake_stats["kws"] == WAKE_LOG_MAX * 2, "计数不受环形上限影响"


def test_the_wake_wrapper_still_walks_the_state_machine():
    """包装层不能把状态机那一步弄丢：capture 靠 ``wake_detected`` 的返回值判断这次唤醒
    被接受了没有，而球和应答音都是**之后**才发生的事。"""

    class StubCapture:
        def __init__(self) -> None:
            self.on_wake = None
            self.on_reject = None
            self.on_recognized = None

    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.plugin.start()
    capture = StubCapture()
    runtime.attach_microphone(capture)

    events = capture.on_wake("你好问问", 0.91)

    kinds = [event["type"] for event in events]
    assert kinds == ["wake.detected", "state.changed"]
    assert runtime.plugin.machine.state.value == "listening"


def test_greeting_never_raises_when_there_is_no_bridge_and_no_acks():
    """欢迎动作失败绝不能让唤醒失败 —— 无头运行（没有球、没有应答音）是支持的模式。"""
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)

    runtime._greet()  # 不抛就是通过


class MutableCapture:
    """记下静音窗被压了几次、每次多久。"""

    def __init__(self) -> None:
        self.on_wake = None
        self.on_reject = None
        self.on_recognized = None
        self.windows: list[float] = []

    def mute_for(self, seconds: float) -> None:
        self.windows.append(seconds)


class BlockingAcks:
    """一个卡在播放里的应答音库，用来把「播放期间」这个瞬间钉住。"""

    def __init__(self, capture: MutableCapture) -> None:
        self.capture = capture
        self.entered = threading.Event()
        self.release = threading.Event()
        self.window_at_play: list[float] | None = None
        self.played = 0
        self.failed: dict[str, str] = {}

    def ensure(self):
        return []

    def play(self, **_kwargs):
        self.window_at_play = list(self.capture.windows)
        self.entered.set()
        self.release.wait(2.0)
        self.played += 1
        return "ack-x.wav"


def _wired(capture: MutableCapture) -> VoiceRuntime:
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.plugin.start()
    runtime.attach_microphone(capture)
    return runtime


def test_the_confirmation_sound_mutes_the_input_while_it_plays():
    """使用者 2026-08-30 报的「**有几率**在唤醒后不能进行后续的对话」。

    确认音是从扬声器出来的，而识别器在唤醒的同一个回调里就开了。没有静音窗的话，
    那 0.8–1.6 秒会被同一支麦克风采回去，端点在人开口之前就触发 —— 这一轮的「请求」
    于是是确认音自己或者一段空转写，两种都表现为「唤醒了但没有后文」。

    两件事都要断言：**窗口在唤醒那个线程上就开好**（晚一步识别器已经吃到确认音的开头），
    以及**播完之后窗口被收回来**（否则把一个偶发缺陷换成一个必然缺陷）。
    """
    capture = MutableCapture()
    runtime = _wired(capture)
    acks = BlockingAcks(capture)
    runtime.attach_acks(acks)

    capture.on_wake("你好问问", 0.91)

    assert capture.windows == [ACK_MUTE_CAP_S], "静音窗必须在唤醒那个线程上同步开好"
    assert acks.entered.wait(2.0), "应答音没被播"
    assert acks.window_at_play == [ACK_MUTE_CAP_S], "播放的时候窗口必须已经开着"

    acks.release.set()
    for _ in range(200):
        if len(capture.windows) >= 2:
            break
        time.sleep(0.01)

    assert capture.windows == [ACK_MUTE_CAP_S, ACK_MUTE_TAIL_S]
    assert acks.played == 1


def test_a_failed_confirmation_sound_still_gives_the_microphone_back():
    """播放抛异常也要收窗。一次播放失败不该让麦克风聋满 ``ACK_MUTE_CAP_S``。"""

    class BoomAcks:
        failed: dict[str, str] = {}

        def ensure(self):
            return []

        def play(self, **_kwargs):
            raise RuntimeError("扬声器坏了")

    capture = MutableCapture()
    runtime = _wired(capture)
    runtime.attach_acks(BoomAcks())

    with pytest.raises(RuntimeError):
        runtime._greet()  # 直接调，不经过那个 daemon 线程，异常才看得见

    assert capture.windows == [ACK_MUTE_TAIL_S]


def test_a_runtime_without_acks_never_mutes_the_input():
    """没有应答音就没有要盖住的声音。压一个窗口只会白白聋掉那么久。"""
    capture = MutableCapture()
    runtime = _wired(capture)

    capture.on_wake("你好问问", 0.91)
    time.sleep(0.05)

    assert capture.windows == []
    assert runtime.acks is None


def test_an_expired_listen_puts_the_state_back_instead_of_lying():
    """使用者 2026-08-30 报的：「唤醒后不立即说话，球会一直卡在在听阶段」。

    采集侧的聆听会自己结束（端点检测静默 2.4 秒就报一次，宽限期用完就收），但此前没有
    任何一条路把这件事告诉状态机 —— 它停在 LISTENING，球显示「在听」，而麦克风已经回到
    唤醒模式。**一个说谎的状态比「已经不听了」糟得多**：使用者会对着一个不听的球说话。
    """
    capture = MutableCapture()
    runtime = _wired(capture)
    capture.on_wake("你好问问", 0.91)
    assert runtime.plugin.machine.state is VoiceState.LISTENING

    capture.on_listen_expired(8.0)

    assert runtime.plugin.machine.state is VoiceState.IDLE
    assert runtime.wake_stats["listen_expired"] == 1
    assert runtime.wake_recent[0]["verdict"] == "listen_expired"
    kinds = [event["type"] for event in runtime.seen]
    assert kinds[-1] == "state.changed", "退回待机这一步必须发事件，否则球不会收"


def test_an_expiry_that_lands_after_a_turn_started_changes_nothing():
    """竞争条件：人恰好在超时的同一刻说了话，回合已经开始。

    这时把状态拽回 IDLE 会打断一个正在进行的回合 —— 所以 ``listening_expired`` 在
    非 LISTENING 状态下是空操作，而不是抛错、也不是强制归位。
    """
    capture = MutableCapture()
    runtime = _wired(capture)
    capture.on_wake("你好问问", 0.91)
    runtime.plugin.submit_text("现在几点")
    assert runtime.plugin.machine.state is VoiceState.THINKING

    capture.on_listen_expired(8.0)

    assert runtime.plugin.machine.state is VoiceState.THINKING


def test_a_hide_timer_is_not_started_without_a_bridge():
    """没有球就没有要收的东西。起一个空转的定时器只会在测试里留下一个活线程。"""
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)

    runtime._schedule_hide()

    assert runtime._hide_timer is None


def test_pump_runs_one_queued_utterance_as_a_turn():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    dispatcher = FakeDispatcher([AgentChunk(kind="text", text="答案"), AgentChunk(kind="done")])
    runtime.dispatcher = dispatcher
    runtime.adapters = {}
    runtime.utterances.put("现在几点")

    result = runtime.pump()

    assert result is not None
    assert result.text == "答案"
    assert dispatcher.calls[0][0] == "现在几点"
    assert runtime.plugin.machine.state == VoiceState.LISTENING


def test_pump_with_an_empty_queue_returns_none():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.dispatcher = FakeDispatcher([])
    runtime.adapters = {}

    assert runtime.pump() is None


def test_say_with_no_dispatcher_is_not_a_silent_noop():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.dispatcher = None
    runtime.adapters = {}

    with pytest.raises(RuntimeError, match="dispatcher"):
        runtime.say("hello")



class SequencedDispatcher:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.confirmed = []

    def dispatch(self, task, adapters, *, speaker=None):
        self.calls.append(task.id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def run_intent(self, task, intent, *, speaker=None):
        self.confirmed.append((task.id, intent.arguments))
        return DispatchResult(route="tool", ok=True)


class CounterResource:
    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self.cancels = []
        self.fail_start = False

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("cannot start")

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1

    def shutdown(self):
        return None

    def cancel(self, turn_id):
        self.cancels.append(turn_id)


class FakeToolRunner:
    tools = {}


class FakeBridge(CounterResource):
    alive = True
    approval = False
    error = None

    def await_confirmation(self, event):
        if self.error is not None:
            raise self.error
        return self.approval

    def send(self, event):
        return True

    def describe(self):
        return {"alive": self.alive}


def test_start_failure_rolls_back_resources_and_allows_retry(monkeypatch):
    capture = CounterResource()
    capture.fail_start = True
    store = CounterResource()
    runtime = VoiceRuntime(with_desktop=False, with_memory=True)
    runtime.plugin.attach_capture(capture)

    monkeypatch.setattr(
        runtime_module,
        "open_memory",
        lambda **_kwargs: (store, object(), object()),
    )
    monkeypatch.setattr(runtime_module, "open_tools", lambda **_kwargs: FakeToolRunner())
    monkeypatch.setattr(runtime, "_open_agents", lambda: ((), {}, []))

    with pytest.raises(RuntimeError, match="cannot start"):
        runtime.start()

    assert runtime._started is False
    assert runtime.dispatcher is None
    assert runtime.memory_store is None
    assert store.closes == 1
    assert capture.stops == 1

    capture.fail_start = False
    report = runtime.start()

    assert report.memory is True
    assert runtime._started is True
    assert capture.starts == 2

    runtime.close()
    assert capture.stops == 2


def test_close_then_start_reuses_attached_capture_and_tts(monkeypatch):
    capture = CounterResource()
    tts = CounterResource()
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime.plugin.attach_capture(capture)
    runtime.plugin.attach_tts(tts)
    monkeypatch.setattr(runtime, "_open_agents", lambda: ((), {}, []))

    runtime.start()
    runtime.close()
    runtime.start()

    assert capture.starts == 2
    assert runtime._started is True
    runtime.close()


def test_close_releases_owned_resources_once_and_resets_state():
    capture = CounterResource()
    tts = CounterResource()
    adapter = CounterResource()
    bridge = FakeBridge()
    store = CounterResource()
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime.plugin.attach_capture(capture)
    runtime.plugin.attach_tts(tts)
    runtime.plugin.running = True
    runtime.plugin.machine.state = VoiceState.THINKING
    runtime.adapters = {"fake": adapter}
    runtime.bridge = bridge
    runtime.memory_store = store
    runtime.dispatcher = object()
    runtime._active_task_id = "t-active"
    runtime._started = True

    runtime.close()
    runtime.close()

    assert capture.stops == 1
    assert tts.stops == 1
    assert adapter.cancels == ["t-active"]
    assert bridge.closes == 1
    assert store.closes == 1
    assert runtime.plugin.audio_capture is capture
    assert runtime.plugin.machine.state == VoiceState.IDLE
    assert runtime._started is False


def test_dispatch_failure_returns_failed_result_and_next_turn_still_runs():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.adapters = {}
    runtime.dispatcher = SequencedDispatcher(
        [
            RuntimeError("agent crashed"),
            DispatchResult(
                route="agent",
                chunks=(AgentChunk(kind="text", text="recovered"), AgentChunk(kind="done")),
                ok=True,
            ),
        ]
    )

    failed = runtime.say("first")
    recovered = runtime.say("second")

    assert failed.ok is False
    assert failed.reason == "dispatch failed: RuntimeError"
    assert recovered.text == "recovered"
    assert runtime.plugin.machine.state == VoiceState.LISTENING
    assert any(event["type"] == "task.failed" for event in runtime.seen)


def test_tts_and_memory_failures_do_not_break_a_successful_turn():
    class FailingTts:
        def speak(self, _text):
            raise RuntimeError("speaker unavailable")

    class FailingMemory:
        def write_turn(self, _text, *, role):
            raise RuntimeError(f"memory unavailable for {role}")

    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.plugin.attach_tts(FailingTts())
    runtime.plugin.attach_memory(FailingMemory(), None)
    runtime.adapters = {}
    runtime.dispatcher = FakeDispatcher(
        [AgentChunk(kind="text", text="answer"), AgentChunk(kind="done")]
    )

    result = runtime.say("question")

    assert result.ok is True
    assert result.text == "answer"
    assert runtime.plugin.machine.state == VoiceState.LISTENING


def test_confirmation_without_explicit_approval_stays_refused():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.bridge = FakeBridge()
    runtime.adapters = {}
    result = DispatchResult(
        route="tool",
        tool="shell.run",
        reason="confirmation required",
        needs_confirmation=True,
        ok=False,
    )
    dispatcher = SequencedDispatcher([result])
    runtime.dispatcher = dispatcher
    runtime._pending_confirm = {
        "version": "1",
        "type": "tool.confirm_required",
        "id": "confirm-1",
        "timestamp": "2026-08-22T00:00:00+00:00",
        "payload": {"command": "echo safe"},
    }

    returned = runtime.say("run echo safe")

    assert returned is result
    assert returned.needs_confirmation is True
    assert dispatcher.confirmed == []
    assert runtime.plugin.machine.state == VoiceState.LISTENING



def test_completion_failure_recovers_from_speaking_state():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.adapters = {}
    runtime.dispatcher = FakeDispatcher(
        [AgentChunk(kind="text", text="answer"), AgentChunk(kind="done")]
    )

    def fail_completion(_reply, *, speak=True):
        """签名要跟着生产走。少了 ``speak`` 这个关键字，这条测的就不是「完成失败之后能
        恢复」而是「桩的签名不对」—— 抛出来的会是 TypeError 而不是它要验的那个异常。"""
        runtime.plugin._state_event(VoiceState.SPEAKING, "test playback")
        raise RuntimeError("completion failed")

    runtime.plugin.complete_turn = fail_completion

    result = runtime.say("question")

    assert result.ok is False
    assert result.reason == "turn completion failed: RuntimeError"
    assert runtime.plugin.machine.state == VoiceState.LISTENING


def test_a_silent_turn_runs_everything_except_the_speaker():
    """``say(speak=False)``：同一轮，同样的事件，不出声。

    两个调用方要它 —— 控制台的打字聊天（`speak_segments` 是阻塞的，不给这个开关一句
    40 字的回答要等音频播完才出现在聊天框里）和消息通道（回复要发到微信，不该同时从
    本机音箱放出来）。**事件序列必须一个不少**：`tts.chunk` 照发，唤醒球照常显示这一轮
    说了什么，少的只有声音。
    """
    spoken: list[object] = []
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.adapters = {}
    # 生产里 ``start()`` 最后一步就是这一行。少了它 ``runtime.seen`` 永远是空的，
    # 而这条测试要断言的正是「事件一个不少」。
    runtime.plugin.on_event = runtime.on_event
    runtime.dispatcher = FakeDispatcher(
        [AgentChunk(kind="text", text="安静的回答"), AgentChunk(kind="done")]
    )
    runtime.plugin.attach_tts(
        SimpleNamespace(
            speak_segments=lambda chunks: spoken.append(chunks),
            is_stopped=lambda: False,
            stop=lambda: None,
        )
    )

    result = runtime.say("讲个笑话", speak=False)

    assert result.ok is True
    assert spoken == [], "speak=False 时不许碰合成器"
    types = [event["type"] for event in runtime.seen]
    for expected in ("turn.started", "asr.final", "llm.delta", "tts.chunk", "turn.done"):
        assert expected in types, expected


# ---------------------------------------------------------------------- 托盘

class TrayBridge:
    """只记下托盘更新与事件的桥。``alive`` 让 ``describe()`` 那条路也能走。"""

    alive = True

    def __init__(self, fail: bool = False) -> None:
        self.trays: list[tuple[str, bool]] = []
        self.events: list[dict] = []
        self.visible: list[bool] = []
        self.fail = fail

    def send(self, event):
        self.events.append(dict(event))
        return True

    def set_tray(self, *, state, paused):
        if self.fail:
            raise RuntimeError("tray is gone")
        self.trays.append((state, paused))
        return True

    def set_visible(self, visible):
        self.visible.append(bool(visible))
        return True

    def describe(self):
        return {"alive": self.alive}


class TrayCapture:
    """借用真实实现的那两个开关，不复制一份。

    ``pause_wake`` / ``resume_wake`` 直接指向生产代码：一个自己写了一遍开关语义的桩
    只能证明桩是对的。``begin_listening`` 是可注入的，因为这里要断言的是运行时**调没调
    它**，而它自己的语义在 tests/test_capture_listening.py 里钉。
    """

    pause_wake = SounddeviceWakeCapture.pause_wake
    resume_wake = SounddeviceWakeCapture.resume_wake

    def __init__(self, opens: bool = True) -> None:
        self.wake_paused = False
        self.opens = opens
        self.began: list[str] = []
        self.muted: list[float] = []

    def begin_listening(self, reason="manual"):
        self.began.append(reason)
        return self.opens

    def start(self, **_kwargs):
        self.started = True

    def stop(self):
        self.started = False

    def mute_for(self, seconds):
        self.muted.append(seconds)


def _tray_runtime(capture=None, bridge=None):
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.bridge = bridge if bridge is not None else TrayBridge()
    if capture is not None:
        runtime.plugin.attach_capture(capture)
    # 生产里 ``start()`` 最后一步就是这两行。少了它们 ``wake_detected`` 会抛
    # 「voice plugin is not running」—— 那不是托盘的缺陷，是这个夹具没搭到生产的形状。
    runtime.plugin.on_event = runtime.on_event
    runtime.plugin.start()
    runtime.bridge.trays.clear()
    runtime.bridge.events.clear()
    runtime.seen.clear()
    return runtime


def test_a_tray_click_pauses_and_resumes_the_wake():
    capture = TrayCapture()
    runtime = _tray_runtime(capture)

    runtime._from_desktop({"kind": "control", "action": "pause"})
    assert capture.wake_paused is True
    assert runtime.wake_paused is True

    runtime._from_desktop({"kind": "control", "action": "resume"})
    assert capture.wake_paused is False
    assert runtime.wake_paused is False
    # 每次都把当前状态推回托盘，菜单文字才不会和实际相反。
    assert [paused for _state, paused in runtime.bridge.trays] == [True, False]


def test_messages_that_are_not_tray_control_are_ignored():
    """确认答复走桥自己那条路（``await_confirmation``）。这里再处理一遍会把一次点击
    算成两次。"""
    capture = TrayCapture()
    runtime = _tray_runtime(capture)

    runtime._from_desktop({"kind": "confirm", "approved": True})
    runtime._from_desktop({"kind": "control", "action": "nonsense"})
    runtime._from_desktop("not a mapping")

    assert capture.wake_paused is False
    assert capture.began == []


def test_manual_wake_from_the_tray_reaches_listening_and_opens_the_recognizer():
    capture = TrayCapture()
    runtime = _tray_runtime(capture)

    assert runtime.wake_manually() is True

    assert runtime.plugin.machine.state == VoiceState.LISTENING
    assert capture.began == ["tray"]
    # 唤醒漏斗照常记一条：主动唤醒也是一次唤醒，不该在统计里凭空消失。
    assert runtime.wake_stats["accepted"] == 1
    assert runtime.wake_recent[0]["keyword"] == "tray"


def test_manual_wake_is_refused_while_the_wake_is_paused():
    """点了「暂停唤醒」还能从同一个菜单唤醒它，那个开关就不是开关。"""
    capture = TrayCapture()
    runtime = _tray_runtime(capture)
    runtime.pause_wake(True)

    assert runtime.wake_manually() is False

    assert capture.began == []
    assert runtime.plugin.machine.state == VoiceState.IDLE


def test_manual_wake_without_a_microphone_still_reaches_listening():
    """打字对话那条路没有 capture，但主动唤醒仍然该把界面带进「在听」。"""
    runtime = _tray_runtime()

    assert runtime.wake_manually() is True
    assert runtime.plugin.machine.state == VoiceState.LISTENING


def test_settings_without_a_url_says_so_instead_of_guessing():
    """URL 带 token，而 token 是控制台那一层生成的。运行时去猜端口只会打开一个 401。"""
    runtime = _tray_runtime()
    logged: list[tuple[str, str]] = []
    runtime.logbook = SimpleNamespace(
        write=lambda source, message, **fields: logged.append((source, message))
    )

    assert runtime.open_settings() is False
    assert any(source == "tray" for source, _message in logged)


def test_settings_opens_the_injected_url(monkeypatch):
    runtime = _tray_runtime()
    runtime.settings_url = "http://127.0.0.1:8765/?t=abc"
    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    assert runtime.open_settings() is True
    assert opened == ["http://127.0.0.1:8765/?t=abc"]


def test_only_state_changes_repaint_the_tray():
    """每条事件都重写菜单文字的托盘会在一轮里闪十几次。"""
    runtime = _tray_runtime()

    runtime.on_event({"type": "task.progress", "payload": {}})
    runtime.on_event({"type": "tool.finished", "payload": {}})
    assert runtime.bridge.trays == []

    runtime.on_event({"type": "state.changed", "payload": {"to": "listening"}})
    assert len(runtime.bridge.trays) == 1


def test_a_broken_tray_does_not_break_the_turn():
    """托盘是显示增强。一个坏掉的菜单不该让正在进行的一轮失败。"""
    runtime = _tray_runtime(bridge=TrayBridge(fail=True))

    runtime.on_event({"type": "state.changed", "payload": {"to": "thinking"}})

    assert runtime.seen[-1]["type"] == "state.changed"


# ------------------- 「这台机器上没装」要传到路由（2026-09-01）


class CheckAdapter:
    """自报可用性的假适配器。``check()`` 是生产里唯一的可用性来源。"""

    def __init__(self, name: str, available: bool = True, raises: bool = False) -> None:
        self._name = name
        self._available = available
        self._raises = raises
        self.checks = 0

    def describe(self):
        from core.agents.contract import AgentDescriptor

        return AgentDescriptor(name=self._name, kind="cli", capabilities=frozenset({"chat"}))

    def check(self):
        self.checks += 1
        if self._raises:
            raise RuntimeError("boom")
        if self._available:
            return {"name": self._name, "kind": "cli", "available": True}
        return {
            "name": self._name,
            "kind": "cli",
            "available": False,
            "reason": f"'{self._name}' is not on PATH",
        }


def test_an_agent_missing_from_this_machine_is_marked_on_the_router():
    """`config/agents.toml` 保留命令不在 PATH 上的条目，所以路由必须自己知道跳过它。

    实测这台机器上 `claude` 装在 `%APPDATA%\npm`，那个目录不在 PATH 上 —— 能力闸门把
    「帮我改这个函数」正确地送给它，然后整轮失败。
    """
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    here = CheckAdapter("relay")
    gone = CheckAdapter("claude", available=False)
    monkey = {"relay": here, "claude": gone}
    runtime._open_agents = lambda: (
        tuple(a.describe() for a in monkey.values()),
        dict(monkey),
        [],
    )

    runtime.start()
    try:
        blocked = runtime.dispatcher.router.describe()["unavailable"]
        assert blocked == {"claude": "'claude' is not on PATH"}
        # 清单里仍然有它 —— 「配了但没装」和「没配」必须能分开看。
        assert set(runtime.dispatcher.router.describe()["agents"]) == {"relay", "claude"}
    finally:
        runtime.close()


def test_a_check_that_raises_is_treated_as_unavailable():
    """探测自己炸了就当它不可用：派到一个连自检都跑不动的后端上只会晚一点失败。"""
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    broken = CheckAdapter("broken", raises=True)
    runtime._open_agents = lambda: ((broken.describe(),), {"broken": broken}, [])

    runtime.start()
    try:
        blocked = runtime.dispatcher.router.describe()["unavailable"]
        assert "broken" in blocked
        assert "check failed" in blocked["broken"]
    finally:
        runtime.close()


# ---------------------------------------------------- 连续对话（2026-09-02）


class FollowUpCapture(TrayCapture):
    """借用真实的 ``resume_listening``，好让「不清身份」这一条测的是生产代码。"""

    resume_listening = SounddeviceWakeCapture.resume_listening

    def __init__(self, opens: bool = True) -> None:
        super().__init__(opens=opens)
        self.listen_grace_s = 8.0
        self.enroll_only = False
        self._listening = False
        self.asr_provider = object()
        self.on_recognized = lambda text: None
        self.on_listen_refused = None
        self.listen_refusals = 0
        self.last_listen_refusal = ""
        self.started_listening = 0

    def _start_listening(self):
        self.started_listening += 1
        self._listening = True

    def _note_listen_refused(self, reason):
        self.listen_refusals += 1
        self.last_listen_refusal = reason


def _talking_runtime(capture, *, follow_up=True):
    runtime = _tray_runtime(capture)
    runtime.follow_up = follow_up
    runtime.dispatcher = FakeDispatcher(
        [AgentChunk(kind="text", text="好的"), AgentChunk(kind="done")]
    )
    runtime.adapters = {}
    return runtime


def test_after_the_reply_the_microphone_stays_open():
    """连续对话：回答说完之后不用再喊唤醒词。"""
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)

    runtime.say("现在几点")

    assert capture.started_listening == 1
    assert capture._listening is True
    assert runtime._following_up is True


def test_the_follow_up_window_keeps_the_verified_speaker():
    """**这一条是连续对话唯一的安全接缝。**

    托盘的主动唤醒必须把身份清成 ``None``（那一刻没有音频可比对）。这一条相反：几秒之前
    才有一次真的声纹通过，而这个窗口是那次通过的延续 —— 清掉它会让「你好小沃，帮我看看 X」
    →「那再跑一下测试」里的第二句突然没有权限，一个在对话中途静默失权的助手比没有连续
    对话更糟。
    """
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)
    runtime.plugin.verified_speaker = "杜"

    runtime.say("现在几点")

    assert runtime.plugin.verified_speaker == "杜"
    assert runtime.effective_speaker == "杜"


def test_the_reply_is_muted_so_it_is_not_transcribed_as_the_next_request():
    """播放期间必须挡住转写 —— 不挡的后果不是多录一点环境声，是**助手把自己的回答
    转写成下一句请求**（识别器此刻是开着的）。

    **2026-09-03 从硬静音改成半双工窗。** 挡的东西没变（转写、电平、增益适应），
    继续跑的多了一样：**唤醒判定** —— 那是「朗读时能随时打断」的前提。采集侧不支持
    ``duck_for`` 时退回硬静音，所以这条测试对两种桩都要成立。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    runtime.say("讲个笑话")

    # 播放前开窗、播放（阻塞）返回之后收窗再压短尾巴。窗长按**回答的字数**估，
    # 不是 ACK_MUTE_CAP_S —— 那个 5 秒是给一句 1.5 秒的确认音定的。
    assert capture.ducked[0] >= DUCK_FLOOR_S
    assert capture.unducks >= 1
    assert capture.muted[-1] == ACK_MUTE_TAIL_S


def test_the_half_duplex_window_is_sized_from_the_reply_not_from_the_ack():
    """**长回答的窗必须比 5 秒长。**

    此前这里用的是 `ACK_MUTE_CAP_S`，而 `core/audio/acks.py` 把那个 5 秒定义为「足够盖住
    最长那句确认音（实测 1.56 s）」。项目自己的验收标准是「回答 40 字以内」，40 字按 4 字/秒
    是 10 秒 —— 窗在第 5 秒到期，之后自适应增益开始跟着**助手自己的声音**适应，等人开口时
    增益已经偏了。症状是「说完一段长回答之后唤醒变差」，而每一层都报告自己健康。
    """
    long_reply = "这是一段四十个字左右的回答" * 4
    assert len(long_reply) >= 40
    assert _duck_seconds(long_reply) > ACK_MUTE_CAP_S
    # 短回答仍然拿下限，不会开出一个比确认音还短的窗
    assert _duck_seconds("好") == DUCK_FLOOR_S
    # 上限是保险丝：再长的文本也不会让麦克风「不适应增益」超过它
    assert _duck_seconds("字" * 10_000) == DUCK_CEILING_S


def test_a_long_reply_really_gets_the_longer_window():
    """不只是那个纯函数对 —— 走一遍 `say()`，窗长必须真的跟着回答走。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    long_reply = "把这句话说得足够长以便撑开半双工窗" * 3
    runtime.dispatcher = FakeDispatcher(
        [AgentChunk(kind="text", text=long_reply), AgentChunk(kind="done")]
    )
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    runtime.say("讲个长笑话")

    assert capture.ducked[0] > ACK_MUTE_CAP_S


def test_follow_up_can_be_turned_off():
    """关掉就退回「每一句都要先喊唤醒词」，那是此前的行为。"""
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture, follow_up=False)

    runtime.say("现在几点")

    assert capture.started_listening == 0
    assert runtime._following_up is False


def test_a_paused_wake_refuses_the_follow_up_window():
    """点了「暂停唤醒」之后连续对话也不该把话筒留着 —— 那个开关的语义是「现在不要听我说话」。"""
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)
    capture.pause_wake()

    runtime.say("现在几点")

    assert capture.started_listening == 0
    assert capture.listen_refusals == 1
    assert runtime._following_up is False


def test_the_orb_countdown_waits_for_the_follow_up_window():
    """挂着连续对话窗口时不起收球倒计时：两个倒计时里短的那个会在人正要接着说的时候
    把球收走。窗口结束（``on_listen_expired``）时才起。"""
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)

    runtime.say("现在几点")
    assert runtime._hide_timer is None, "挂着窗口时不该有倒计时"

    runtime._listen_expired(8.0)

    assert runtime._following_up is False
    assert runtime._hide_timer is not None, "窗口结束了才起倒计时"
    runtime._cancel_hide()


def test_follow_up_failure_never_fails_the_turn():
    """连续对话是增强。它开不起来时这一轮的回答仍然算成功。"""

    class Broken(FollowUpCapture):
        def resume_listening(self, reason="follow-up"):
            raise RuntimeError("device went away")

    runtime = _talking_runtime(Broken())

    result = runtime.say("现在几点")

    assert result.ok is True
    assert runtime._following_up is False


# ---------------------------------------------------- 结束本次对话（2026-09-03）


def test_a_farewell_never_reaches_the_dispatcher():
    """「退下吧」不派发。

    一次派发是 2–20 秒和一次出网，换回来的是一句礼貌话 —— 而这句话要的不是话，是动作。
    """
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)

    result = runtime.say("退下吧")

    assert runtime.dispatcher.calls == [], "结束对话不该走派发"
    assert result.route == "dismiss"
    assert result.ok is True
    assert result.text == runtime_module.FAREWELL


def test_a_farewell_closes_the_microphone_instead_of_reopening_it():
    """**这一条是这个功能的全部要点。**

    让 agent 来答「退下吧」的话，它会说一句「好的再见」，然后 ``_speak_and_follow_up``
    照旧把话筒留开 8 秒 —— 那正是使用者刚刚要关掉的东西。
    """
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)

    runtime.say("结束本次对话")

    assert capture.started_listening == 0, "结束之后不该再开聆听"
    assert runtime._following_up is False
    assert runtime.plugin.machine.state == VoiceState.IDLE


def test_a_farewell_hides_the_orb_at_once():
    """不是十秒倒计时：人刚明确说了结束，一颗还在桌面上待十秒的球说的是反话。"""
    bridge = TrayBridge()
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)
    runtime.bridge = bridge

    runtime.say("没事了")

    assert bridge.visible[-1] is False
    assert runtime._hide_timer is None, "立刻收，所以不该留着倒计时"


def test_a_farewell_is_still_a_whole_turn():
    """事件序列一条不少，少的只有中间那次派发。

    绕开状态机自己发几个事件的「快路径」会让这一轮在唤醒球和日志里凭空消失，
    而「我说了它没反应」和「它听见了并且结束了」必须能分开。
    """
    bridge = TrayBridge()
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)
    runtime.bridge = bridge

    runtime.say("退下吧")

    types = [event["type"] for event in bridge.events]
    for expected in ("turn.started", "asr.final", "llm.delta", "tts.chunk", "turn.done"):
        assert expected in types, expected
    # 最后一条状态是待机，而且它排在 turn.done 之后 —— 顺序反了就是「先收摊再说完」。
    states = [event for event in bridge.events if event["type"] == "state.changed"]
    assert states[-1]["payload"]["to"] == "idle"
    assert types.index("turn.done") < len(types) - 1
    assert runtime.turns == 1


def test_the_farewell_is_muted_like_any_other_playback():
    """告别语也是从扬声器出来的，而采集此刻已经回到唤醒模式 —— 不压窗就是让它听自己说话。"""
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    runtime.say("拜拜")

    assert capture.muted[0] == ACK_MUTE_CAP_S
    assert capture.muted[-1] == ACK_MUTE_TAIL_S


def test_an_ordinary_request_still_runs_a_turn():
    """反向的护栏：结束判定不许把普通请求吞掉。"""
    capture = FollowUpCapture()
    runtime = _talking_runtime(capture)

    result = runtime.say("帮我结束这个进程")

    assert runtime.dispatcher.calls[0][0] == "帮我结束这个进程"
    assert result.route == "agent"
    assert runtime._following_up is True


# ------------------------------------------ 朗读时打断（半双工窗，2026-09-03）


class BargeInCapture(FollowUpCapture):
    """记下半双工窗的开关，好断言「打断之后没有再压尾巴」。"""

    def __init__(self, opens: bool = True) -> None:
        super().__init__(opens=opens)
        self.ducked: list[float] = []
        self.unducks = 0
        self.verified_speaker = "杜"

    def duck_for(self, seconds):
        self.ducked.append(seconds)

    def unduck(self):
        self.unducks += 1


def test_the_reply_opens_a_half_duplex_window_not_a_hard_mute():
    """回答的播放走半双工窗：**转写停，唤醒判定继续** —— 那是能打断的前提。

    此前这里压的是硬静音窗，而硬静音在音频回调最前面 ``return``，播放期间 KWS 一块音频
    都收不到。「必须等它读完或者重新喊唤醒词」的成因就在这一行上。
    """
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    runtime.say("放一句话")

    assert capture.ducked, "回答没有开半双工窗 —— 那样就打断不了"
    assert capture.unducks >= 1, "播放返回之后没有收窗；上限是保险丝不是窗口长度"


def test_a_barge_in_does_not_swallow_the_start_of_the_interrupting_sentence():
    """打断之后**不压尾巴静音窗**。

    那 0.25 秒是给扬声器余响留的；打断的时候扬声器已经停了、人正在说话，压下去等于把
    他那句话的头 0.25 秒吃掉 —— 症状是「打断成功了但它听不全我说什么」。
    """
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)

    class InterruptingTts:
        """播放到一半模拟一次唤醒命中，就像人喊了「你好小沃」。"""

        def __init__(self) -> None:
            self.stopped = 0

        def speak_segments(self, segments):
            runtime._woken("你好小沃", 0.62)
            return None

        def stop(self):
            self.stopped += 1

    runtime.plugin.attach_tts(InterruptingTts())
    capture.muted.clear()

    runtime.say("这是一段会被打断的回答")

    assert capture.muted == [], f"打断之后仍然压了静音窗：{capture.muted}"
    assert capture.unducks >= 1, "打断之后半双工窗还挂着 —— 挂着的那段正是人在说的话"


def test_a_barge_in_does_not_re_open_the_follow_up_window():
    """打断走的是唤醒的正常路径，识别器已经由 ``_authorise`` 开好了。这里再
    ``resume_listening()`` 会看到 ``_listening`` 已经是 True 而返回 False，然后
    「连续对话没开起来」被记成一条警告 —— 那是个假故障。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)

    class Interrupting:
        def speak_segments(self, segments):
            runtime._woken("你好小沃", 0.62)

        def stop(self):
            pass

    runtime.plugin.attach_tts(Interrupting())
    written: list[dict] = []
    runtime.logbook = type(
        "Book",
        (),
        {"write": lambda _s, source, message, **f: written.append({**f, "message": message})},
    )()
    before = capture.started_listening

    runtime.say("会被打断")

    assert not any("连续对话没开起来" in row["message"] for row in written), written
    assert capture.started_listening == before, "打断之后又开了一次聆听 —— 那会重置端点计时"


def test_a_barge_in_is_logged_as_an_interruption_not_a_plain_wake():
    """日志措辞要分开。一次打断和一次从待机叫起来在读日志的人眼里是两件事，而它们此前
    印的是同一句「命中『你好小沃』」。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    # 先把状态机推到 SPEAKING —— 打断只在「正在回答」时才叫打断。
    runtime._reach_listening()
    runtime.plugin.submit_text("先开一轮")
    written: list[str] = []
    runtime.logbook = type(
        "Book", (), {"write": lambda _s, source, message, **f: written.append(message)}
    )()

    runtime._woken("你好小沃", 0.62)

    assert any("打断了正在朗读的回答" in message for message in written), written


# ------------------------------------------ 「进度怎么样了」本机答（2026-09-03）


def test_a_progress_question_is_answered_locally_without_dispatching():
    """一次派发是 2–20 秒和一次出网，而这句话的意思正是「我不想再等了」。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))
    dispatcher = runtime.dispatcher

    result = runtime.say("进度怎么样了")

    assert result.route == "progress"
    assert dispatcher.calls == [], "问进度跑了一次派发"
    assert result.ok is True


def test_the_report_names_the_work_in_flight_with_a_number():
    """报进度不能只说「在忙」—— 带一个秒数才是可行动的。"""
    runtime = _talking_runtime(BargeInCapture())
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))
    runtime._inflight = {"task": "t-1", "text": "把这个仓库的测试跑一遍", "started": 0.0}

    result = runtime.say("进度怎么样了")

    assert "把这个仓库的测试跑一遍" in result.text
    assert "秒" in result.text


def test_after_a_turn_the_report_falls_back_to_the_last_one():
    """被打断之后问「刚才那个怎么了」是最自然的追问，而那时手上已经没活了。
    没有这份快照，答案只能是「现在没事在做」—— 一句正确但没用的话。"""
    runtime = _talking_runtime(BargeInCapture())
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    runtime.say("讲个笑话")
    result = runtime.say("进度怎么样了")

    assert "刚才" in result.text and "讲个笑话" in result.text


def test_an_idle_runtime_does_not_invent_work():
    runtime = _talking_runtime(BargeInCapture())
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    result = runtime.say("你在干什么")

    assert result.text == "现在没有在做的事"


def test_asking_for_progress_keeps_the_microphone_open():
    """他问这一句多半是为了决定下一步做什么。让他为了说下一句再喊一次唤醒词，
    正是这个功能想解决的那种摩擦。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))

    runtime.say("进度怎么样了")

    assert capture._listening is True
    assert runtime._following_up is True


# ------------------------- 停嘴不需要授权：打断的真正修法（2026-09-03）


def test_a_kws_hit_stops_the_speaker_before_the_voiceprint_gate():
    """**这一条是「朗读时打断不了」的真正修法。**

    上一版把停 TTS 放在 `wake_detected` 里，也就是声纹门**之后**。可是打断发生在扬声器正在
    响的时候，而那时环形缓冲里是「人声 + 扬声器串音」—— 使用者用音箱不戴耳机（他自己实测那样
    唤醒率最好），串音把相似度压到门槛以下，于是唤醒被拒、TTS 一个字都没停。半双工窗让 KWS
    听得见了，而听得见之后那一步仍然卡在门上。

    停自己的嘴**不是安全动作**：它不读文件、不跑命令、不动状态机。所以它在门前面。
    """
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    stops = []
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None,
        is_stopped=lambda: False,
        stop=lambda: stops.append(1),
    ))
    runtime._reach_listening()
    runtime.plugin.submit_text("先开一轮")  # 状态推到 THINKING

    runtime._kws_hit("你好小沃")

    assert stops, "KWS 命中时没停嘴 —— 那就是「必须等它读完」"
    assert capture.unducks >= 1, "半双工窗没收掉，而挂着的那段正是人在说的话"


def test_hushing_does_not_move_the_state_machine():
    """状态由 `wake_detected`（过了声纹）或者这一轮自己的收尾来改。在这里改状态会让
    「声纹没过」变成「状态说在听但没人在听」—— 一个说谎的状态。"""
    runtime = _talking_runtime(BargeInCapture())
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    ))
    runtime._reach_listening()
    runtime.plugin.submit_text("先开一轮")
    before = runtime.plugin.machine.state

    runtime._kws_hit("你好小沃")

    assert runtime.plugin.machine.state is before


def test_an_idle_kws_hit_does_not_touch_the_speaker():
    """待机时命中不该去动播放 —— 那时没有在播的东西，而一次多余的 `stop()` 会把
    「刚才那次为什么停了」变成一个要查的问题。"""
    runtime = _talking_runtime(BargeInCapture())
    stops = []
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: stops.append(1)
    ))

    runtime._kws_hit("你好小沃")

    assert stops == []


def test_a_rejected_barge_in_still_left_the_room_quiet():
    """两种结果都要可用：门过了 → 正常打断并开始听；**门没过 → 声音停了**，
    人在安静里再喊一次，而这一次没有串音。在这之前第二种情况是「它继续读完，你无能为力」。"""
    capture = BargeInCapture()
    runtime = _talking_runtime(capture)
    stops = []
    runtime.plugin.attach_tts(SimpleNamespace(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: stops.append(1)
    ))
    runtime._reach_listening()
    runtime.plugin.submit_text("先开一轮")

    runtime._kws_hit("你好小沃")          # 声音停了
    runtime._wake_rejected("你好小沃", "below threshold 0.5", 0.41)  # 门没过

    assert stops, "门没过就连声音都没停 —— 那是修之前的行为"
    assert runtime.wake_stats["rejected"] == 1


# -- 让合成提前开始（预热，2026-09-05）-----------------------------------------


class WarmTts:
    """记下 `prewarm` 收到了什么。其余照 `SimpleNamespace` 那几个桩的形状。"""

    def __init__(self, *, accepts: bool = True) -> None:
        self.warmed: list[str] = []
        self.spoken: list[list[str]] = []
        self.accepts = accepts

    def prewarm(self, text: str) -> bool:
        self.warmed.append(text)
        return self.accepts

    def speak_segments(self, chunks) -> None:
        self.spoken.append(list(chunks))

    def is_stopped(self) -> bool:
        return False

    def stop(self) -> None:
        pass


class StreamingDispatcher:
    """按块喂 `on_partial`，就像真派发器那样。"""

    def __init__(self, pieces) -> None:
        self.pieces = list(pieces)
        self.on_partial = None

    def dispatch(self, task, adapters, *, speaker=None):
        del task, adapters, speaker
        buffered = ""
        for piece in self.pieces:
            buffered += piece
            if self.on_partial is not None:
                self.on_partial(buffered)
        return DispatchResult(
            route="agent",
            chunks=(AgentChunk(kind="text", text=buffered), AgentChunk(kind="done")),
            ok=True,
        )


def _warm_runtime(tts, pieces):
    runtime = VoiceRuntime(with_desktop=False, with_memory=False, visible=False)
    runtime._started = True
    dispatcher = StreamingDispatcher(pieces)
    dispatcher.on_partial = runtime._prewarm_speech
    runtime.dispatcher = dispatcher
    runtime.adapters = {}
    runtime.plugin.attach_tts(tts)
    return runtime


def test_the_first_sentence_goes_to_synthesis_before_the_reply_is_finished():
    """预热的判据不是「快了多少」（计时断言在忙机器上会假红），是**它在回答写完之前
    就已经拿到第一句话了**，而且拿到的和真要说的第一段逐字节相同。"""
    tts = WarmTts()
    runtime = _warm_runtime(tts, ["好，", "开好了。", "还要", "别的吗？"])

    result = runtime.say("打开网易云")

    assert tts.warmed == ["好，开好了。"]
    assert tts.spoken[0][0] == "好，开好了。", "预热那一段必须就是真要说的第一段"
    assert result.ok


def test_nothing_is_prewarmed_until_the_first_sentence_can_no_longer_change():
    """一段的时候第一段还会变：`split_speech` 会把「只剩标点的一段」折回上一句
    （`你好。` 后面来一个 `！` 变成 `你好。！`），于是预热出来的音频差一个字节 —— 白花。"""
    tts = WarmTts()
    runtime = _warm_runtime(tts, ["你好。", "！"])

    runtime.say("在吗")

    assert tts.warmed == [], "整段回答只切出一句，预热无从判断第一段已经定下来"


def test_a_reply_with_a_tool_call_is_not_prewarmed():
    """那一轮的回答会被 `_settle_tool_calls` 整个换掉（工具结果回给模型再问一次），
    预热的是一段不会被说出口的文字。播错音是不可能的（文本逐字节比对），代价只是浪费。"""
    tts = WarmTts()
    runtime = _warm_runtime(tts, ["好。", "⟦vox:app.open {\"name\": \"网易云\"}⟧", "开好了。"])

    runtime.say("打开网易云")

    assert tts.warmed == []


def test_a_silent_turn_never_spends_a_synthesis():
    """`speak=False` 的两个调用方（控制台打字、微信）不出声，为它们预热是白花额度。"""
    tts = WarmTts()
    runtime = _warm_runtime(tts, ["好，", "开好了。", "还要别的吗？"])

    runtime.say("打开网易云", speak=False)

    assert tts.warmed == []


def test_prewarming_happens_at_most_once_per_turn_and_again_next_turn():
    """每轮至多一次（在途请求最多一个），但**每轮都要试** —— 上一轮排上过不代表这一轮。"""
    tts = WarmTts()
    runtime = _warm_runtime(tts, ["一。", "二。", "三。"])

    runtime.say("第一轮")
    runtime.say("第二轮")

    assert tts.warmed == ["一。", "一。"]


def test_a_provider_without_prewarm_is_asked_once_not_every_chunk():
    """本机那把嗓子没有这个方法。问一次就记住，不必每个 chunk 再 `getattr` 一遍。"""
    calls = []

    class Counting(SimpleNamespace):
        def __getattr__(self, name):
            if name == "prewarm":
                calls.append(name)
                raise AttributeError(name)
            raise AttributeError(name)

    tts = Counting(
        speak_segments=lambda chunks: None, is_stopped=lambda: False, stop=lambda: None
    )
    runtime = _warm_runtime(tts, ["一。", "二。", "三。"])

    result = runtime.say("说点什么")

    assert len(calls) == 1
    assert result.ok


def test_a_prewarm_that_raises_does_not_break_the_turn():
    """预热是旁路：它失败的正确后果是回到「等 LLM 写完再合成」，不是让这一轮的回答消失。"""

    class Angry(WarmTts):
        def prewarm(self, text: str) -> bool:
            raise RuntimeError("排不上")

    tts = Angry()
    runtime = _warm_runtime(tts, ["好，", "开好了。", "还要别的吗？"])

    result = runtime.say("打开网易云")

    assert result.ok and result.text == "好，开好了。还要别的吗？"
    assert tts.spoken, "照样出声"
