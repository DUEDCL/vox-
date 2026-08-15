"""TTS wiring into the voice path (complete_turn -> speak, cancel -> stop).

The TTS engine is opt-in, exactly like memory and tools: without one,
``complete_turn`` still emits ``tts.chunk`` and walks speaking -> done. With
one, the audio is a side effect between SPEAKING and turn.done, and its failure
must not end the turn.

Evidence level: AUTO (fake TTS engine, no speaker, no model).
"""

from __future__ import annotations

from core.state import VoiceState
from evox_plugin import VoicePlugin


class FakeTts:
    def __init__(self, raise_on_speak: bool = False) -> None:
        self.spoken: list[str] = []
        self.stopped = 0
        self.raise_on_speak = raise_on_speak

    def speak(self, text: str) -> None:
        if self.raise_on_speak:
            raise RuntimeError("audio device gone")
        self.spoken.append(text)

    def stop(self) -> None:
        self.stopped += 1


def ready_plugin() -> VoicePlugin:
    plugin = VoicePlugin()
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    plugin.submit_text("你好")
    return plugin


def test_complete_turn_emits_the_same_sequence_without_tts():
    plugin = ready_plugin()

    events = plugin.complete_turn("你好，我在")

    assert [e["type"] for e in events] == [
        "llm.delta", "tts.chunk", "state.changed", "turn.done", "state.changed"
    ]


def test_complete_turn_speaks_when_a_tts_engine_is_attached():
    tts = FakeTts()
    plugin = ready_plugin()
    plugin.attach_tts(tts)

    events = plugin.complete_turn("你好，我在")

    assert tts.spoken == ["你好，我在"]
    # The speak side effect adds no events: the contract is unchanged.
    assert [e["type"] for e in events] == [
        "llm.delta", "tts.chunk", "state.changed", "turn.done", "state.changed"
    ]


def test_a_failing_tts_does_not_end_the_turn():
    plugin = ready_plugin()
    plugin.attach_tts(FakeTts(raise_on_speak=True))

    events = plugin.complete_turn("你好，我在")

    assert [e["type"] for e in events] == [
        "llm.delta", "tts.chunk", "state.changed", "turn.done", "state.changed"
    ]
    assert plugin.machine.state == VoiceState.LISTENING


def test_cancel_stops_inflight_tts():
    tts = FakeTts()
    plugin = ready_plugin()
    plugin.attach_tts(tts)
    plugin.machine.transition(VoiceState.SPEAKING, "test")

    plugin.cancel()

    assert tts.stopped == 1
    assert plugin.machine.state == VoiceState.CANCELLED


def test_attach_tts_is_opt_in():
    plugin = VoicePlugin()

    assert plugin.attach_tts(None) == {"tts_attached": False}
    assert plugin.attach_tts(FakeTts()) == {"tts_attached": True}

