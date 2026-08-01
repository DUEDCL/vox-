"""Realtime microphone capture with the speaker gate wired in.

This is the only module in the project that opens a capture device, and the only
one that decides whether a wake-word hit is allowed to reach the platform. Both
facts are deliberate: red line 1 (no audio persisted or uploaded) and the
fail-closed speaker gate each need exactly one enforcement point.

Gate placement follows ADR 002: verification happens at the KWS hit, against the
ring buffer, *before* anything observable happens. Verifying the first recognised
sentence instead would mean an unauthorised speaker has already seen the orb.
"""

from __future__ import annotations

import importlib
from typing import Any

from .base import ProviderUnavailable
from .kws import SherpaKeywordProvider
from .ring import AudioRingBuffer


class SounddeviceWakeCapture:
    """Optional realtime microphone adapter around ``sounddevice.InputStream``.

    ``require_verification`` defaults to ``True`` and is fail-closed: with it on,
    ``start()`` refuses to open the device unless a usable verifier with at least
    one enrolled speaker is attached. A gate that silently degrades to "anyone may
    wake it" is worse than no gate, because it advertises protection it is not
    providing.
    """

    def __init__(
        self,
        keyword_provider: SherpaKeywordProvider,
        on_wake: Any,
        *,
        sample_rate: int = 16000,
        blocksize: int = 1600,
        device: int | str | None = None,
        speech_gate: Any = None,
        verifier: Any = None,
        on_reject: Any = None,
        require_verification: bool = True,
        buffer_seconds: float = 3.0,
        verify_seconds: float = 1.5,
    ) -> None:
        self.keyword_provider = keyword_provider
        self.on_wake = on_wake
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = device
        self.speech_gate = speech_gate
        self.verifier = verifier
        self.on_reject = on_reject
        self.require_verification = require_verification
        self.verify_seconds = verify_seconds
        self._ring = AudioRingBuffer(sample_rate=sample_rate, seconds=buffer_seconds)
        self._stream: Any = None
        self._inference_stream: Any = None

    # -- gate ----------------------------------------------------------------

    @property
    def gate_active(self) -> bool:
        """Whether a wake hit will actually be checked against a voiceprint."""
        return self.verifier is not None and self.require_verification

    def _check_gate_preconditions(self) -> None:
        """Refuse to start when the configured gate cannot possibly hold.

        Every branch here raises. There is no path that logs a warning and opens
        the microphone anyway -- that is what fail-closed means in practice.
        """
        if not self.require_verification:
            return
        if self.verifier is None:
            raise ProviderUnavailable(
                "speaker verification is required but no verifier is attached; "
                "pass require_verification=False only if you accept that anyone "
                "can wake the platform"
            )
        status = self.verifier.load()
        if not status.available:
            raise ProviderUnavailable(
                f"speaker verification is required but unusable: {status.details['reason']}"
            )
        if not self.verifier.speakers:
            raise ProviderUnavailable(
                "speaker verification is required but nobody is enrolled; "
                "run scripts/enroll_speaker.py first"
            )

    def _authorise(self, keyword: str) -> None:
        """Decide one wake hit, then drop the audio it was decided on."""
        if not self.gate_active:
            # Escape hatch only: diagnose() reports this as a warning.
            self.on_wake(keyword, None)
            return
        window = self._ring.snapshot(self.verify_seconds)
        try:
            result = self.verifier.verify(window, sample_rate=self.sample_rate)
        except Exception as exc:  # a verifier fault is a rejection, never a pass
            if self.on_reject is not None:
                self.on_reject(keyword, f"verifier error: {exc}", 0.0)
            return
        finally:
            # The window has served its only purpose. Holding it longer widens
            # the biometric exposure for no benefit.
            self._ring.clear()
        if result.accepted:
            self.on_wake(keyword, result.score)
        elif self.on_reject is not None:
            self.on_reject(keyword, result.reason, result.score)

    # -- capture -------------------------------------------------------------

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status:
            # Capture status is intentionally left to the caller's logger.
            return
        samples = indata[:, 0]
        self._ring.write(samples)
        if self.speech_gate is not None and not self.speech_gate(samples):
            return
        for keyword, _kws_score in self.keyword_provider.feed(
            self._inference_stream, samples, self.sample_rate
        ):
            self._authorise(keyword)

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as exc:
            raise ProviderUnavailable("sounddevice is not installed") from exc
        # Gate first: a refused gate must not leave a device open behind it.
        self._check_gate_preconditions()
        status = self.keyword_provider.load()
        if not status.available:
            raise ProviderUnavailable(status.details["reason"])
        self._inference_stream = self.keyword_provider.create_stream()
        self._ring.clear()
        self._stream = sounddevice.InputStream(
            samplerate=self.sample_rate,
            blocksize=self.blocksize,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        self._stream = None
        self._inference_stream = None
        self._ring.clear()
        self.keyword_provider.close()
