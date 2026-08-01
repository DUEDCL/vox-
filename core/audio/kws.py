from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .base import ProviderStatus, ProviderUnavailable


class SherpaKeywordProvider:
    """Lazy, local Sherpa-ONNX keyword spotter.

    The provider owns only model inference. Audio capture is kept in
    ``SounddeviceWakeCapture`` so model tests can run against wav/sample arrays
    without opening a microphone.
    """

    def __init__(
        self,
        model_dir: str | Path,
        *,
        model_suffix: str = "epoch-99-avg-1-chunk-16-left-64",
        keywords_file: str | Path | None = None,
        keywords_threshold: float = 0.25,
        num_threads: int = 2,
        provider: str = "cpu",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.model_suffix = model_suffix
        self.keywords_file = Path(keywords_file) if keywords_file else self.model_dir / "keywords.txt"
        self.keywords_threshold = keywords_threshold
        self.num_threads = num_threads
        self.execution_provider = provider
        self._spotter: Any = None

    @property
    def available(self) -> bool:
        return self.model_dir.is_dir() and all(
            (self.model_dir / f"{name}-{self.model_suffix}.int8.onnx").is_file()
            for name in ("encoder", "decoder", "joiner")
        ) and (self.model_dir / "tokens.txt").is_file() and self.keywords_file.is_file()

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(False, str(self.model_dir), {"reason": "sherpa model files not found"})
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            self._spotter = sherpa.KeywordSpotter(
                encoder=str(self.model_dir / f"encoder-{self.model_suffix}.int8.onnx"),
                decoder=str(self.model_dir / f"decoder-{self.model_suffix}.int8.onnx"),
                joiner=str(self.model_dir / f"joiner-{self.model_suffix}.int8.onnx"),
                tokens=str(self.model_dir / "tokens.txt"),
                keywords_file=str(self.keywords_file),
                keywords_threshold=self.keywords_threshold,
                num_threads=self.num_threads,
                provider=self.execution_provider,
            )
        except Exception as exc:
            self._spotter = None
            return ProviderStatus(False, str(self.model_dir), {"reason": f"sherpa load failed: {exc}"})
        return ProviderStatus(True, str(self.model_dir), {"engine": "sherpa-onnx", "provider": self.execution_provider})

    def create_stream(self) -> Any:
        if self._spotter is None:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])
        return self._spotter.create_stream()

    def feed(self, stream: Any, samples: Any, sample_rate: int = 16000) -> list[str]:
        """Feed one realtime chunk and return all keyword hits from the chunk."""
        if self._spotter is None:
            raise ProviderUnavailable("sherpa keyword provider is not loaded")
        stream.accept_waveform(sample_rate, samples)
        hits: list[str] = []
        while self._spotter.is_ready(stream):
            self._spotter.decode_stream(stream)
            result = self._spotter.get_result(stream)
            if result:
                hits.append(result)
                self._spotter.reset_stream(stream)
        return hits

    def close(self) -> None:
        """Release native inference state; the Python wrapper has no explicit close."""
        self._spotter = None
