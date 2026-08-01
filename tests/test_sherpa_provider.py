from pathlib import Path
import wave

import pytest

from core.providers import ProviderUnavailable, SherpaKeywordProvider, SherpaVadProvider


MODEL_DIR = Path("models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
VAD_MODEL = Path("models/silero_vad.onnx")


def test_missing_model_is_reported_without_importing_runtime(tmp_path):
    provider = SherpaKeywordProvider(tmp_path)
    status = provider.load()
    assert status.available is False
    assert "model files" in status.details["reason"]
    with pytest.raises(ProviderUnavailable, match="model files"):
        provider.create_stream()


@pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="isolated KWS model is not downloaded")
def test_real_model_loads_and_silence_has_no_hit():
    pytest.importorskip("sherpa_onnx")
    provider = SherpaKeywordProvider(MODEL_DIR)
    status = provider.load()
    assert status.available, status.details
    stream = provider.create_stream()
    hits = provider.feed(stream, [0.0] * 1600)
    assert hits == []
    provider.close()


@pytest.mark.skipif(not VAD_MODEL.is_file(), reason="Silero VAD model is not available")
def test_sherpa_vad_rejects_silence_and_detects_bundled_speech():
    pytest.importorskip("sherpa_onnx")
    numpy = pytest.importorskip("numpy")
    provider = SherpaVadProvider(VAD_MODEL)
    status = provider.load()
    assert status.available, status.details

    for _ in range(100):
        result = provider.feed(numpy.zeros(512, dtype=numpy.float32))
    result = provider.flush()
    assert result["speech"] is False
    assert result["segments"] == []

    provider.reset()
    wav_path = MODEL_DIR / "test_wavs" / "3.wav"
    with wave.open(str(wav_path), "rb") as wav:
        samples = numpy.frombuffer(wav.readframes(wav.getnframes()), dtype=numpy.int16)
    samples = samples.astype(numpy.float32) / 32768.0
    segments = []
    for offset in range(0, len(samples), 512):
        segments.extend(provider.feed(samples[offset : offset + 512])["segments"])
    segments.extend(provider.flush()["segments"])
    assert segments
    assert sum(segment["samples"] for segment in segments) > 16000
    provider.close()
