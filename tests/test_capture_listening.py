"""The listening phase: wake -> ASR -> recognised text (ADR 001).

The capture has two modes on one stream. Before a wake it feeds KWS; after an
**accepted** wake it feeds the streaming recognizer instead, and on an endpoint
it hands the final text to ``on_recognized`` and returns to KWS. A rejected wake
must not open the recognizer -- that would transcribe an unauthorised voice.

Evidence level: AUTO (stub KWS/verifier/ASR, no device, no model).
"""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import AsrResult, ProviderStatus, SounddeviceWakeCapture
from core.audio.speaker import VerificationResult


class StubKws:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])
        self.closed = False

    def load(self):
        return ProviderStatus(True, "stub", {"engine": "stub"})

    def create_stream(self):
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, samples, sample_rate
        hits, self.hits = self.hits, []
        return [(keyword, None) for keyword in hits]

    def close(self):
        self.closed = True


class StubAsr:
    """Endpoints after ``endpoint_after`` feeds, then returns ``final_text``."""

    def __init__(self, final_text="读一下 README", endpoint_after=1) -> None:
        self.final_text = final_text
        self.endpoint_after = endpoint_after
        self.fed = 0
        self.streams = 0
        self.resets = 0
        self.closed = False

    def load(self):
        return ProviderStatus(True, "stub", {"engine": "stub"})

    def create_stream(self):
        self.streams += 1
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, samples, sample_rate
        self.fed += 1
        return AsrResult(text="部分", is_endpoint=self.fed >= self.endpoint_after)

    def finalize(self, stream):
        del stream
        return self.final_text

    def reset(self, stream):
        del stream
        self.resets += 1

    def close(self):
        self.closed = True


class StubVerifier:
    def __init__(self, *, accepted=True) -> None:
        self.result = VerificationResult(
            accepted, "owner" if accepted else None, 0.91, "match" if accepted else "below"
        )
        self.speakers = ["owner"]

    def load(self):
        return ProviderStatus(True, "stub", {"dim": 192})

    def verify(self, samples, *, sample_rate=16000):
        del samples, sample_rate
        return self.result


def block(samples: int = 160, value: float = 0.2) -> np.ndarray:
    return np.full((samples, 1), value, dtype="float32")


def build(*, asr=None, recognized=None, verifier=None, require_verification=False):
    kws = StubKws(["你好问问"])
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        blocksize=160,
        require_verification=require_verification,
        verifier=verifier,
        asr_provider=asr,
        on_recognized=(recognized.append if recognized is not None else None),
    )
    capture._inference_stream = kws.create_stream()
    return capture, kws


def test_wake_only_mode_is_unchanged_without_an_asr_provider():
    woke = []
    capture, _kws = build()
    capture.on_wake = lambda keyword, score: woke.append(keyword)

    capture._callback(block(), 160, None, None)

    assert woke == ["你好问问"]
    assert capture._listening is False


def test_an_accepted_wake_switches_to_asr_and_delivers_the_text():
    asr = StubAsr("读一下 README")
    recognized: list[str] = []
    capture, _kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)
    assert capture._listening is True, "an accepted wake must open the recognizer"
    assert asr.streams == 1

    capture._callback(block(), 160, None, None)

    assert recognized == ["读一下 README"]
    assert capture._listening is False, "an endpoint returns the capture to KWS"
    assert asr.resets == 1


def test_audio_during_listening_never_reaches_kws():
    asr = StubAsr(endpoint_after=99)
    recognized: list[str] = []
    capture, kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)
    kws.hits = ["你好问问"]
    capture._callback(block(), 160, None, None)

    # Still queued for KWS, because the block went to the recognizer instead.
    assert kws.hits == ["你好问问"]
    # One feed, not two: the block the wake fired on opens the recognizer but is
    # not itself transcribed, so the wake word does not land in the request.
    assert asr.fed == 1
    assert recognized == []


def test_a_rejected_wake_never_opens_the_recognizer():
    asr = StubAsr()
    recognized: list[str] = []
    capture, _kws = build(
        asr=asr,
        recognized=recognized,
        verifier=StubVerifier(accepted=False),
        require_verification=True,
    )
    capture.on_reject = lambda *a: None

    capture._callback(block(), 160, None, None)

    assert capture._listening is False
    assert asr.streams == 0, "transcribing an unauthorised voice is the failure"
    assert recognized == []


def test_an_empty_transcription_is_not_delivered():
    asr = StubAsr(final_text="   ")
    recognized: list[str] = []
    capture, _kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)
    capture._callback(block(), 160, None, None)

    assert recognized == [], "silence must not start a turn"
    assert capture._listening is False

