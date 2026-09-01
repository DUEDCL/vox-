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
from vox_plugin.runtime import WAKE_LOG_MAX, VoiceRuntime


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

    def fail_completion(_reply):
        runtime.plugin._state_event(VoiceState.SPEAKING, "test playback")
        raise RuntimeError("completion failed")

    runtime.plugin.complete_turn = fail_completion

    result = runtime.say("question")

    assert result.ok is False
    assert result.reason == "turn completion failed: RuntimeError"
    assert runtime.plugin.machine.state == VoiceState.LISTENING


# ---------------------------------------------------------------------- 托盘

class TrayBridge:
    """只记下托盘更新与事件的桥。``alive`` 让 ``describe()`` 那条路也能走。"""

    alive = True

    def __init__(self, fail: bool = False) -> None:
        self.trays: list[tuple[str, bool]] = []
        self.events: list[dict] = []
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
