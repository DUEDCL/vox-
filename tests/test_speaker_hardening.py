"""Gate hardening: quality floors, brute-force cooldown, multi-window vote.

These run without the speaker model on purpose -- the cheap input-side gates
are placed *before* the model check in ``verify``, so their behaviour is fully
testable here. The hardening is heuristics against junk inputs and brute-force
attempts; it does NOT claim replay-attack detection (ADR 002 limitation).

Evidence level: AUTO.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.audio import SpeakerVerificationProvider
from core.audio.speaker import load_speaker_config


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _provider(tmp_path, **kwargs) -> SpeakerVerificationProvider:
    return SpeakerVerificationProvider(
        tmp_path / "absent-model.onnx",
        store_path=tmp_path / "voiceprints.json",
        **kwargs,
    )


# -- quality gates (run before the model, hence testable model-free) -----------


def test_silence_is_rejected_before_the_model_check(tmp_path):
    result = _provider(tmp_path).verify([0.0] * 16000)

    assert result.accepted is False
    assert "too quiet" in result.reason


def test_clipped_audio_is_rejected(tmp_path):
    loud = np.array([1.0, -1.0] * 8000, dtype=np.float32)
    result = _provider(tmp_path).verify(loud)

    assert result.accepted is False
    assert "clipped" in result.reason


def test_healthy_amplitude_still_reaches_the_model_gate(tmp_path):
    speech_like = (np.sin(np.linspace(0, 400, 16000)) * 0.2).astype(np.float32)
    result = _provider(tmp_path).verify(speech_like)

    assert result.accepted is False
    assert "not found" in result.reason  # quality passed; model gate answered


def test_empty_buffer_is_quality_rejected(tmp_path):
    assert "empty" in _provider(tmp_path).verify(np.zeros(0, dtype=np.float32)).reason


# -- brute-force cooldown -------------------------------------------------------


def test_repeated_junk_locks_the_gate_then_it_expires(tmp_path):
    clock = FakeClock(start=100.0)
    provider = _provider(
        tmp_path,
        max_consecutive_rejections=2,
        cooldown_s=30.0,
        clock=clock,
    )
    silence = [0.0] * 16000

    first = provider.verify(silence)
    second = provider.verify(silence)
    assert first.accepted is False and second.accepted is False
    clock.now += 1.0

    third = provider.verify(silence)  # even good audio is refused now
    assert third.accepted is False
    assert "cooling down" in third.reason

    clock.now += 31.0  # past the cooldown
    expired = provider.verify(silence)
    assert "cooling down" not in expired.reason


def test_streak_window_older_than_a_cooldown_starts_fresh(tmp_path):
    clock = FakeClock(start=100.0)
    provider = _provider(
        tmp_path,
        max_consecutive_rejections=2,
        cooldown_s=30.0,
        clock=clock,
    )
    silence = [0.0] * 16000

    provider.verify(silence)
    clock.now += 91.0  # longer than the streak window (max(cooldown, 60))
    provider.verify(silence)  # old pressure forgotten; this is strike one

    assert provider.gate_stats["consecutive_rejections"] == 1


def test_gate_stats_count_each_path(tmp_path):
    clock = FakeClock()
    provider = _provider(tmp_path, max_consecutive_rejections=99, clock=clock)
    silence = [0.0] * 16000

    provider.verify(silence)
    provider.verify((np.ones(16000, dtype=np.float32)))  # clipped

    stats = provider.gate_stats
    assert stats["rejected_quality"] == 2
    assert stats["accepted"] == 0
    assert stats["consecutive_rejections"] == 2


# -- multi-window verification (scripted embeddings, no model) ------------------


def _speech_like(samples: int) -> np.ndarray:
    """Audible buffer that clears the RMS floor and the clip ceiling."""
    return (np.sin(np.linspace(0, samples * 0.05, samples)) * 0.2).astype(np.float32)


class ScriptedManager:
    def __init__(self, speakers) -> None:
        self.all_speakers = list(speakers)
        self.num_speakers = len(self.all_speakers)

    def score(self, name, vector):
        # The scripted embedding carries the score plus an index of which
        # enrolled speaker this window matched; other names score below zero.
        matched = self.all_speakers[int(vector[1]) % len(self.all_speakers)]
        if name != matched:
            return -1.0
        return float(vector[0])


class ScriptedSpeaker(SpeakerVerificationProvider):
    """Real gate flow, scripted embeddings: no model, no enrollment store."""

    def __init__(self, scores, *, tmp_path, speakers=("due",), **kwargs):
        kwargs.setdefault("clock", FakeClock())
        super().__init__(
            tmp_path / "absent-model.onnx",
            store_path=tmp_path / "voiceprints.json",
            **kwargs,
        )
        self._scores = [list(entry) for entry in scores]
        self._extractor = object()  # _require() sees a loaded engine
        self._manager = ScriptedManager(speakers)

    def embed(self, samples, sample_rate=16000):
        entry = self._scores.pop(0)
        score = entry[0]
        speaker_index = entry[1] if len(entry) > 1 else 0
        return [float(score), float(speaker_index)]


def test_multi_window_requires_unanimous_match(tmp_path):
    tts = ScriptedSpeaker([[0.9], [0.85]], tmp_path=tmp_path, verify_windows=2)

    result = tts.verify(_speech_like(4 * 16000))

    assert result.accepted is True
    assert result.reason == "all windows match"
    assert result.score == pytest.approx(0.9)


def test_one_weak_window_rejects_the_whole_attempt(tmp_path):
    tts = ScriptedSpeaker([[0.9], [0.3]], tmp_path=tmp_path, verify_windows=2)

    result = tts.verify(_speech_like(4 * 16000))

    assert result.accepted is False
    assert "window 1 below threshold" in result.reason


def test_disagreeing_windows_reject(tmp_path):
    tts = ScriptedSpeaker(
        [[0.9, 0], [0.9, 1]],
        tmp_path=tmp_path,
        verify_windows=2,
        speakers=("due", "intruder"),
    )

    result = tts.verify(_speech_like(4 * 16000))

    assert result.accepted is False
    assert "disagree" in result.reason


def test_short_buffer_cannot_run_multi_window(tmp_path):
    tts = ScriptedSpeaker([], tmp_path=tmp_path, verify_windows=2)

    result = tts.verify(_speech_like(8000))  # < 2 x 0.6 s

    assert result.accepted is False
    assert "not enough audio" in result.reason


def test_multi_window_accept_resets_the_streak(tmp_path):
    tts = ScriptedSpeaker([[0.9], [0.9]], tmp_path=tmp_path, verify_windows=2)
    tts._rejection_streak = 4
    tts.gate_stats["consecutive_rejections"] = 4

    tts.verify(_speech_like(4 * 16000))

    assert tts.gate_stats["consecutive_rejections"] == 0


# -- configuration and audit surface --------------------------------------------


def test_hardening_keys_flow_through_from_config(tmp_path):
    config = tmp_path / "speaker.toml"
    config.write_text(
        "[speaker]\n"
        "min_rms = 0.02\n"
        "verify_windows = 3\n"
        "cooldown_s = 12\n",
        encoding="utf-8",
    )
    merged = load_speaker_config(config)

    assert merged["min_rms"] == 0.02
    assert merged["verify_windows"] == 3
    assert merged["cooldown_s"] == 12
    # Defaults stay secure when a key is absent.
    assert merged["max_clip_ratio"] == 0.05
    assert merged["max_consecutive_rejections"] == 5


def test_describe_reports_gate_config_and_counts_without_vectors(tmp_path):
    import json
    provider = _provider(tmp_path, verify_windows=2, cooldown_s=15.0)
    provider.store.save({"due": [[0.1, 0.2], [0.3, 0.4]]}, dim=2)
    provider.verify([0.0] * 16000)  # one quality rejection for the counter

    described = provider.describe()

    assert described["gate"]["verify_windows"] == 2
    assert described["gate"]["cooldown_s"] == 15.0
    assert described["gate_stats"]["rejected_quality"] == 1
    serialised = json.dumps(described)
    assert "0.1" not in serialised.replace("0.05", "").replace("0.002", "")
    assert "[0.1, 0.2]" not in serialised
