"""语音活动检测（VAD）：唯一能回答「这段音频里到底有没有人在说话」的东西。

``SherpaVadProvider`` 是引擎包装（取语音段，验收脚本在用）；``SileroSpeechGate`` 是把它
适配成 ``capture.speech_gate`` 那个钩子形状的可调用对象，并且能对一整段音频回答「有没有
人说话」。

## 为什么必须有它，而不是再调一次阈值

2026-08-31 的事故链条完整地说明了「没有 VAD 的自动增益」为什么必然出事：使用者的设备
原始峰值只有 0.0587，房间底噪高于 ``AutoGain.floor_peak``(0.004)，于是增益把底噪一路抬到
目标电平 —— 一段静音在下游看起来是 rms 0.21 的健康语音。声纹从它注册出一个**房间的指纹**，
再拿一段底噪去比，余弦 0.979「通过」。三层读数全绿，而门实际上谁都放。

根因不是某个阈值定错了，是**一个跟峰值的 AGC 在原理上分不清「轻的语音」和「没有语音」**。
峰值、RMS、削波比例都是能量统计量，而「是不是人声」不是能量问题。任何用能量阈值去近似
它的做法都会在某台设备上翻车 —— 使用者的原话：「真正的最佳效果应该是无论何种设备、音量，
都能准确的识别唤醒词」。要做到这一点，增益必须**只在语音上适应**，而这需要一个真的 VAD。

## 为什么是 sherpa-onnx 自带的 silero，不是自己写

先搜先例：VAD 是通用能力，不是本项目特有的。而 ``sherpa-onnx`` 1.13.4 **已经装着**
`VadModelConfig` / `SileroVadModelConfig` / `VoiceActivityDetector`，``models/silero_vad.onnx``
（2.3 MB）也早就在盘上。所以这条路是**零新依赖**：同一个 onnxruntime、同一套 provider
形状，连模型都不用再下。silero-vad 本身是 MIT。

## 它在这套东西里的位置

**不拿它去闸 KWS。** KWS 是流式解码器，喂给它一条被切碎的音频流可能反而降低命中率，而
命中率正是要保住的东西。VAD 在这里做两件事，都是「让别的层看到真相」：

1. **驱动增益**：``AutoGain`` 只在语音块上更新包络，底噪永远不会把增益抬上去；
2. **回答「这一段有语音吗」**：控制台的取样、注册、试一句据此拒绝，而不是靠一条峰值线。

失败姿态是**放行**（``available=False`` 时恒真）。这一层是鲁棒性增强，不是安全边界 ——
安全边界是声纹门。一个加载不起来的 VAD 不该让唤醒整体失效。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .base import ProviderStatus, ProviderUnavailable

#: 出厂模型名。放在 ``models/`` 下，和别的模型同一个位置。
DEFAULT_MODEL_NAME = "silero_vad.onnx"

#: silero 要求的窗口大小（样本数）。16 kHz 上是 512 —— 模型的输入形状，不是可调项。
SILERO_WINDOW = 512


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


class SileroSpeechGate:
    """``gate(samples) -> bool``：「这一块里有人在说话吗」。

    包一层而不是直接用 ``SherpaVadProvider``：``capture.speech_gate`` 那个钩子要的是一个
    可调用对象，而且这里的失败姿态必须是**放行** —— 引擎那一层在缺模型时会抛
    ``ProviderUnavailable``，那对一个鲁棒性增强层是错的姿态。
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_speech_duration: float = 0.25,
        min_silence_duration: float = 0.30,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        self.model_path = Path(model_path or root / "models" / DEFAULT_MODEL_NAME)
        self.sample_rate = int(sample_rate)
        self.threshold = float(threshold)
        self.min_speech_duration = float(min_speech_duration)
        self.min_silence_duration = float(min_silence_duration)
        self._engine: SherpaVadProvider | None = None
        #: 观测量。``speech_blocks / blocks`` 就是「这段时间里有多少是语音」。
        self.blocks = 0
        self.speech_blocks = 0
        self.error = ""

    @property
    def available(self) -> bool:
        return self.model_path.is_file()

    def _open(self) -> SherpaVadProvider | None:
        """建一个引擎。失败只记一次原因，之后不再重试（免得每块都去碰磁盘）。"""
        if self._engine is not None:
            return self._engine
        if self.error or not self.available:
            self.error = self.error or "vad model not found"
            return None
        engine = SherpaVadProvider(
            self.model_path,
            sample_rate=self.sample_rate,
            threshold=self.threshold,
            min_speech_duration=self.min_speech_duration,
            min_silence_duration=self.min_silence_duration,
        )
        status = engine.load()
        if not status.available:
            self.error = str(status.details.get("reason", "vad load failed"))
            return None
        self._engine = engine
        return engine

    def load(self) -> ProviderStatus:
        engine = self._open()
        if engine is None:
            return ProviderStatus(False, str(self.model_path), {"reason": self.error})
        return ProviderStatus(
            True,
            str(self.model_path),
            {"engine": "sherpa-onnx silero", "threshold": self.threshold},
        )

    def __call__(self, samples: Any) -> bool:
        """**加载不起来时恒返回 True。**

        这一层是鲁棒性增强而不是安全边界（那是声纹门），一个读不到模型的 VAD 不该让唤醒
        整体失效 —— 失败姿态是放行。
        """
        self.blocks += 1
        engine = self._open()
        if engine is None:
            return True
        try:
            speaking = bool(engine.feed(_flat(samples))["speech"])
        except Exception as exc:  # noqa: BLE001 - 同上：坏了就放行
            self.error = f"{type(exc).__name__}: {exc}"
            self._engine = None
            return True
        if speaking:
            self.speech_blocks += 1
        return speaking

    def has_speech(self, samples: Any, *, min_ratio: float = 0.08) -> bool:
        """一整段音频里有没有语音。**用一个新的引擎**，不碰流式那一个的状态。

        ``min_ratio`` 是「多少比例的窗口被判成语音才算这一段有人说话」。0.08 很宽松：
        3 秒的录音里只要有约 0.25 秒语音就算 —— 这道闸要挡的是**完全没人说话**，
        不是「说得不够多」。缺模型时返回 ``True``（放行，见 ``__call__``）。
        """
        if not self.available:
            return True
        import numpy as np

        values = np.asarray(samples, dtype="float32").reshape(-1)
        if values.size < SILERO_WINDOW:
            return False
        probe = SileroSpeechGate(
            self.model_path,
            sample_rate=self.sample_rate,
            threshold=self.threshold,
            min_speech_duration=self.min_speech_duration,
            min_silence_duration=self.min_silence_duration,
        )
        if probe._open() is None:
            return True
        windows = 0
        speech = 0
        for start in range(0, values.size - SILERO_WINDOW + 1, SILERO_WINDOW):
            windows += 1
            if probe(values[start : start + SILERO_WINDOW]):
                speech += 1
        probe.close()
        if not windows:
            return False
        return (speech / windows) >= float(min_ratio)

    def reset(self) -> None:
        if self._engine is not None:
            self._engine.reset()

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "loaded": self._engine is not None,
            "model": str(self.model_path),
            "threshold": self.threshold,
            "blocks": int(self.blocks),
            "speech_blocks": int(self.speech_blocks),
            "error": self.error,
        }

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close()
        self._engine = None


def _flat(samples: Any) -> Any:
    """sherpa 的 ``accept_waveform`` 要一维 float32。"""
    import numpy as np

    return np.asarray(samples, dtype="float32").reshape(-1)


__all__ = ["DEFAULT_MODEL_NAME", "SILERO_WINDOW", "SherpaVadProvider", "SileroSpeechGate"]
