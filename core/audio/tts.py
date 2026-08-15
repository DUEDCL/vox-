"""Local text-to-speech via sherpa-onnx MeloTTS (ADR 001).

MeloTTS VITS synthesises zh/en text to 22.05 kHz float32 audio, entirely on the
CPU in this process -- the TTS half of red line 1. This provider owns synthesis
only: playback, the TTS queue and barge-in belong to the turn orchestrator, not
to model inference, for the same reason KWS/VAD are split from capture.

The model is the pinned ``vits-melo-tts-zh_en`` under ``models/``. Loading is
lazy and idempotent; a missing model reports ``available=False`` rather than
raising at import or construction time.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .base import ProviderStatus, ProviderUnavailable

#: Files the MeloTTS packaging must contain. ``data_dir`` carries the rule FSTs
#: that convert Chinese text (dates, numbers, phones) to phonemes before VITS.
_RULE_FSTS = ("date.fst", "number.fst", "phone.fst", "new_heteronym.fst")


@dataclass
class TtsAudio:
    """One synthesised utterance, as float32 samples. Not frozen: the array is
    the payload and the consumer may hand it straight to a playback stream."""

    samples: np.ndarray
    sample_rate: int
    elapsed_ms: int


class SherpaTtsProvider:
    """Lazy, local MeloTTS synthesizer behind the same provider shape as KWS/VAD."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        num_threads: int = 2,
        provider: str = "cpu",
        speaker_id: int = 0,
        speed: float = 1.0,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.execution_provider = provider
        self.speaker_id = speaker_id
        self.speed = speed
        self._tts: Any = None

    @property
    def available(self) -> bool:
        return (
            (self.model_dir / "model.onnx").is_file()
            and (self.model_dir / "tokens.txt").is_file()
            and (self.model_dir / "lexicon.txt").is_file()
            and (self.model_dir / "dict").is_dir()
            and all((self.model_dir / name).is_file() for name in _RULE_FSTS)
        )

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(
                False, str(self.model_dir), {"reason": "melo tts model files not found"}
            )
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            vits = sherpa.OfflineTtsVitsModelConfig(
                model=str(self.model_dir / "model.onnx"),
                tokens=str(self.model_dir / "tokens.txt"),
                lexicon=str(self.model_dir / "lexicon.txt"),
                dict_dir=str(self.model_dir / "dict"),
                data_dir=str(self.model_dir),
            )
            model = sherpa.OfflineTtsModelConfig(
                vits=vits, num_threads=self.num_threads, provider=self.execution_provider
            )
            rules = ",".join(str(self.model_dir / name) for name in _RULE_FSTS)
            self._tts = sherpa.OfflineTts(
                sherpa.OfflineTtsConfig(model=model, rule_fsts=rules, max_num_sentences=1)
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised here
            self._tts = None
            return ProviderStatus(
                False, str(self.model_dir), {"reason": f"melo tts load failed: {exc}"}
            )
        return ProviderStatus(
            True, str(self.model_dir), {"engine": "sherpa-onnx", "provider": self.execution_provider}
        )

    def synthesize(
        self, text: str, *, speaker_id: int | None = None, speed: float | None = None
    ) -> TtsAudio:
        """One utterance to samples. Empty text and an unloadable model both raise."""
        if not text.strip():
            raise ProviderUnavailable("tts text cannot be empty")
        if self._tts is None:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])
        started = time.perf_counter()
        audio = self._tts.generate(
            text,
            sid=self.speaker_id if speaker_id is None else speaker_id,
            speed=self.speed if speed is None else speed,
        )
        return TtsAudio(
            samples=np.asarray(audio.samples, dtype=np.float32),
            sample_rate=audio.sample_rate,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def close(self) -> None:
        """Release native inference state; the Python wrapper has no explicit close."""
        self._tts = None


__all__ = ["SherpaTtsProvider", "TtsAudio"]
