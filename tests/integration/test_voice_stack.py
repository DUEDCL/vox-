"""Integration checks over the assembled voice stack.

Promoted from ``tmp_proto/t10_voice_stack_validation.py``, which stays in
``scripts/acceptance/`` as the evidence generator (it prints timings and RTF for
``docs/research/prototype-results.md``). What lives here instead is the part that
belongs in every regression run: the two behaviours the design red lines depend
on, and the guarantee that emitted events satisfy the contract.

Model-dependent checks skip when ``models/`` is absent, so this file passes on a
fresh clone -- the models are 413 MB and deliberately not in version control.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.events import validate_event
from core.state import VoiceState
from vox_plugin import VoicePlugin

ROOT = Path(__file__).resolve().parents[2]
KWS_DIR = ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
VAD_MODEL = ROOT / "models" / "silero_vad.onnx"


class RecordingTransport:
    """A swappable ``ConversationTransport`` that records what it was asked."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.sent: list[str] = []
        self.cancelled: list[str] = []
        self._turns = 0

    def send(self, text: str, *, session_id: str | None = None) -> dict:
        self._turns += 1
        self.sent.append(text)
        return {"turn_id": f"{self.tag}-{self._turns}", "reply": f"[{self.tag}] {text}"}

    def cancel(self, turn_id: str) -> dict:
        self.cancelled.append(turn_id)
        return {"cancelled": turn_id}


def _run_one_turn(tag: str) -> tuple[VoicePlugin, RecordingTransport]:
    transport = RecordingTransport(tag)
    plugin = VoicePlugin(transport=transport)
    plugin.start()
    plugin.wake_detected("你好问问", 0.87)
    plugin.submit_text("今天天气怎么样")
    return plugin, transport


def test_two_backends_drive_an_identical_turn_path():
    """Design red line 2: swapping the transport must not change the flow."""
    first, first_transport = _run_one_turn("backendA")
    second, second_transport = _run_one_turn("backendB")

    assert first_transport.sent == second_transport.sent
    assert first.machine.state == second.machine.state == VoiceState.THINKING
    # Same path, but genuinely different backends behind it.
    assert first.last_turn_id != second.last_turn_id
    assert first.last_reply != second.last_reply

    first.stop()
    second.stop()


def test_barge_in_cancel_reaches_the_transport_and_the_state_machine():
    """Interrupting during playback must cancel the pending turn, not just the UI."""
    plugin, transport = _run_one_turn("barge")
    plugin.complete_turn(plugin.last_reply or "……")
    plugin.submit_text("停")
    pending_turn = plugin.last_turn_id
    assert pending_turn is not None

    # Represent active TTS, then barge in.
    plugin.machine.state = VoiceState.SPEAKING
    event = plugin.cancel()

    assert event["type"] == "turn.cancelled"
    assert plugin.machine.state == VoiceState.CANCELLED
    assert transport.cancelled == [pending_turn]

    plugin.stop()
    assert plugin.machine.state == VoiceState.IDLE


def test_every_emitted_event_satisfies_the_contract():
    """One full turn's events, checked against contracts/voice-events.schema.json."""
    plugin, _ = _run_one_turn("contract")
    plugin.complete_turn("好的")
    plugin.cancel()
    plugin.stop()

    assert len(plugin.events) > 8
    for event in plugin.events:
        validate_event(event)


@pytest.mark.skipif(not KWS_DIR.is_dir(), reason="KWS model not present")
def test_kws_model_loads_and_releases():
    from core.audio import SherpaKeywordProvider

    provider = SherpaKeywordProvider(KWS_DIR)
    status = provider.load()
    assert status.available, status.details
    assert provider.create_stream() is not None
    provider.close()


@pytest.mark.skipif(not VAD_MODEL.is_file(), reason="Silero VAD model not present")
def test_vad_model_loads_and_releases():
    from core.audio import SherpaVadProvider

    provider = SherpaVadProvider(VAD_MODEL)
    status = provider.load()
    assert status.available, status.details
    provider.close()
