"""TTS against the real 170 MB MeloTTS model.

Model-free properties (``available``, empty-text refusal) are asserted without
loading weights; the one property that needs weights -- synthesising a short
phrase into float32 audio -- is skipped when the model is absent.

Evidence level: AUTO (real local model load + synthesis, no playback device).
Playing the audio through a speaker and hearing it is REAL, and is not claimed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.audio import SherpaTtsProvider, TtsAudio
from core.audio.base import ProviderUnavailable

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "vits-melo-tts-zh_en"


def provider() -> SherpaTtsProvider:
    return SherpaTtsProvider(MODEL_DIR)


def test_available_is_false_for_a_missing_model():
    assert SherpaTtsProvider(ROOT / "models" / "does-not-exist").available is False


def test_synthesizing_empty_text_is_refused_before_loading():
    with pytest.raises(ProviderUnavailable):
        provider().synthesize("   ")


@pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="melo tts model not present")
def test_synthesize_produces_float32_audio():
    p = provider()
    status = p.load()
    assert status.available is True

    audio = p.synthesize("你好问问")
    assert isinstance(audio, TtsAudio)
    # The pinned vits-melo-tts-zh_en model outputs 44.1 kHz. The rate is the
    # model's own; a change here means the weights were swapped, which is a
    # regression worth catching.
    assert audio.sample_rate == 44100
    assert audio.samples.dtype == np.float32
    assert len(audio.samples) > 0
    # Four characters is roughly a second of speech; the exact length is a
    # property of the model, not a contract, so only a sane floor is asserted.
    assert len(audio.samples) / audio.sample_rate > 0.1
    assert audio.elapsed_ms >= 0
    p.close()


@pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="melo tts model not present")
def test_speak_delegates_to_the_injected_playback():
    class FakePlayback:
        def __init__(self) -> None:
            self.calls = []

        def play(self, samples, sample_rate, *, blocking=True):
            self.calls.append((len(samples), sample_rate, blocking))

    p = provider()
    p.load()
    playback = FakePlayback()
    p.playback = playback

    audio = p.speak("你好", blocking=False)

    assert playback.calls == [(len(audio.samples), audio.sample_rate, False)]
    p.close()

