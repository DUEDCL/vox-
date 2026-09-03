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

#: 云端识别的出厂模型。写在这里而不是只写在 `asr_cloud.py`：``_open_asr`` 要在
#: `asr.model` 为空时填一个默认值，而那个默认值该和控制台上显示的是同一个。
DEFAULT_CLOUD_ASR_MODEL = "qwen-audio-3.0-asr-flash"


@dataclass
class VoiceStack:
    """The assembled providers plus what could not be assembled and why."""

    config: dict[str, Any]
    capture: SounddeviceWakeCapture | None = None
    kws: SherpaKeywordProvider | None = None
    #: 识别器。**类型是 Any** —— 2026-09-03 起它也可能是 `DashScopeAsrProvider`。
    #: 和 tts 同一个理由：标死一个具体类会让红线 2 的「可替换」变成谎话。
    asr: Any = None
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
        # 「在哪」对两种 provider 不是同一样东西，和 TTS 那一行同一个道理：本机是模型目录，
        # 云端是模型名 + 端点主机。给云端印一个目录会让读的人以为配置没生效。
        if self.asr is None:
            asr_where = "(disabled)"
        elif hasattr(self.asr, "model_dir"):
            asr_where = str(self.asr.model_dir)
        else:
            asr_where = f"{self.asr.model} @ {self.asr._safe_endpoint()}"
            latency = int(getattr(self.asr, "last_latency_ms", 0))
            failures = int(getattr(self.asr, "failures", 0))
            if latency:
                asr_where = f"{asr_where}（上一次 {latency} ms）"
            if failures:
                asr_where = f"{asr_where} —— 失败 {failures} 次"
        asr_cloud = self.asr is not None and not hasattr(self.asr, "model_dir")
        row(
            "asr",
            asr_ready or not asr_on,
            asr_where,
            ""
            if asr_ready or not asr_on
            else (
                f"云端识别缺 {getattr(self.asr, 'key_env', 'VOX_ASR_KEY')} —— "
                "在控制台「密钥」那一栏存进去"
                if asr_cloud
                else "缺识别模型：解压 models/asr.tar.bz2 或设 VOX_ASR_MODEL_DIR（当前只唤醒不转写）"
            ),
        )

        tts_on = bool(self.config.get("tts.enabled", True))
        tts_ready = self.tts is not None and self.tts.available
        # 「在哪」对两种 provider 不是同一样东西：本机是模型目录，云端是端点主机 + 音色。
        # 报路径的那一行如果对云端也印目录，读的人会以为配置没生效。
        #
        # **穿过 `FallbackTts`。** 云端那条路现在裹在退路里（见 tts_fallback.py），所以
        # 这里问的必须是它包着的那个主合成器 —— 2026-09-03 这一行直接读 `self.tts.model`
        # 把 `/api/state` 整个打成 500（`console failed: AttributeError`），症状是控制台
        # 顶上写「连接失败」而语音其实一直在正常工作。一个包装层让**别的**层报错，
        # 就是这种「看起来毫不相关」的故障。
        engine = getattr(self.tts, "primary", self.tts)
        degraded = bool(getattr(self.tts, "latched", False))
        if self.tts is None:
            where = "(disabled)"
        elif hasattr(engine, "model_dir"):
            where = str(engine.model_dir)
        else:
            where = f"{engine.model} / {engine.voice} @ {engine._safe_endpoint()}"
            if degraded:
                # 降级了就把这件事印在「在哪」那一栏：使用者听到的是本机那把嗓子，
                # 而这一行如果只印云端的音色，他会以为配置生效了。
                where = f"{where} —— 已降级为本机 VITS"
        cloud = self.tts is not None and not hasattr(engine, "model_dir")
        problems = list(getattr(self.tts, "problems", ()) or ())
        row(
            "tts",
            (tts_ready and not degraded) or not tts_on,
            where,
            ""
            if (tts_ready and not degraded) or not tts_on
            else (
                problems[-1]
                if problems
                else (
                    f"云端合成缺密钥：把 key 写进 .env 的 {self.config.get('tts.key_env', 'VOX_DASHSCOPE_KEY')}（当前不出声）"
                    if cloud
                    else "缺合成模型：解压 models/tts.tar.bz2 或设 VOX_TTS_MODEL_DIR（当前不出声）"
                )
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

    **2026-09-02：云端那条路的失败现在降级到本机，而不是静音。** 上一版的立场是「不降级，
    因为一个要求 longyuan 的人拿到 VITS 的默认女声会以为配置生效了」。真机上那个立场的
    代价出现了：`VOX_TTS_KEY` 被另一份 key 覆盖 → 百炼 401 → 三层各自吞掉 → **一句话都
    不出声，而且哪里都不说为什么**。对语音助手来说「不出声」和「没听见」「崩了」在使用者
    那一侧同形。所以现在是降级 + 大声说（见 `core/audio/tts_fallback.py` 的模块头）。
    """
    provider = str(resolved.get("tts.provider", "sherpa")).strip().lower()
    if provider in ("dashscope", "cosyvoice", "aliyun", "bailian"):
        from core.audio.tts_cloud import DashScopeTtsProvider
        from core.audio.tts_fallback import FallbackTts

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
        local = SherpaTtsProvider(
            resolved["tts_dir"],
            num_threads=int(resolved["tts.num_threads"]),
            speaker_id=int(resolved["tts.speaker_id"]),
            speed=float(resolved["tts.speed"]),
        )
        # 装上退路而不是直接返回 cloud：401 这类失败**只在真的合成时**才暴露，而那时
        # `complete_turn` 会把异常吞掉。见 tts_fallback.py 的模块头。
        tts = FallbackTts(cloud, local)
        # 这里就 load 一次，让「云端起不起来」在启动清单上有答案。`FallbackTts.load()`
        # 会在失败时自己切到本机并留下原因（原因里带 key_env 的**变量名** —— 「缺 key」
        # 不可行动，「缺 VOX_… 这个变量」可行动）。
        tts.load()
        if tts.latched:
            warnings.append(f"云端 TTS 不可用：{tts.problems[-1]}")
        return tts
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


def _open_asr(resolved: dict[str, Any], warnings: list[str]) -> Any:
    """按 ``asr.provider`` 建识别器。两个 provider 同形，所以这是唯一知道有两种的地方。

    与 ``_open_tts`` 的一个**故意的差别：没有 FallbackAsr。** TTS 的降级发生在合成那一刻
    （401 只在真的合成时暴露），而识别的降级只能发生在启动时 —— 一句话说到一半切引擎会
    让那句话的前半段丢在云端、后半段丢在本机，两边都不完整。所以这里的选择在开麦之前
    就定下来，并把「为什么不是你配的那个」大声说出来。
    """
    provider = str(resolved.get("asr.provider", "sherpa")).strip().lower()
    if provider in ("dashscope", "qwen", "aliyun", "bailian", "cloud"):
        from core.audio.asr_cloud import DashScopeAsrProvider

        cloud = DashScopeAsrProvider(
            model=str(resolved.get("asr.model", "")).strip() or DEFAULT_CLOUD_ASR_MODEL,
            key_env=str(resolved.get("asr.key_env", "")).strip() or "VOX_ASR_KEY",
            silence_s=float(resolved["asr.silence_s"]),
            max_utterance_s=float(resolved["asr.max_utterance_s"]),
            timeout_s=float(resolved["asr.timeout_s"]),
            vad_model=str(resolved.get("vad_model", "")) or None,
        )
        if cloud.available:
            return cloud
        # 缺 key。**退回本机而不是不转写** —— 「唤醒了，球弹出来了，一个字都没转」在使用者
        # 那一侧和「崩了」同形。退回去还能用，只是会把「小沃」听成「小吴」。
        warnings.append(
            f"云端识别不可用：{cloud.key_env} 没有值 —— 这一轮退回本机模型"
            "（它的字表里没有「沃」，专名会听错）。在控制台「密钥」那一栏存进去即可"
        )
        provider = "sherpa"
    if provider not in ("sherpa", "local", ""):
        warnings.append(f"未知的 asr.provider {provider!r}，按本机 sherpa 处理")
    asr = SherpaStreamingAsrProvider(
        resolved["asr_dir"], num_threads=int(resolved["asr.num_threads"])
    )
    if not asr.available:
        warnings.append(
            f"asr model not found at {resolved['asr_dir']}; wake only, no transcription"
        )
        return None
    return asr


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
        asr = _open_asr(resolved, warnings)

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
