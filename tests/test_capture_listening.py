"""The listening phase: wake -> ASR -> recognised text (ADR 001).

The capture has two modes on one stream. Before a wake it feeds KWS; after an
**accepted** wake it feeds the streaming recognizer instead, and on an endpoint
it hands the final text to ``on_recognized`` and returns to KWS. A rejected wake
must not open the recognizer -- that would transcribe an unauthorised voice.

Evidence level: AUTO (stub KWS/verifier/ASR, no device, no model).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import AsrResult, ProviderStatus, ProviderUnavailable, SounddeviceWakeCapture
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


class TrackingKws(StubKws):
    def __init__(
        self, hits=None, *, fail_load=False, fail_create=False, fail_feed=False, fail_close=False
    ) -> None:
        super().__init__(hits)
        self.fail_load = fail_load
        self.fail_create = fail_create
        self.fail_feed = fail_feed
        self.fail_close = fail_close
        self.loads = 0
        self.streams = 0
        self.closes = 0

    def load(self):
        self.loads += 1
        if self.fail_load:
            return ProviderStatus(False, "stub", {"reason": "kws unavailable"})
        return super().load()

    def create_stream(self):
        self.streams += 1
        if self.fail_create:
            raise RuntimeError("kws stream failed")
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        if self.fail_feed:
            self.fail_feed = False
            raise RuntimeError("private kws detail")
        return super().feed(stream, samples, sample_rate)

    def close(self):
        self.closes += 1
        super().close()
        if self.fail_close:
            raise RuntimeError("kws close failed")


class TrackingAsr(StubAsr):
    def __init__(
        self,
        *args,
        fail_load=False,
        fail_feed=False,
        fail_finalize=False,
        fail_reset=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fail_load = fail_load
        self.fail_feed = fail_feed
        self.fail_finalize = fail_finalize
        self.fail_reset = fail_reset
        self.loads = 0
        self.closes = 0

    def load(self):
        self.loads += 1
        if self.fail_load:
            return ProviderStatus(False, "stub", {"reason": "asr unavailable"})
        return super().load()

    def feed(self, stream, samples, sample_rate=16000):
        if self.fail_feed:
            raise RuntimeError("private asr feed detail")
        return super().feed(stream, samples, sample_rate)

    def finalize(self, stream):
        if self.fail_finalize:
            raise ValueError("private transcript detail")
        return super().finalize(stream)

    def reset(self, stream):
        super().reset(stream)
        if self.fail_reset:
            raise RuntimeError("private asr reset detail")

    def close(self):
        self.closes += 1
        super().close()


class FakeInputStream:
    def __init__(self, *, fail_start=False, fail_stop=False, fail_close=False, **kwargs) -> None:
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.kwargs = kwargs
        self.starts = 0
        self.stops = 0
        self.closes = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("device start failed")

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("device stop failed")

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("device close failed")


def install_stream_factory(monkeypatch, streams):
    pending = list(streams)

    def factory(**kwargs):
        stream = pending.pop(0)
        stream.kwargs = kwargs
        return stream

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=factory))


def test_a_failed_device_start_rolls_back_and_can_retry(monkeypatch):
    first = FakeInputStream(fail_start=True)
    second = FakeInputStream()
    install_stream_factory(monkeypatch, [first, second])
    kws = TrackingKws()
    asr = TrackingAsr()
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        require_verification=False,
        asr_provider=asr,
        on_recognized=lambda _text: None,
    )

    with pytest.raises(RuntimeError, match="device start failed"):
        capture.start()

    assert capture._stream is None
    assert capture._inference_stream is None
    assert capture._asr_stream is None
    assert capture._listening is False
    assert first.stops == 1
    assert first.closes == 1
    assert kws.closes == 1
    assert asr.closes == 1

    capture.start()
    assert capture._stream is second
    assert capture._inference_stream is not None
    assert second.starts == 1
    assert kws.loads == 2
    assert asr.loads == 2


@pytest.mark.parametrize("failure", ["kws-load", "kws-stream", "asr-load"])
def test_provider_start_failures_leave_no_partial_state(monkeypatch, failure):
    stream = FakeInputStream()
    install_stream_factory(monkeypatch, [stream])
    kws = TrackingKws(fail_load=failure == "kws-load", fail_create=failure == "kws-stream")
    asr = TrackingAsr(fail_load=failure == "asr-load")
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        require_verification=False,
        asr_provider=asr,
        on_recognized=lambda _text: None,
    )

    with pytest.raises((ProviderUnavailable, RuntimeError)):
        capture.start()

    assert capture._stream is None
    assert capture._inference_stream is None
    assert capture._asr_stream is None
    assert capture._listening is False
    assert kws.closes == 1
    assert asr.closes == (1 if failure == "asr-load" else 0)
    assert stream.starts == 0


def test_kws_callback_failure_is_isolated_and_future_audio_can_recover():
    woke = []
    kws = TrackingKws(["你好问问"], fail_feed=True)
    capture = SounddeviceWakeCapture(kws, lambda *args: woke.append(args), require_verification=False)
    capture._keyword_provider_loaded = True
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == "RuntimeError"
    assert "private kws detail" not in capture.last_callback_error
    assert capture._inference_stream is not None

    capture._callback(block(), 160, None, None)
    assert woke == [("你好问问", None)]


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [("feed", "RuntimeError"), ("finalize", "ValueError"), ("reset", "RuntimeError")],
)
def test_asr_callback_failures_return_to_kws_without_leaking(failure, error_type):
    asr = TrackingAsr(
        fail_feed=failure == "feed",
        fail_finalize=failure == "finalize",
        fail_reset=failure == "reset",
    )
    recognized = []
    capture, kws = build(asr=asr, recognized=recognized)
    capture._keyword_provider_loaded = True

    capture._callback(block(), 160, None, None)
    assert capture._listening is True

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == error_type
    assert capture._listening is False
    assert capture._asr_stream is None
    assert asr.resets == 1
    assert recognized == []
    kws.hits = ["你好问问"]
    capture._callback(block(), 160, None, None)
    assert capture._listening is True



def test_on_wake_failure_is_isolated_from_the_audio_thread():
    kws = TrackingKws(["你好问问"])

    def fail_callback(_keyword, _score):
        raise OSError("wake consumer leaked a path")

    capture = SounddeviceWakeCapture(kws, fail_callback, require_verification=False)
    capture._keyword_provider_loaded = True
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == "OSError"
    assert capture._listening is False
    assert capture._inference_stream is not None


def test_on_reject_failure_is_isolated_and_never_becomes_a_wake():
    woke = []
    kws = TrackingKws(["你好问问"])

    def fail_callback(*_args):
        raise PermissionError("rejection consumer detail")

    capture = SounddeviceWakeCapture(
        kws,
        lambda *args: woke.append(args),
        verifier=StubVerifier(accepted=False),
        on_reject=fail_callback,
    )
    capture._keyword_provider_loaded = True
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert woke == []
    assert capture.callback_errors == 1
    assert capture.last_callback_error == "PermissionError"
    assert capture._listening is False


def test_on_recognized_failure_is_isolated_after_asr_state_is_cleared():
    asr = TrackingAsr("敏感识别文本")

    def fail_callback(_text):
        raise LookupError("consumer included sensitive content")

    capture, _kws = build(asr=asr, recognized=[])
    capture.on_recognized = fail_callback
    capture._keyword_provider_loaded = True
    capture._callback(block(), 160, None, None)

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == "LookupError"
    assert capture._listening is False
    assert capture._asr_stream is None
    assert asr.resets == 1


def test_stop_is_best_effort_and_second_stop_has_no_side_effects():
    kws = TrackingKws(fail_close=True)
    asr = TrackingAsr()
    stream = FakeInputStream(fail_stop=True, fail_close=True)
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        require_verification=False,
        asr_provider=asr,
        on_recognized=lambda _text: None,
    )
    capture._stream = stream
    capture._keyword_provider_loaded = True
    capture._asr_provider_loaded = True
    capture._inference_stream = kws.create_stream()
    capture._asr_stream = asr.create_stream()
    capture._listening = True
    capture._ring.write(np.ones(10, dtype="float32"))

    capture.stop()

    assert capture._stream is None
    assert capture._inference_stream is None
    assert capture._asr_stream is None
    assert capture._listening is False
    assert len(capture._ring) == 0
    assert stream.stops == 1
    assert stream.closes == 1
    assert asr.resets == 1
    assert kws.closes == 1
    assert asr.closes == 1

    capture.stop()
    assert stream.stops == 1
    assert stream.closes == 1
    assert asr.resets == 1
    assert kws.closes == 1
    assert asr.closes == 1
