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
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

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
    config_path = Path(path or os.getenv("VOX_SPEAKER_CONFIG", root / "config" / DEFAULT_CONFIG_NAME))
    defaults: dict[str, Any] = {
        "require_verification": True,
        "threshold": 0.5,
        "min_verify_seconds": 0.6,
        "min_enroll_seconds": 1.5,
        "buffer_seconds": 3.0,
        "verify_seconds": 1.5,
        # Gate-hardening limits (2026-08-24). Quality floors reject junk audio
        # before it reaches the model; the cooldown throttles brute-force wake
        # attempts. They are heuristics, NOT anti-replay spoof detection --
        # ADR 002's limitation stands until a dedicated spoof model lands.
        "min_rms": 0.002,
        "max_clip_ratio": 0.05,
        "verify_windows": 1,
        "max_consecutive_rejections": 5,
        "cooldown_s": 30.0,
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
        min_rms: float = 0.002,
        max_clip_ratio: float = 0.05,
        verify_windows: int = 1,
        max_consecutive_rejections: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        self.model_path = Path(
            model_path or os.getenv("VOX_SPEAKER_MODEL", root / "models" / DEFAULT_MODEL_NAME)
        )
        self.store = SpeakerStore(
            store_path or os.getenv("VOX_SPEAKER_ENROLLMENT", root / "enrollment" / "voiceprints.json")
        )
        self.threshold = threshold
        self.min_verify_seconds = min_verify_seconds
        self.min_enroll_seconds = min_enroll_seconds
        self.num_threads = num_threads
        self.execution_provider = provider
        self.min_rms = min_rms
        self.max_clip_ratio = max_clip_ratio
        #: >1 splits the buffer into equal windows and requires every one of
        #: them to match the same speaker. Default 1 keeps the single-window
        #: decision; the stricter setting needs REAL-MIC tuning before use.
        self.verify_windows = max(1, int(verify_windows))
        self.max_consecutive_rejections = max(0, int(max_consecutive_rejections))
        self.cooldown_s = cooldown_s
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        # Brute-force throttle state. Counts only -- no audio, no vectors.
        self._rejection_streak = 0
        self._last_rejection_at = 0.0
        self._cooldown_until = 0.0
        self.gate_stats = {
            "accepted": 0,
            "rejected_below_threshold": 0,
            "rejected_quality": 0,
            "rejected_cooldown": 0,
            "consecutive_rejections": 0,
        }
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

        This never raises for an ordinary rejection. Every failure path --
        cooldown, bad audio quality, model missing, nobody enrolled, embedding
        error, below threshold -- returns ``accepted=False``,
        so a caller that only branches on ``accepted`` is fail-closed by
        construction.
        """
        # Cheap input-side gates run before anything expensive or
        # environment-dependent, so their verdicts stay reachable even on a
        # host with no model installed.
        if self._cooldown_active():
            self.gate_stats["rejected_cooldown"] += 1
            remaining = round(self._cooldown_until - self._clock(), 1)
            return VerificationResult(
                False, None, 0.0, f"verification cooling down for {remaining}s"
            )
        quality = self._audio_quality_issue(samples)
        if quality is not None:
            self.gate_stats["rejected_quality"] += 1
            return self._after_input_rejection(
                VerificationResult(False, None, 0.0, quality)
            )
        try:
            self._require()
        except ProviderUnavailable as exc:
            return VerificationResult(False, None, 0.0, str(exc))
        if self._manager.num_speakers == 0:
            return VerificationResult(False, None, 0.0, "no speaker enrolled")
        if self.verify_windows > 1:
            return self._verify_multi_window(samples, sample_rate)
        try:
            vector = self.embed(samples, sample_rate)
        except Exception as exc:
            return VerificationResult(False, None, 0.0, f"embedding failed: {exc}")
        name, score = self._best_match(vector)
        if name is None:
            return VerificationResult(False, None, 0.0, "no comparable enrollment")
        if score >= self.threshold:
            self._rejection_streak = 0
            self.gate_stats["consecutive_rejections"] = 0
            self.gate_stats["accepted"] += 1
            return VerificationResult(True, name, score, "match")
        self.gate_stats["rejected_below_threshold"] += 1
        return self._after_input_rejection(
            VerificationResult(False, None, score, f"below threshold {self.threshold}")
        )

    def _cooldown_active(self) -> bool:
        return self._clock() < self._cooldown_until

    def _after_input_rejection(self, result: VerificationResult) -> VerificationResult:
        """Feed one input-driven rejection into the brute-force throttle.

        Model-missing and nobody-enrolled rejections say nothing about the
        input, so they never reach here -- only junk or unmatched audio does.
        A streak older than one cooldown period starts over: yesterday's
        pressure must not lock the owner out today.
        """
        now = self._clock()
        if now - self._last_rejection_at > max(self.cooldown_s, 60.0):
            self._rejection_streak = 0
        self._rejection_streak += 1
        self._last_rejection_at = now
        self.gate_stats["consecutive_rejections"] = self._rejection_streak
        if (
            self.max_consecutive_rejections
            and self._rejection_streak >= self.max_consecutive_rejections
        ):
            self._cooldown_until = now + self.cooldown_s
        return result

    def _audio_quality_issue(self, samples: Any) -> str | None:
        """Reject silence or clipping before any model runs.

        Cheap, deterministic, testable without the model. These checks throw
        away garbage inputs; they are heuristics and do NOT detect replayed
        speech (ADR 002's limitation stands until a spoof model lands).
        """
        values = np.asarray(samples, dtype=np.float32)
        if values.size == 0:
            return "empty audio buffer"
        rms = float(np.sqrt(np.mean(np.square(values))))
        if rms < self.min_rms:
            return f"audio too quiet to verify (rms {rms:.5f} < {self.min_rms})"
        clip_ratio = float(np.mean(np.abs(values) >= 0.99))
        if clip_ratio > self.max_clip_ratio:
            return f"audio is clipped/saturated ({clip_ratio:.2f} at rail; limit {self.max_clip_ratio})"
        return None

    def _verify_multi_window(self, samples: Any, sample_rate: int) -> VerificationResult:
        """Every equal window must match the same speaker above threshold.

        Stricter than a single pass: flukes and short splices must survive
        every window instead of one. Needs REAL-MIC tuning of threshold and
        window count before production use.
        """
        values = np.asarray(samples, dtype=np.float32)
        window_length = len(values) // self.verify_windows
        minimum = int(self.min_verify_seconds * sample_rate)
        if window_length < minimum:
            return self._after_input_rejection(
                VerificationResult(
                    False,
                    None,
                    0.0,
                    f"not enough audio for {self.verify_windows}-window verification:"
                    f" {len(values)} samples < {self.verify_windows} x {minimum}",
                )
            )
        best_score = 0.0
        agreed_speaker: str | None = None
        for index in range(self.verify_windows):
            chunk = values[index * window_length : (index + 1) * window_length]
            vector = self.embed(chunk, sample_rate)
            name, score = self._best_match(vector)
            best_score = max(best_score, score)
            if name is None or score < self.threshold:
                return self._after_input_rejection(
                    VerificationResult(
                        False,
                        None,
                        best_score,
                        f"window {index} below threshold {self.threshold}",
                    )
                )
            if agreed_speaker is None:
                agreed_speaker = name
            elif name != agreed_speaker:
                return self._after_input_rejection(
                    VerificationResult(
                        False,
                        None,
                        best_score,
                        f"windows disagree on speaker: {agreed_speaker} vs {name}",
                    )
                )
        self._rejection_streak = 0
        self.gate_stats["consecutive_rejections"] = 0
        self.gate_stats["accepted"] += 1
        return VerificationResult(True, agreed_speaker, best_score, "all windows match")

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
            "gate": {
                "min_rms": self.min_rms,
                "max_clip_ratio": self.max_clip_ratio,
                "verify_windows": self.verify_windows,
                "max_consecutive_rejections": self.max_consecutive_rejections,
                "cooldown_s": self.cooldown_s,
            },
            "gate_stats": dict(self.gate_stats),
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
            min_rms=overrides.pop("min_rms", config["min_rms"]),
            max_clip_ratio=overrides.pop("max_clip_ratio", config["max_clip_ratio"]),
            verify_windows=overrides.pop("verify_windows", config["verify_windows"]),
            max_consecutive_rejections=overrides.pop(
                "max_consecutive_rejections", config["max_consecutive_rejections"]
            ),
            cooldown_s=overrides.pop("cooldown_s", config["cooldown_s"]),
            **overrides,
        )



