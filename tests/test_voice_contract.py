import pytest

from core.state import VoiceState
from evox_plugin import VoicePlugin


def test_voice_contract_lifecycle():
    plugin = VoicePlugin()
    plugin.start()
    types = [event["type"] for event in plugin.wake_detected("小沃小沃", 0.91)]
    types.extend(event["type"] for event in plugin.submit_text("现在几点了"))
    assert types == ["wake.detected", "state.changed", "turn.started", "asr.final", "state.changed"]
    assert plugin.status()["state"] == VoiceState.THINKING.value


def test_invalid_submit_and_cancel():
    plugin = VoicePlugin()
    with pytest.raises(RuntimeError):
        plugin.submit_text("hello")
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    assert plugin.cancel()["type"] == "turn.cancelled"
    assert plugin.machine.state == VoiceState.CANCELLED
