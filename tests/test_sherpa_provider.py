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


# --------------------------------------------- 束宽（2026-09-01 的唤醒率修正）


def test_the_beam_width_actually_reaches_the_spotter(monkeypatch):
    """``max_active_paths`` 必须真的传进 sherpa。

    这条钉的是本项目反复踩到的那一类缺陷：**一个能改、能存、不生效的配置项**
    （`tts.instruction` 漏传过一次，`config/keywords.txt` 的条数写死在页面里过一次）。
    束宽尤其危险，因为漏传它不会报错 —— 只是在噪声里少命中，而每一层都报告自己健康。
    """
    import sys
    import types

    seen: dict[str, object] = {}

    class FakeSpotter:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    fake = types.SimpleNamespace(KeywordSpotter=FakeSpotter)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    provider = SherpaKeywordProvider(
        MODEL_DIR, max_active_paths=16, keywords_score=2.0, num_trailing_blanks=2
    )
    if not provider.available:
        pytest.skip("isolated KWS model is not downloaded")
    assert provider.load().available
    assert seen["max_active_paths"] == 16
    assert seen["keywords_score"] == 2.0
    assert seen["num_trailing_blanks"] == 2


def test_the_shipped_default_is_wider_than_sherpas():
    """sherpa-onnx 的默认束宽是 4，实测它在 0 dB SNR 上只剩 2/5。

    这一条不是在测 sherpa，是在防止「有人把它改回默认值以为那是保守选择」——
    在这个模型上 beam 4 才是有代价的那一个，而代价看不见（每块耗时几乎一样）。
    """
    from core.audio.config import load_voice_config
    from core.audio.kws import DEFAULT_MAX_ACTIVE_PATHS

    assert DEFAULT_MAX_ACTIVE_PATHS >= 8
    assert int(load_voice_config()["wake.max_active_paths"]) >= 8


@pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="isolated KWS model is not downloaded")
def test_a_wider_beam_does_not_make_silence_a_hit():
    """束宽是召回参数，不该把静音变成命中 —— 加宽之后这条负样本仍然必须为空。"""
    pytest.importorskip("sherpa_onnx")
    provider = SherpaKeywordProvider(MODEL_DIR, max_active_paths=32)
    assert provider.load().available
    stream = provider.create_stream()
    for _ in range(20):
        assert provider.feed(stream, [0.0] * 1600) == []
    provider.close()


# --------------------------------------- 端点检测的静音时长（2026-09-01 的延迟测量）


def test_the_endpoint_rules_actually_reach_the_recognizer(monkeypatch):
    """三条端点规则必须真的传进 sherpa。

    和束宽同一类风险：它们此前写死在 ``load()`` 里，于是「说完之后还要等多久」既量不到
    也调不动 —— 而那 1.2 秒是每一轮延迟里唯一 100% 在我们手里的一段。
    """
    import sys
    import types

    seen: dict[str, object] = {}

    class FakeRecognizer:
        @staticmethod
        def from_transducer(**kwargs):
            seen.update(kwargs)
            return object()

    fake = types.SimpleNamespace(OnlineRecognizer=FakeRecognizer)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    from core.audio.asr import SherpaStreamingAsrProvider
    from core.audio.config import load_voice_config

    provider = SherpaStreamingAsrProvider(
        load_voice_config()["asr_dir"], rule2_silence=0.8, rule1_silence=2.0
    )
    if not provider.available:
        pytest.skip("streaming ASR model is not unpacked")
    assert provider.load().available
    assert seen["rule2_min_trailing_silence"] == 0.8
    assert seen["rule1_min_trailing_silence"] == 2.0
    assert seen["rule3_min_utterance_length"] == 20.0


def test_the_shipped_endpoint_wait_is_not_shortened_without_evidence():
    """``rule2`` 默认 1.2 秒。**这一条防的是「顺手把它调小省延迟」。**

    实测（`.vox-ref/endpoint_probe.py`，本人真录音走生产那条两段路径）：这位说话人
    自己的**短语间停顿是 1.0–1.1 秒**。rule2 = 1.2 时那些停顿不切句；降到 1.0 就切 ——
    症状是后半句落进下一轮或者干脆丢掉，而使用者看到的是「它没听全」。

    换句话说这 1.2 秒不是余量，是按停顿长度定的。要改它必须先量新的停顿分布，
    不能因为「省 400 毫秒」就动。
    """
    from core.audio.asr import SherpaStreamingAsrProvider

    provider = SherpaStreamingAsrProvider("nonexistent")
    assert provider.rule2_silence >= 1.2
    assert provider.rule1_silence >= 2.4
