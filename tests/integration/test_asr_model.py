"""Streaming ASR against the real zipformer zh-14M model.

Model-free properties (``available``, unloaded feed) are asserted without
loading weights; the one property that needs weights -- recognising the bundled
wav into CJK text -- is skipped when the model is absent.

Evidence level: AUTO (real local model load + recognition of a pre-recorded
wav, no microphone). Transcribing a live microphone is REAL-MIC, not claimed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.audio import AsrResult, SherpaStreamingAsrProvider
from core.audio.base import ProviderUnavailable

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
WAV = MODEL_DIR / "test_wavs" / "0.wav"


def provider() -> SherpaStreamingAsrProvider:
    return SherpaStreamingAsrProvider(MODEL_DIR)


def test_available_is_false_for_a_missing_model():
    assert SherpaStreamingAsrProvider(ROOT / "models" / "does-not-exist").available is False


def test_feed_requires_a_loaded_provider():
    with pytest.raises(ProviderUnavailable):
        provider().feed(object(), np.zeros(1600, dtype=np.float32))


@pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="streaming asr model not present")
def test_the_bundled_wav_recognises_to_chinese_text():
    soundfile = pytest.importorskip("soundfile")
    samples, sample_rate = soundfile.read(WAV, dtype="float32")

    p = provider()
    assert p.load().available is True
    stream = p.create_stream()

    partial = ""
    for offset in range(0, len(samples), 1600):
        result = p.feed(stream, samples[offset : offset + 1600], sample_rate)
        partial = result.text
    combined = (p.finalize(stream) or partial).strip()
    p.close()

    # The bundled wav is real Chinese speech; the recognizer must produce some
    # CJK text (the exact transcription is a property of the model).
    assert combined, "streaming ASR produced no text"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in combined)

