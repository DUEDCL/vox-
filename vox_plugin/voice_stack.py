"""The voice stack, assembled from config: four models and one capture device.

``VoiceRuntime`` knows how to run a turn but nothing about microphones or model
files, and ``scripts/acceptance/live_conversation.py`` knew both -- with the three
model directories and a hard-coded ``speaker="owner"`` baked into an acceptance
script. That is why 「说话就能用」 lived in a file whose header says it needs a
human in the room. This module is the missing assembly, and
``scripts/run_voice.py`` is its command line.

Three decisions, each with a plausible-looking opposite:

- **Missing TTS or ASR degrades; a missing voiceprint does not.** Silence and
  wake-only are usable modes worth reporting. An unguarded wake is not a mode --
  ``capture.start()`` refuses it, and this module must not hand it an
  ``require_verification=False`` to work around that.

- **``readiness()`` is the one answer to "what am I still missing".** The console's
  checklist and the command line's startup report read the same list, so a fix
  that shows up in one shows up in the other.

- **Nothing here holds the verified speaker.** The gate reports it through
  ``capture.on_verified`` into the plugin (see ``vox_plugin/plugin.py``); a stack
  object that cached a name would be a second source of truth for the one fact
  ``shell.run`` exists to demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.audio import (
    SherpaKeywordProvider,
    SherpaStreamingAsrProvider,
    SherpaTtsProvider,
    SounddeviceWakeCapture,
    SpeakerVerificationProvider,
    load_speaker_config,
    load_voice_config,
    resolve_device,
    resolve_keywords_file,
)


@dataclass
class VoiceStack:
    """The assembled providers plus what could not be assembled and why."""

    config: dict[str, Any]
    capture: SounddeviceWakeCapture | None = None
    kws: SherpaKeywordProvider | None = None
    asr: SherpaStreamingAsrProvider | None = None
    tts: SherpaTtsProvider | None = None
    verifier: SpeakerVerificationProvider | None = None
    warnings: tuple[str, ...] = ()
    #: Set when the gate is deliberately off (the escape hatch). Reported as a
    #: warning everywhere, because "anyone can wake it" must never be quiet.
    gate_off: bool = False
    _closed: bool = field(default=False, init=False, repr=False)

    def readiness(self) -> list[dict[str, Any]]:
        """One row per thing that can be missing, with what to do about it.

        This is deliberately a list of plain dicts rather than a nested report:
        the console renders it as rows and the command line prints it as lines,
        and neither should have to know the shape of a tree.
        """
        rows: list[dict[str, Any]] = []

        def row(item: str, ready: bool, detail: str, hint: str = "") -> None:
            rows.append({"item": item, "ready": ready, "detail": detail, "hint": hint})

        kws_ready = self.kws is not None and self.kws.available
        row(
            "wake",
            kws_ready,
            str(self.kws.model_dir) if self.kws else "(not built)",
            "" if kws_ready else "缺唤醒模型：解压 models/kws.tar.bz2 或设 VOX_KWS_MODEL_DIR",
        )

        asr_on = bool(self.config.get("asr.enabled", True))
        asr_ready = self.asr is not None and self.asr.available
        row(
            "asr",
            asr_ready or not asr_on,
            str(self.asr.model_dir) if self.asr else "(disabled)",
            ""
            if asr_ready or not asr_on
            else "缺识别模型：解压 models/asr.tar.bz2 或设 VOX_ASR_MODEL_DIR（当前只唤醒不转写）",
        )

        tts_on = bool(self.config.get("tts.enabled", True))
        tts_ready = self.tts is not None and self.tts.available
        row(
            "tts",
            tts_ready or not tts_on,
            str(self.tts.model_dir) if self.tts else "(disabled)",
            ""
            if tts_ready or not tts_on
            else "缺合成模型：解压 models/tts.tar.bz2 或设 VOX_TTS_MODEL_DIR（当前不出声）",
        )

        if self.gate_off:
            row("speaker", False, "gate is off", "声纹门已关：任何人都能唤醒，只该用于调试")
        else:
            described = self.verifier.describe() if self.verifier else {}
            enrolled = described.get("speakers") or []
            model_ok = bool(described.get("available"))
            row(
                "speaker",
                model_ok and bool(enrolled),
                f"model={'ok' if model_ok else 'missing'} enrolled={len(enrolled)}",
                ""
                if model_ok and enrolled
                else (
                    "缺声纹模型：见 THIRD_PARTY_NOTICES.md 的下载说明"
                    if not model_ok
                    else "还没有人注册：在控制台录 3 段，或跑 scripts/enroll_speaker.py"
                ),
            )
        return rows

    def close(self) -> None:
        """Release every provider this stack built. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for provider in (self.capture, self.kws, self.asr, self.tts, self.verifier):
            if provider is None:
                continue
            for method in ("stop", "close"):
                callback = getattr(provider, method, None)
                if callable(callback):
                    try:
                        callback()
                    except Exception:
                        # Teardown is best-effort: a native audio error must not
                        # stop the remaining providers from being released.
                        pass
                    break


def open_voice_stack(
    config: dict[str, Any] | None = None,
    *,
    require_verification: bool | None = None,
    with_tts: bool | None = None,
    with_asr: bool | None = None,
    device: int | str | None = None,
) -> VoiceStack:
    """Build the four providers and one capture from ``config/voice.toml``.

    Nothing is loaded here beyond the voiceprint's own ``describe()``: the KWS,
    ASR and TTS models load on ``capture.start()`` / first synthesis, so building
    a stack to *inspect* it (which is what the console's checklist does) costs no
    model load.

    ``require_verification=False`` is the escape hatch and it is recorded as such
    (``gate_off``). It is a keyword-only argument with no config-file equivalent
    on purpose: turning the gate off should require a deliberate line of code or a
    named command-line flag, never a stray edit in a TOML file.
    """
    resolved = dict(config) if config is not None else load_voice_config()
    warnings: list[str] = []

    if require_verification is None:
        try:
            require_verification = bool(load_speaker_config()["require_verification"])
        except Exception as exc:  # noqa: BLE001 - unreadable config, stay closed
            warnings.append(f"speaker config unreadable, keeping the gate on: {type(exc).__name__}")
            require_verification = True

    kws = SherpaKeywordProvider(
        resolved["kws_dir"],
        keywords_file=resolve_keywords_file(resolved),
        keywords_threshold=float(resolved["wake.keywords_threshold"]),
        num_threads=int(resolved["wake.num_threads"]),
    )
    if not kws.available:
        warnings.append(f"wake model not found at {resolved['kws_dir']}")

    asr = None
    if with_asr if with_asr is not None else bool(resolved["asr.enabled"]):
        asr = SherpaStreamingAsrProvider(
            resolved["asr_dir"], num_threads=int(resolved["asr.num_threads"])
        )
        if not asr.available:
            warnings.append(
                f"asr model not found at {resolved['asr_dir']}; wake only, no transcription"
            )
            asr = None

    tts = None
    if with_tts if with_tts is not None else bool(resolved["tts.enabled"]):
        tts = SherpaTtsProvider(
            resolved["tts_dir"],
            num_threads=int(resolved["tts.num_threads"]),
            speaker_id=int(resolved["tts.speaker_id"]),
            speed=float(resolved["tts.speed"]),
        )
        if not tts.available:
            warnings.append(f"tts model not found at {resolved['tts_dir']}; answers stay silent")
            tts = None

    verifier = None
    if require_verification:
        try:
            verifier = SpeakerVerificationProvider.from_config()
        except Exception as exc:  # noqa: BLE001 - reported; capture.start() refuses
            # Deliberately *not* a downgrade to require_verification=False. An
            # unguarded wake is not a degraded mode, it is a different product.
            warnings.append(f"voiceprint gate unavailable: {type(exc).__name__}: {exc}")
    else:
        warnings.append("voiceprint gate is OFF: anyone can wake this platform")

    speaker_config = {}
    try:
        speaker_config = load_speaker_config()
    except Exception:  # noqa: BLE001 - defaults below are the safe ones
        pass

    capture = SounddeviceWakeCapture(
        kws,
        on_wake=lambda keyword, score: None,
        sample_rate=int(resolved["input.sample_rate"]),
        blocksize=int(resolved["input.blocksize"]),
        device=device if device is not None else resolve_device(resolved),
        verifier=verifier,
        require_verification=require_verification,
        buffer_seconds=float(speaker_config.get("buffer_seconds", 3.0)),
        verify_seconds=float(speaker_config.get("verify_seconds", 1.5)),
        asr_provider=asr,
    )

    return VoiceStack(
        config=resolved,
        capture=capture,
        kws=kws,
        asr=asr,
        tts=tts,
        verifier=verifier,
        warnings=tuple(warnings),
        gate_off=not require_verification,
    )


__all__ = ["VoiceStack", "open_voice_stack"]
