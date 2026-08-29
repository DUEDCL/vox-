"""``open_voice_stack``: what degrades, what refuses, and what it reports.

The load-bearing test in this file is
``test_a_missing_voiceprint_model_does_not_turn_the_gate_off``. Everything else
here is about honest reporting; that one is about the difference between a
degraded mode and a different product.

Nothing loads a model: the providers are lazy, so pointing them at an empty
directory exercises the whole assembly at AUTO level with no ``models/`` present.
"""

from __future__ import annotations

import pytest

from core.audio.config import load_voice_config
from vox_plugin.voice_stack import VoiceStack, open_voice_stack


@pytest.fixture
def nowhere(monkeypatch, tmp_path):
    """Point all four model paths at directories that do not exist."""
    monkeypatch.setenv("VOX_KWS_MODEL_DIR", str(tmp_path / "kws"))
    monkeypatch.setenv("VOX_ASR_MODEL_DIR", str(tmp_path / "asr"))
    monkeypatch.setenv("VOX_TTS_MODEL_DIR", str(tmp_path / "tts"))
    monkeypatch.setenv("VOX_VAD_MODEL", str(tmp_path / "vad.onnx"))
    monkeypatch.setenv("VOX_SPEAKER_ENROLLMENT", str(tmp_path / "voiceprints.json"))
    return load_voice_config(tmp_path / "absent.toml")


def test_a_missing_asr_model_degrades_to_wake_only(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.asr is None
    assert stack.capture.asr_provider is None
    assert any("asr model not found" in w for w in stack.warnings)
    stack.close()


def test_a_missing_tts_model_degrades_to_silence(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.tts is None
    assert any("answers stay silent" in w for w in stack.warnings)
    stack.close()


def test_a_missing_voiceprint_model_does_not_turn_the_gate_off(nowhere):
    """A stack built with the gate on keeps it on even with nothing to verify against.

    ``capture.start()`` is what refuses, and that refusal is the product working.
    Silently flipping ``require_verification`` to False here would turn "nobody is
    enrolled" into "anyone may wake it" -- the exact substitution the fail-closed
    design exists to prevent.
    """
    stack = open_voice_stack(nowhere, require_verification=True)
    assert stack.capture.require_verification is True
    assert stack.gate_off is False
    stack.close()


def test_turning_the_gate_off_is_always_reported(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.gate_off is True
    assert stack.capture.verifier is None
    assert any("gate is OFF" in w for w in stack.warnings)
    stack.close()


def test_the_gate_defaults_to_the_speaker_config(nowhere):
    """With no explicit argument the shipped ``require_verification = true`` wins."""
    stack = open_voice_stack(nowhere)
    assert stack.capture.require_verification is True
    stack.close()


def test_disabling_asr_and_tts_by_config(nowhere):
    nowhere["asr.enabled"] = False
    nowhere["tts.enabled"] = False
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.asr is None and stack.tts is None
    # A disabled provider is not a missing one: no "not found" warning for it.
    assert not any("asr model not found" in w for w in stack.warnings)
    stack.close()


def test_capture_receives_the_configured_audio_parameters(nowhere):
    nowhere["input.sample_rate"] = 16000
    nowhere["input.blocksize"] = 800
    nowhere["input.device"] = "7"
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.capture.sample_rate == 16000
    assert stack.capture.blocksize == 800
    assert stack.capture.device == 7
    stack.close()


def test_an_explicit_device_beats_the_config(nowhere):
    nowhere["input.device"] = "7"
    stack = open_voice_stack(nowhere, require_verification=False, device="USB")
    assert stack.capture.device == "USB"
    stack.close()


def test_wake_threshold_and_threads_reach_the_provider(nowhere):
    nowhere["wake.keywords_threshold"] = 0.4
    nowhere["wake.num_threads"] = 3
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.kws.keywords_threshold == 0.4
    assert stack.kws.num_threads == 3
    stack.close()


def test_readiness_reports_one_row_per_thing_that_can_be_missing(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    rows = stack.readiness()
    assert [row["item"] for row in rows] == ["wake", "asr", "tts", "speaker"]
    for row in rows:
        assert set(row) == {"item", "ready", "detail", "hint"}
    stack.close()


def test_readiness_hints_say_what_to_do_about_each_gap(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    hints = {row["item"]: row["hint"] for row in stack.readiness()}
    assert "VOX_KWS_MODEL_DIR" in hints["wake"]
    assert hints["speaker"]  # the escape hatch is a gap worth naming, not a pass
    stack.close()


def test_readiness_marks_a_disabled_provider_as_not_blocking(nowhere):
    nowhere["tts.enabled"] = False
    stack = open_voice_stack(nowhere, require_verification=False)
    tts_row = next(row for row in stack.readiness() if row["item"] == "tts")
    assert tts_row["ready"] is True
    assert tts_row["hint"] == ""
    stack.close()


def test_readiness_never_reports_a_voiceprint_vector(nowhere):
    """``describe()`` is the only sanctioned view of enrollment data."""
    stack = open_voice_stack(nowhere)
    rows = repr(stack.readiness())
    assert "embedding" not in rows and "vector" not in rows
    stack.close()


def test_close_is_idempotent(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    stack.close()
    stack.close()


def test_close_survives_a_provider_that_raises(nowhere):
    class Angry:
        def close(self) -> None:
            raise RuntimeError("native teardown failed")

    stack = VoiceStack(config=nowhere, tts=Angry(), kws=Angry())
    stack.close()  # best-effort: neither raise escapes


def test_a_stack_holds_no_speaker_identity(nowhere):
    """Identity lives on the plugin, set by the gate. A second holder would be a
    second source of truth for the one fact ``shell.run`` demands."""
    stack = open_voice_stack(nowhere, require_verification=False)
    assert not hasattr(stack, "speaker")
    assert not hasattr(stack, "verified_speaker")
    stack.close()
