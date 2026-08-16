"""The runtime assembly -- the line that wires voice into dispatch.

``VoiceRuntime.say`` is the one place a recognised utterance becomes a
dispatched turn. Its constructor split (``start()`` reads config and spawns the
orb) is what lets these tests build a runtime headlessly and inject a fake
dispatcher, so the wiring is asserted without a desktop build or a database.

Evidence level: AUTO (fake dispatcher, no subprocess, no socket).
"""

from __future__ import annotations

import pytest

from core.agents.contract import AgentChunk
from core.dispatch.dispatcher import DispatchResult
from core.state import VoiceState
from evox_plugin.runtime import VoiceRuntime


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
    assert capture.on_wake == runtime.plugin.wake_detected


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

    with pytest.raises(AssertionError):
        runtime.say("hello")

