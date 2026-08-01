"""Local speaker verification (声纹校验).

Only a previously enrolled voice may drive the platform. This runs entirely on
the existing sherpa-onnx runtime -- no new dependency, no network call.

Two red-line consequences are enforced here:

* Audio is never persisted. ``enroll`` and ``verify`` accept in-memory sample
  chunks and keep only the resulting embedding vectors.
* Enrollment data is biometric. It lives outside the repository tree by default
  and is listed in ``.gitignore``; ``describe`` never returns raw vectors.
"""

from __future__ import annotations

import importlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .base import ProviderStatus, ProviderUnavailable

DEFAULT_MODEL_NAME = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
DEFAULT_CONFIG_NAME = "speaker.toml"
STORE_VERSION = 1


def load_speaker_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``config/speaker.toml``, falling back to the built-in defaults.

    ``tomllib`` is standard library from 3.11, so configurability costs no new
    dependency. A missing file is not an error -- the shipped defaults are the
    secure ones (``require_verification = true``), and the fallback must not be
    the moment protection quietly turns off.
    """
    root = Path(__file__).resolve().parents[2]
    config_path = Path(path or os.getenv("EVOX_SPEAKER_CONFIG", root / "config" / DEFAULT_CONFIG_NAME))
    defaults: dict[str, Any] = {
        "require_verification": True,
        "threshold": 0.5,
        "min_verify_seconds": 0.6,
        "min_enroll_seconds": 1.5,
        "buffer_seconds": 3.0,
        "verify_seconds": 1.5,
    }
    if not config_path.is_file():
        return defaults
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProviderUnavailable(f"speaker config is unreadable: {exc}") from exc
    merged = dict(defaults)
    for section in ("speaker", "capture"):
        for key, value in (raw.get(section) or {}).items():
            if key in merged:
                merged[key] = value
    return merged


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one gate decision.

    ``accepted`` is the only field the wake path should branch on. ``score`` is
    the cosine similarity against the best matching enrolled speaker and is
    reported for diagnostics and threshold tuning.
    """

    accepted: bool
    speaker: str | None
    score: float
    reason: str


@dataclass(frozen=True)
class EnrollmentResult:
    speaker: str
    samples_used: int
    total_seconds: float
    dim: int


class SpeakerStore:
    """Embedding-only persistence for enrolled speakers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, list[list[float]]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderUnavailable(f"speaker enrollment store is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            raise ProviderUnavailable("speaker enrollment store has an unsupported version")
        speakers = raw.get("speakers")
        if not isinstance(speakers, dict):
            return {}
        return {
            name: [[float(x) for x in vector] for vector in vectors]
            for name, vectors in speakers.items()
            if isinstance(vectors, list) and vectors
        }

    def save(self, speakers: dict[str, list[list[float]]], *, dim: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STORE_VERSION, "dim": dim, "speakers": speakers}
        # Write through a temp file so an interrupted save cannot corrupt an
        # existing enrollment.
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)


class SpeakerVerificationProvider:
    """Lazy, local speaker verification through sherpa-onnx.

    Loading is deferred exactly like the other providers: constructing this
    object never reads the model, and a missing model yields an unavailable
    ``ProviderStatus`` instead of an import-time crash.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        store_path: str | Path | None = None,
        threshold: float = 0.5,
        min_verify_seconds: float = 0.6,
        min_enroll_seconds: float = 1.5,
        num_threads: int = 1,
        provider: str = "cpu",
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        self.model_path = Path(
            model_path or os.getenv("EVOX_SPEAKER_MODEL", root / "models" / DEFAULT_MODEL_NAME)
        )
        self.store = SpeakerStore(
            store_path or os.getenv("EVOX_SPEAKER_ENROLLMENT", root / "enrollment" / "voiceprints.json")
        )
        self.threshold = threshold
        self.min_verify_seconds = min_verify_seconds
        self.min_enroll_seconds = min_enroll_seconds
        self.num_threads = num_threads
        self.execution_provider = provider
        self._extractor: Any = None
        self._manager: Any = None
        self._dim = 0

    @property
    def available(self) -> bool:
        return self.model_path.is_file()

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def speakers(self) -> list[str]:
        if self._manager is None:
            return sorted(self.store.load())
        return sorted(self._manager.all_speakers)

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(
                False, str(self.model_path), {"reason": "speaker verification model not found"}
            )
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            config = sherpa.SpeakerEmbeddingExtractorConfig(
                model=str(self.model_path),
                num_threads=self.num_threads,
                provider=self.execution_provider,
            )
            if not config.validate():
                raise ValueError("invalid speaker embedding extractor configuration")
            self._extractor = sherpa.SpeakerEmbeddingExtractor(config)
            self._dim = self._extractor.dim
            self._manager = sherpa.SpeakerEmbeddingManager(self._dim)
        except Exception as exc:
            self._extractor = None
            self._manager = None
            return ProviderStatus(False, str(self.model_path), {"reason": f"speaker model load failed: {exc}"})
        enrolled = self._restore()
        return ProviderStatus(
            True,
            str(self.model_path),
            {"engine": "sherpa-onnx", "dim": self._dim, "enrolled": enrolled, "threshold": self.threshold},
        )

    def _restore(self) -> list[str]:
        """Re-register persisted embeddings into a fresh manager."""
        restored: list[str] = []
        for name, vectors in self.store.load().items():
            usable = [v for v in vectors if len(v) == self._dim]
            if usable and self._manager.add(name, usable):
                restored.append(name)
        return sorted(restored)

    def _require(self) -> None:
        if self._extractor is None:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])

    # -- embedding -----------------------------------------------------------

    def embed(self, samples: Any, sample_rate: int = 16000) -> list[float]:
        """Compute one embedding from in-memory samples.

        ``samples`` is any float32 buffer the sherpa stream accepts. It is
        consumed and dropped -- nothing reaches the filesystem.
        """
        self._require()
        duration = len(samples) / float(sample_rate)
        if duration < self.min_verify_seconds:
            raise ProviderUnavailable(
                f"audio too short for speaker verification: {duration:.2f}s "
                f"< {self.min_verify_seconds}s"
            )
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=sample_rate, waveform=samples)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            raise ProviderUnavailable("speaker extractor did not accept enough audio")
        return [float(x) for x in self._extractor.compute(stream)]

    # -- enrollment ----------------------------------------------------------

    def enroll(
        self, name: str, chunks: Iterable[Any], *, sample_rate: int = 16000
    ) -> EnrollmentResult:
        """Register or extend one speaker from several sample chunks.

        Existing vectors for ``name`` are kept and the new ones appended, so a
        weak enrollment can be improved without a full re-record.
        """
        self._require()
        name = (name or "").strip()
        if not name:
            raise ValueError("speaker name must not be empty")
        vectors: list[list[float]] = []
        total_seconds = 0.0
        for chunk in chunks:
            total_seconds += len(chunk) / float(sample_rate)
            vectors.append(self.embed(chunk, sample_rate))
        if not vectors:
            raise ValueError("enrollment needs at least one sample chunk")
        if total_seconds < self.min_enroll_seconds:
            raise ProviderUnavailable(
                f"enrollment audio too short: {total_seconds:.2f}s "
                f"< {self.min_enroll_seconds}s"
            )
        speakers = self.store.load()
        speakers.setdefault(name, []).extend(vectors)
        self.store.save(speakers, dim=self._dim)
        # Re-register from scratch: the manager has no append-to-existing call.
        self._manager.remove(name)
        self._manager.add(name, speakers[name])
        return EnrollmentResult(name, len(vectors), total_seconds, self._dim)

    # -- verification --------------------------------------------------------

    def verify(self, samples: Any, *, sample_rate: int = 16000) -> VerificationResult:
        """Decide whether in-memory audio belongs to an enrolled speaker.

        This never raises for an ordinary rejection. Every failure path -- model
        missing, nobody enrolled, embedding error -- returns ``accepted=False``,
        so a caller that only branches on ``accepted`` is fail-closed by
        construction.
        """
        try:
            self._require()
        except ProviderUnavailable as exc:
            return VerificationResult(False, None, 0.0, str(exc))
        if self._manager.num_speakers == 0:
            return VerificationResult(False, None, 0.0, "no speaker enrolled")
        try:
            vector = self.embed(samples, sample_rate)
        except Exception as exc:
            return VerificationResult(False, None, 0.0, f"embedding failed: {exc}")
        name, score = self._best_match(vector)
        if name is None:
            return VerificationResult(False, None, 0.0, "no comparable enrollment")
        if score >= self.threshold:
            return VerificationResult(True, name, score, "match")
        return VerificationResult(False, None, score, f"below threshold {self.threshold}")

    def _best_match(self, vector: list[float]) -> tuple[str | None, float]:
        """Best cosine score across enrolled speakers.

        ``SpeakerEmbeddingManager.search`` would only answer yes/no. Scoring each
        speaker also yields the number, which threshold tuning and the
        ``wake.rejected`` diagnostics both need.
        """
        best_name: str | None = None
        best_score = 0.0
        for name in self._manager.all_speakers:
            try:
                score = float(self._manager.score(name, vector))
            except Exception:
                continue
            if best_name is None or score > best_score:
                best_name, best_score = name, score
        return best_name, best_score

    # -- maintenance ---------------------------------------------------------

    def remove(self, name: str) -> bool:
        """Delete one speaker's enrollment from both the store and the manager.

        Works without a loaded model so enrollment can be cleaned up on a host
        that no longer has the model file.
        """
        speakers = self.store.load()
        existed = speakers.pop(name, None) is not None
        if existed:
            dim = self._dim or next(
                (len(v) for vectors in speakers.values() for v in vectors), 0
            )
            self.store.save(speakers, dim=dim)
        if self._manager is not None:
            self._manager.remove(name)
        return existed

    def describe(self) -> dict[str, Any]:
        """Status for ``diagnose()``: names and counts only, never raw vectors.

        Enrollment data is biometric, so this is the single sanctioned way to
        report on it. Callers must not reach into ``store`` directly.
        """
        try:
            speakers = self.store.load()
        except ProviderUnavailable as exc:
            return {
                "available": self.available,
                "model": str(self.model_path),
                "store": str(self.store.path),
                "loaded": self._extractor is not None,
                "speakers": [],
                "reason": str(exc),
            }
        return {
            "available": self.available,
            "model": str(self.model_path),
            "store": str(self.store.path),
            "loaded": self._extractor is not None,
            "dim": self._dim,
            "threshold": self.threshold,
            "speakers": sorted(speakers),
            "samples_per_speaker": {name: len(v) for name, v in speakers.items()},
        }

    def close(self) -> None:
        self._extractor = None
        self._manager = None
        self._dim = 0

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        **overrides: Any,
    ) -> SpeakerVerificationProvider:
        """Build a provider from ``config/speaker.toml``.

        Only the threshold and duration limits come from the file. Paths stay on
        environment variables so a config file checked into a repository can
        never point at somebody's enrollment data.
        """
        config = load_speaker_config(config_path)
        return cls(
            threshold=overrides.pop("threshold", config["threshold"]),
            min_verify_seconds=overrides.pop("min_verify_seconds", config["min_verify_seconds"]),
            min_enroll_seconds=overrides.pop("min_enroll_seconds", config["min_enroll_seconds"]),
            **overrides,
        )



