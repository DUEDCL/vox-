from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .base import ProviderStatus, ProviderUnavailable


class SherpaVadProvider:
    """Stateful Silero VAD through the sherpa-onnx runtime."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_duration: float = 0.5,
        min_speech_duration: float = 0.25,
        max_speech_duration: float = 20.0,
        num_threads: int = 1,
        provider: str = "cpu",
    ) -> None:
        self.model_path = Path(model_path)
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.min_silence_duration = min_silence_duration
        self.min_speech_duration = min_speech_duration
        self.max_speech_duration = max_speech_duration
        self.num_threads = num_threads
        self.execution_provider = provider
        self._vad: Any = None

    def load(self) -> ProviderStatus:
        if not self.model_path.is_file():
            return ProviderStatus(False, str(self.model_path), {"reason": "silero VAD model not found"})
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            silero = sherpa.SileroVadModelConfig(
                model=str(self.model_path),
                threshold=self.threshold,
                min_silence_duration=self.min_silence_duration,
                min_speech_duration=self.min_speech_duration,
                max_speech_duration=self.max_speech_duration,
            )
            config = sherpa.VadModelConfig(
                silero_vad=silero,
                sample_rate=self.sample_rate,
                num_threads=self.num_threads,
                provider=self.execution_provider,
            )
            if not config.validate():
                raise ValueError("invalid sherpa VAD configuration")
            self._vad = sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=60)
        except Exception as exc:
            self._vad = None
            return ProviderStatus(False, str(self.model_path), {"reason": f"sherpa VAD load failed: {exc}"})
        return ProviderStatus(True, str(self.model_path), {"engine": "sherpa-onnx", "model": "silero-vad"})

    def feed(self, samples: Any) -> dict[str, Any]:
        if self._vad is None:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])
        self._vad.accept_waveform(samples)
        return self._result()

    def flush(self) -> dict[str, Any]:
        if self._vad is None:
            raise ProviderUnavailable("sherpa VAD provider is not loaded")
        self._vad.flush()
        return self._result()

    def _result(self) -> dict[str, Any]:
        segments: list[dict[str, int]] = []
        while not self._vad.empty():
            segment = self._vad.front
            segments.append({"start": segment.start, "samples": len(segment.samples)})
            self._vad.pop()
        return {"speech": self._vad.is_speech_detected(), "segments": segments}

    def reset(self) -> None:
        if self._vad is not None:
            self._vad.reset()

    def close(self) -> None:
        self._vad = None
