from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderUnavailable(RuntimeError):
    pass


@dataclass
class ProviderStatus:
    available: bool
    source: str
    details: dict[str, Any]


class VoxCordAdapter:
    """Optional adapter around an adjacent VoxCord checkout.

    The integration is intentionally dynamic: this project remains importable and
    testable without VoxCord, while a configured checkout supplies real local
    wake/VAD/provider implementations.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.getenv("EVOX_VOXCORD_ROOT") or r"D:\program\voxcord"
        self.root = Path(configured)
        self.core_root = self.root / "packages" / "voxcord_core"
        self.core_lib = self.core_root / "lib"
        self._loaded = False

    def load(self) -> ProviderStatus:
        if not self.core_lib.is_dir():
            return ProviderStatus(False, str(self.root), {"reason": "voxcord core not found"})
        for path in (self.core_lib, self.core_root):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        try:
            importlib.import_module("voxcord_core.audio_engine.wake_word")
            importlib.import_module("voxcord_core.vad.silero_adapter")
        except Exception as exc:
            return ProviderStatus(False, str(self.root), {"reason": f"import failed: {exc}"})
        self._loaded = True
        return ProviderStatus(True, str(self.root), {"wake": "voxcord", "vad": "silero-or-rms"})

    def _require(self) -> None:
        if not self._loaded:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])

    def create_wake_providers(self) -> list[Any]:
        self._require()
        module = importlib.import_module("voxcord_core.audio_engine.wake_word")
        return module.create_wake_word_providers()

    def create_vad(self) -> Any:
        self._require()
        config_module = importlib.import_module("voxcord_core.config")
        vad_module = importlib.import_module("voxcord_core.vad.silero_adapter")
        return vad_module.SileroVADService(config_module.load_config())

    async def evaluate_vad(self, samples: list[float]) -> dict[str, Any]:
        service = self.create_vad()
        message_module = importlib.import_module("voxcord_core.core.message")
        return await service.evaluate(message_module.BusMessage("vad.evaluate", data={"samples": samples}))


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
