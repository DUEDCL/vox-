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
from core.audio.gain import AutoGain
from core.audio.vad import SileroSpeechGate


@dataclass
class VoiceStack:
    """The assembled providers plus what could not be assembled and why."""

    config: dict[str, Any]
    capture: SounddeviceWakeCapture | None = None
    kws: SherpaKeywordProvider | None = None
    asr: SherpaStreamingAsrProvider | None = None
    #: 合成器。**类型是 Any 而不是 SherpaTtsProvider** —— 2026-08-29 起它也可能是
    #: `DashScopeTtsProvider`。两者摆同一个形状，标死一个具体类会让「可替换」变成谎话。
    tts: Any = None
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
        # 「在哪」对两种 provider 不是同一样东西：本机是模型目录，云端是端点主机 + 音色。
        # 报路径的那一行如果对云端也印目录，读的人会以为配置没生效。
        if self.tts is None:
            where = "(disabled)"
        elif hasattr(self.tts, "model_dir"):
            where = str(self.tts.model_dir)
        else:
            where = f"{self.tts.model} / {self.tts.voice} @ {self.tts._safe_endpoint()}"
        cloud = self.tts is not None and not hasattr(self.tts, "model_dir")
        row(
            "tts",
            tts_ready or not tts_on,
            where,
            ""
            if tts_ready or not tts_on
            else (
                f"云端合成缺密钥：把 key 写进 .env 的 {self.config.get('tts.key_env', 'VOX_DASHSCOPE_KEY')}（当前不出声）"
                if cloud
                else "缺合成模型：解压 models/tts.tar.bz2 或设 VOX_TTS_MODEL_DIR（当前不出声）"
            ),
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


def _open_tts(resolved: dict[str, Any], warnings: list[str]) -> Any:
    """按 ``tts.provider`` 建合成器。缺什么就降级为不出声并如实报告。

    两个 provider 摆的是同一个形状（`synthesize` / `speak` / `speak_segments` / `stop`），
    所以这个函数是**唯一**知道有两种 TTS 的地方 —— 插件、编排器、控制台都不需要知道。
    这正是红线 2 说的「组件可替换」。

    云端那条路的失败**不降级到本机**：一个要求 longyuan 的人拿到 VITS 的默认女声会以为
    配置生效了。报出来、静音，让「没生效」是可见的。
    """
    provider = str(resolved.get("tts.provider", "sherpa")).strip().lower()
    if provider in ("dashscope", "cosyvoice", "aliyun", "bailian"):
        from core.audio.tts_cloud import DashScopeTtsProvider

        model = str(resolved.get("tts.model", "")).strip() or "cosyvoice-v2"
        voice = str(resolved.get("tts.voice", "")).strip() or "longyuan"
        key_env = str(resolved.get("tts.key_env", "")).strip() or "VOX_DASHSCOPE_KEY"
        cloud = DashScopeTtsProvider(
            model=model,
            voice=voice,
            key_env=key_env,
            speed=float(resolved["tts.speed"]),
            # **必须传。** 2026-08-30 查出这一行此前漏了，后果不是「少一个可选项」：
            # `config/voice.toml` 里那句「用温柔、亲和、放松的语气说」、控制台上那一栏、
            # 以及为它写的整段注释全部对生产无效 —— 听到的一直是裸音色。而 `EDITABLE`
            # 里有 `tts.instruction`，所以页面上改了会显示成功。**一个能改、能存、
            # 不生效的配置项比没有这个配置项糟得多。**
            instruction=str(resolved.get("tts.instruction", "")).strip(),
        )
        status = cloud.load()
        if not status.available:
            warnings.append(f"云端 TTS 不可用：{status.details['reason']}（回答不出声）")
            return None
        return cloud
    if provider not in ("sherpa", "local", ""):
        warnings.append(f"未知的 tts.provider {provider!r}，按本机 sherpa 处理")
    tts = SherpaTtsProvider(
        resolved["tts_dir"],
        num_threads=int(resolved["tts.num_threads"]),
        speaker_id=int(resolved["tts.speaker_id"]),
        speed=float(resolved["tts.speed"]),
    )
    if not tts.available:
        warnings.append(f"tts model not found at {resolved['tts_dir']}; answers stay silent")
        return None
    return tts


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
        # 束宽。**必须传** —— 它是「安静房间里叫得应、有点噪声就叫不应」的那个参数，
        # 而漏传它的后果不是「少一个可选项」：`config/voice.toml` 里那一行会变成一个
        # 能改、能存、不生效的配置项。实测 beam 4 在 0 dB SNR 只剩 2/5，16 是 5/5，
        # 每块耗时不变。见 core/audio/kws.py 的表。
        max_active_paths=int(resolved["wake.max_active_paths"]),
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
        tts = _open_tts(resolved, warnings)

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

    resolved_device = device if device is not None else resolve_device(resolved)
    if device is None and isinstance(resolved_device, str):
        # 配置里写了名字，但没有一只输入设备匹配得上（可用的那几个 host API 里没有它）。
        #
        # **改用系统默认，而不是把名字原样交给 PortAudio。** 交给它的话抛的是
        # `Multiple input devices found for '耳机'` 后面跟两条 WDM-KS 条目 —— 而那两条
        # 恰恰是 `_match_device` 刻意排除掉的（它们开不起来，见 config.py 的
        # `_EXCLUDED_HOST_APIS`）。让一个已经判断过「这些不能用」的解析结果去触发一条
        # 列举它们的报错，是最容易把人带错方向的一种失败。
        #
        # 退到默认设备之后麦克风可能是聋的（这台机器上默认就是那只峰值 0.00003 的阵列），
        # 但那条路有专门的探测：开麦 4 秒后「全零输入」进运行日志（error 级）。
        # 「有一只可能听不见的麦克风」比「一只都没有」可诊断得多。
        #
        # **只对来自配置的名字这么做。** ``device=`` 参数是调用方点名的（探针的
        # ``--device`` 走这条），那种情况原样交出去 —— 替调用方改主意会让一个专门指定
        # 设备的测试悄悄测到别的设备上。
        warnings.append(
            f"input.device = {resolved_device!r} 没匹配到任何可用的输入设备 —— "
            "耳机可能没插（蓝牙设备断开后就不在枚举里了）。这一轮改用**系统默认设备**，"
            "它若是聋的会在开麦 4 秒后进运行日志；控制台就绪清单里有当前的设备清单"
        )
        resolved_device = None

    capture = SounddeviceWakeCapture(
        kws,
        on_wake=lambda keyword, score: None,
        sample_rate=int(resolved["input.sample_rate"]),
        blocksize=int(resolved["input.blocksize"]),
        device=resolved_device,
        verifier=verifier,
        require_verification=require_verification,
        # VAD。**它不闸 KWS**，作用是让增益只在语音上适应、并回答「这一段有语音吗」。
        # 见 core/audio/vad.py：一个跟峰值的 AGC 分不清「轻的语音」和「放大后的底噪」，
        # 而那正是 2026-08-31 把声纹门变成 fail-open 的那件事。零新依赖、模型已在盘上。
        speech_gate=SileroSpeechGate(sample_rate=int(resolved["input.sample_rate"])),
        buffer_seconds=float(speaker_config.get("buffer_seconds", 3.0)),
        verify_seconds=float(speaker_config.get("verify_seconds", 1.5)),
        # 唤醒之后给多少秒开口。见 core/audio/capture.py 的 listen_grace_s ——
        # 不给宽限期的话，静默 2.4 秒（端点检测的 rule1）就把聆听结束掉。
        listen_grace_s=float(speaker_config.get("listen_grace_s", 8.0)),
        asr_provider=asr,
        # 自适应输入增益。默认开:让「Windows 输入音量该调多少」不再是用户的事 ——
        # 实测那个可用窗口很窄（默认 100 时削波、调到 7 才能命中），而窗口位置取决于
        # 用哪只麦克风、戴不戴耳机、离多远。可用 [input] auto_gain = false 关掉。
        auto_gain=AutoGain() if bool(resolved.get("input.auto_gain", True)) else None,
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
