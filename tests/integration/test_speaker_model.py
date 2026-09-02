"""Speaker verification against the real 37 MB model.

Skipped when the model is absent, exactly like the KWS and VAD model tests --
``tests/test_speaker.py`` deliberately covers the model-free properties, and this
file covers the one property that needs weights: whether the thing actually
tells voices apart.

Evidence level: **AUTO**. The audio is the seven recordings bundled with the KWS
model, which are real human speech but recorded, not live. That is enough to
calibrate a threshold and enough to prove discrimination exists. It is *not*
REAL-MIC and must not be reported as such -- own-voice pass rate, other-person
rejection and replay behaviour all still need a live microphone and a second
person (release blocker #8).
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.audio import SpeakerVerificationProvider
from core.audio.speaker import DEFAULT_MODEL_NAME

ROOT = Path(__file__).resolve().parents[2]
# 跟着出厂默认走，不写死文件名 —— 2026-08-29 换过一次模型（ERes2Net → CAM++），
# 写死的那一版会在换型当天变红，而它想测的是「当前默认那个模型有判别力」。
MODEL = ROOT / "models" / DEFAULT_MODEL_NAME
WAVS = ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01" / "test_wavs"

# Grouping derived from the measured cosine matrix, not from any label shipped
# with the wavs: within a group scores run 0.74-0.87, across groups -0.13-0.37.
SAME_SPEAKER_GROUPS = [(0, 1, 2), (3, 4), (5, 6)]

pytestmark = pytest.mark.skipif(
    not MODEL.is_file() or not WAVS.is_dir(),
    reason="speaker model or bundled wavs not present",
)


def _read(index: int):
    soundfile = pytest.importorskip("soundfile")
    samples, sample_rate = soundfile.read(WAVS / f"{index}.wav", dtype="float32")
    return samples, sample_rate


@pytest.fixture()
def provider(tmp_path):
    provider = SpeakerVerificationProvider(store_path=tmp_path / "voiceprints.json")
    status = provider.load()
    assert status.available, status.details
    yield provider
    provider.close()


def test_the_model_loads_and_reports_its_dimension(provider):
    # 维度不写死:CAM++ 是 192,ERes2Net 是 512。断言的是「报得出一个正的维度」,
    # 那才是这条测试想说的事(模型真加载起来了)。
    assert provider.dim > 0
    assert provider.describe()["available"] is True


def test_embeddings_separate_speakers(provider):
    """The score matrix must be separable, or no threshold can work."""
    import numpy

    vectors = {}
    for index in range(7):
        samples, sample_rate = _read(index)
        vectors[index] = numpy.asarray(provider.embed(samples, sample_rate))

    def cosine(a, b):
        return float(a @ b / (numpy.linalg.norm(a) * numpy.linalg.norm(b)))

    same = [
        cosine(vectors[i], vectors[j])
        for group in SAME_SPEAKER_GROUPS
        for i, j in itertools.combinations(group, 2)
    ]
    lookup = {i: n for n, group in enumerate(SAME_SPEAKER_GROUPS) for i in group}
    different = [
        cosine(vectors[i], vectors[j])
        for i, j in itertools.combinations(range(7), 2)
        if lookup[i] != lookup[j]
    ]
    assert min(same) > max(different), (
        f"same-speaker min {min(same):.3f} must exceed cross-speaker max {max(different):.3f}"
    )
    # The shipped default sits inside the gap; this is the AUTO evidence for it.
    assert max(different) < 0.5 < min(same)


def test_enrolled_speaker_is_admitted_and_others_are_refused(provider):
    samples, sample_rate = _read(0)
    result = provider.enroll("owner", [samples], sample_rate=sample_rate)
    assert result.dim == provider.dim

    for index in (1, 2):
        accepted = provider.verify(_read(index)[0], sample_rate=sample_rate)
        assert accepted.accepted, f"wav {index} is the same speaker: {accepted}"
        assert accepted.speaker == "owner"

    for index in (3, 4, 5, 6):
        refused = provider.verify(_read(index)[0], sample_rate=sample_rate)
        assert not refused.accepted, f"wav {index} is a different speaker: {refused}"
        assert refused.speaker is None
        # 原因是中文，而且带上「差多少」：0.448 和 -0.022 是完全不同的两件事
        # （条件不够好 vs 不是这个人），只写 below threshold 把两者混成一句话。
        assert "相似度" in refused.reason and "阈值" in refused.reason


def test_appending_samples_keeps_the_earlier_enrollment(provider):
    first, sample_rate = _read(5)
    provider.enroll("owner", [first], sample_rate=sample_rate)
    provider.enroll("owner", [_read(6)[0]], sample_rate=sample_rate)
    assert provider.describe()["samples_per_speaker"]["owner"] == 2
    assert provider.verify(first, sample_rate=sample_rate).accepted


def test_synthetic_tones_cannot_stand_in_for_speech(provider):
    """A negative result worth keeping: synthetic audio proves nothing here.

    Two harmonic stacks an octave apart -- about as different as two synthetic
    signals get -- still score far above the threshold, because the model was
    trained on speech and reads them as the same non-voice. Any future test that
    tries to check discrimination with generated tones would pass vacuously.
    """
    import numpy

    def tone(f0: float, seed: int, samples: int = 32000):
        t = numpy.arange(samples) / 16000.0
        rng = numpy.random.default_rng(seed)
        stack = sum(numpy.sin(2 * numpy.pi * f0 * k * t) / k for k in range(1, 8))
        return (0.3 * stack + 0.01 * rng.standard_normal(samples)).astype("float32")

    provider.enroll("synthetic", [tone(120, seed=1)])
    impostor = provider.verify(tone(240, seed=3))
    assert impostor.accepted, (
        "if this ever starts failing, synthetic audio became usable for "
        "discrimination tests -- update the docs before relying on it"
    )
