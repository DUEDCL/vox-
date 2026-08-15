"""Streaming speech recognition via sherpa-onnx (ADR 001).

The ``sherpa-onnx-streaming-zipformer-zh-14M`` transducer turns 16 kHz audio
into CJK text, chunk by chunk, with endpoint detection -- the ASR half of red
line 1. This provider owns recognition only: the microphone stream is fed by
``SounddeviceWakeCapture``, and the recognised text is handed to the caller
rather than to any cloud.

Loading is lazy and idempotent; a missing model reports ``available=False``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ProviderStatus, ProviderUnavailable


@dataclass(frozen=True)
class AsrResult:
    """One decoded increment: the partial text and whether an endpoint fired."""

    text: str
    is_endpoint: bool


class SherpaStreamingAsrProvider:
    """Lazy, local streaming recognizer behind the same provider shape as KWS/VAD."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        num_threads: int = 2,
        provider: str = "cpu",
        enable_endpoint_detection: bool = True,
        decoding_method: str = "greedy_search",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.execution_provider = provider
        self.enable_endpoint_detection = enable_endpoint_detection
        self.decoding_method = decoding_method
        self._recognizer: Any = None

    @property
    def available(self) -> bool:
        return (
            all(
                (self.model_dir / f"{name}-epoch-99-avg-1.onnx").is_file()
                for name in ("encoder", "decoder", "joiner")
            )
            and (self.model_dir / "tokens.txt").is_file()
        )

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(
                False, str(self.model_dir), {"reason": "streaming asr model files not found"}
            )
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            self._recognizer = sherpa.OnlineRecognizer.from_transducer(
                tokens=str(self.model_dir / "tokens.txt"),
                encoder=str(self.model_dir / "encoder-epoch-99-avg-1.onnx"),
                decoder=str(self.model_dir / "decoder-epoch-99-avg-1.onnx"),
                joiner=str(self.model_dir / "joiner-epoch-99-avg-1.onnx"),
                num_threads=self.num_threads,
                sample_rate=16000,
                feature_dim=80,
                enable_endpoint_detection=self.enable_endpoint_detection,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=1.2,
                rule3_min_utterance_length=20.0,
                decoding_method=self.decoding_method,
                provider=self.execution_provider,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised here
            self._recognizer = None
            return ProviderStatus(
                False, str(self.model_dir), {"reason": f"streaming asr load failed: {exc}"}
            )
        return ProviderStatus(
            True, str(self.model_dir), {"engine": "sherpa-onnx", "provider": self.execution_provider}
        )

    def create_stream(self) -> Any:
        if self._recognizer is None:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])
        return self._recognizer.create_stream()

    def feed(
        self, stream: Any, samples: Any, sample_rate: int = 16000
    ) -> AsrResult:
        """Accept one chunk and return the partial text plus an endpoint flag.

        ``is_endpoint`` means the recognizer has heard enough silence to call an
        utterance finished; the caller should then read the final text and
        ``reset`` the stream.
        """
        if self._recognizer is None:
            raise ProviderUnavailable("streaming asr provider is not loaded")
        stream.accept_waveform(sample_rate, samples)
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        return AsrResult(
            text=self._recognizer.get_result(stream),
            is_endpoint=self._recognizer.is_endpoint(stream),
        )

    def finalize(self, stream: Any) -> str:
        """Flush the stream and return the final text."""
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        return self._recognizer.get_result(stream)

    def reset(self, stream: Any) -> None:
        self._recognizer.reset(stream)

    def close(self) -> None:
        self._recognizer = None


__all__ = ["AsrResult", "SherpaStreamingAsrProvider"]
