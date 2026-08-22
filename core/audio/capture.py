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
        asr_provider: Any = None,
        on_recognized: Any = None,
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
        #: Optional streaming ASR for the listening phase after a wake. With
        #: neither ``asr_provider`` nor ``on_recognized`` the capture stays in
        #: wake-only mode, exactly as before.
        self.asr_provider = asr_provider
        self.on_recognized = on_recognized
        self._listening = False
        self._asr_stream: Any = None
        self._ring = AudioRingBuffer(sample_rate=sample_rate, seconds=buffer_seconds)
        self._stream: Any = None
        self._inference_stream: Any = None
        self._keyword_provider_loaded = False
        self._asr_provider_loaded = False
        self._verifier_loaded = False
        self._callback_faulted = False
        #: Count only business/callback exceptions; never retain their messages.
        self.callback_errors = 0
        self.last_callback_error: str | None = None

    # -- lifecycle helpers ----------------------------------------------------

    @staticmethod
    def _safe_call(obj: Any, method_name: str, *args: Any) -> None:
        """Call an optional teardown/reset method without blocking cleanup."""
        try:
            method = getattr(obj, method_name, None)
            if method is not None:
                method(*args)
        except Exception:
            # Teardown is best-effort. In particular, never let a native audio
            # error prevent the other resources from being reset.
            pass

    def _cleanup_resources(self) -> None:
        """Best-effort teardown used by both failed starts and ``stop``.

        Fields are detached before calling foreign/native code. That makes the
        operation idempotent even when a stream or provider raises, and prevents
        a later cleanup attempt from repeating the same side effect.
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            self._safe_call(stream, "stop")
            self._safe_call(stream, "close")

        asr_stream, self._asr_stream = self._asr_stream, None
        self._listening = False
        if asr_stream is not None:
            self._safe_call(self.asr_provider, "reset", asr_stream)

        self._inference_stream = None
        self._callback_faulted = False
        self._ring.clear()

        keyword_loaded, self._keyword_provider_loaded = self._keyword_provider_loaded, False
        if keyword_loaded:
            self._safe_call(self.keyword_provider, "close")

        asr_loaded, self._asr_provider_loaded = self._asr_provider_loaded, False
        if asr_loaded:
            self._safe_call(self.asr_provider, "close")

        verifier_loaded, self._verifier_loaded = self._verifier_loaded, False
        if verifier_loaded:
            self._safe_call(self.verifier, "close")

    def _reset_kws_stream(self) -> bool:
        """Replace a possibly poisoned KWS stream after a callback failure."""
        had_kws_state = self._inference_stream is not None or self._keyword_provider_loaded
        self._inference_stream = None
        if not had_kws_state:
            return True
        try:
            self._inference_stream = self.keyword_provider.create_stream()
        except Exception:
            # A callback cannot safely stop sounddevice from inside itself. Keep
            # the device object for ``stop()``, but stop processing future audio.
            self._callback_faulted = True
            return False
        self._callback_faulted = False
        return True

    def _recover_after_callback_error(self) -> None:
        """Return to a safe KWS-only state after an exception in the callback."""
        asr_stream, self._asr_stream = self._asr_stream, None
        self._listening = False
        if asr_stream is not None:
            self._safe_call(self.asr_provider, "reset", asr_stream)
        self._ring.clear()
        self._reset_kws_stream()

    def _record_callback_error(self, exc: Exception) -> None:
        self.callback_errors += 1
        # Exception messages can contain paths, user text, or provider details.
        # The callback surface retains only a non-sensitive type name.
        self.last_callback_error = type(exc).__name__

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
        self._verifier_loaded = True
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
            self._start_listening()
            return
        window = self._ring.snapshot(self.verify_seconds)
        try:
            result = self.verifier.verify(window, sample_rate=self.sample_rate)
        except Exception as exc:  # a verifier fault is a rejection, never a pass
            if self.on_reject is not None:
                self.on_reject(keyword, f"verifier error: {type(exc).__name__}", 0.0)
            return
        finally:
            # The window has served its only purpose. Holding it longer widens
            # the biometric exposure for no benefit.
            self._ring.clear()
        if result.accepted:
            self.on_wake(keyword, result.score)
            self._start_listening()
        elif self.on_reject is not None:
            self.on_reject(keyword, result.reason, result.score)

    def _start_listening(self) -> None:
        """Enter ASR mode after an accepted wake, so the follow-up speech is
        transcribed rather than fed to KWS."""
        if self.asr_provider is None or self.on_recognized is None:
            return
        self._asr_stream = self.asr_provider.create_stream()
        self._listening = True

    def _recognize(self, samples: Any) -> None:
        """Feed the recognizer; on an endpoint, deliver the final text and
        return to KWS mode."""
        if self._asr_stream is None:
            self._listening = False
            return
        result = self.asr_provider.feed(self._asr_stream, samples, self.sample_rate)
        if not result.is_endpoint:
            return
        asr_stream = self._asr_stream
        text = self.asr_provider.finalize(asr_stream)
        # Detach first so a reset/callback failure cannot trigger a second reset
        # from the outer recovery path.
        self._listening = False
        self._asr_stream = None
        self.asr_provider.reset(asr_stream)
        if text.strip():
            self.on_recognized(text.strip())

    # -- capture -------------------------------------------------------------

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if self._callback_faulted or status:
            # Capture status is intentionally left to the caller's logger. A
            # failed recovery keeps the stream alive only for best-effort stop.
            return
        try:
            samples = indata[:, 0]
            if self._listening:
                self._recognize(samples)
                return
            self._ring.write(samples)
            if self.speech_gate is not None and not self.speech_gate(samples):
                return
            for keyword, _kws_score in self.keyword_provider.feed(
                self._inference_stream, samples, self.sample_rate
            ):
                self._authorise(keyword)
        except Exception as exc:
            self._record_callback_error(exc)
            self._recover_after_callback_error()

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as exc:
            raise ProviderUnavailable("sounddevice is not installed") from exc
        try:
            # Gate first: a refused gate must not leave a device open behind it.
            self._check_gate_preconditions()
            self._keyword_provider_loaded = True
            status = self.keyword_provider.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])
            self._inference_stream = self.keyword_provider.create_stream()
            if self.asr_provider is not None:
                self._asr_provider_loaded = True
                asr_status = self.asr_provider.load()
                if not asr_status.available:
                    raise ProviderUnavailable(asr_status.details["reason"])
            self._ring.clear()
            self._callback_faulted = False
            self._stream = sounddevice.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception:
            # Includes provider/model errors, InputStream construction errors,
            # and InputStream.start() failures. All partial resources are reset so
            # the next call can retry from a clean transaction boundary.
            self._cleanup_resources()
            raise

    def stop(self) -> None:
        self._cleanup_resources()
