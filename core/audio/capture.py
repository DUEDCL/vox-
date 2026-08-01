from __future__ import annotations

import importlib
from typing import Any

from .base import ProviderUnavailable
from .kws import SherpaKeywordProvider


class SounddeviceWakeCapture:
    """Optional realtime microphone adapter around ``sounddevice.InputStream``."""

    def __init__(
        self,
        keyword_provider: SherpaKeywordProvider,
        on_wake: Any,
        *,
        sample_rate: int = 16000,
        blocksize: int = 1600,
        device: int | str | None = None,
        speech_gate: Any = None,
    ) -> None:
        self.keyword_provider = keyword_provider
        self.on_wake = on_wake
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = device
        self.speech_gate = speech_gate
        self._stream: Any = None
        self._inference_stream: Any = None

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status:
            # Capture status is intentionally left to the caller's logger.
            return
        samples = indata[:, 0]
        if self.speech_gate is not None and not self.speech_gate(samples):
            return
        for keyword in self.keyword_provider.feed(self._inference_stream, samples, self.sample_rate):
            self.on_wake(keyword, 1.0)

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as exc:
            raise ProviderUnavailable("sounddevice is not installed") from exc
        status = self.keyword_provider.load()
        if not status.available:
            raise ProviderUnavailable(status.details["reason"])
        self._inference_stream = self.keyword_provider.create_stream()
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
        self.keyword_provider.close()
