"""The runtime assembly -- the line that wires voice into dispatch.

``VoiceRuntime.say`` is the one place a recognised utterance becomes a
dispatched turn. Its constructor split (``start()`` reads config and spawns the
orb) is what lets these tests build a runtime headlessly and inject a fake
dispatcher, so the wiring is asserted without a desktop build or a database.

Evidence level: AUTO (fake dispatcher, no subprocess, no socket).
"""

from __future__ import annotations

import pytest

import vox_plugin.runtime as runtime_module
from core.agents.contract import AgentChunk
from core.dispatch.dispatcher import DispatchResult
from core.state import VoiceState
from vox_plugin.runtime import VoiceRuntime


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
    assert capture.on_reject == runtime.plugin.wake_rejected


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
