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
        prefer_int8: bool = True,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.execution_provider = provider
        self.enable_endpoint_detection = enable_endpoint_detection
        self.decoding_method = decoding_method
        #: int8 优先。同一个模型的 int8 build 在 CPU 上快一倍多，而这是个常驻负载；
        #: fp32 只在没有 int8 时用。
        self.prefer_int8 = prefer_int8
        #: 取不出中间结果的次数。见 ``_partial`` —— 不是零不代表坏，但一直涨说明这个模型
        #: 不适合做实时转写显示。
        self.partial_errors = 0
        self._recognizer: Any = None

    def _find(self, role: str) -> Path | None:
        """一个角色（encoder/decoder/joiner）对应的 onnx 文件。

        **文件名不能写死。** sherpa-onnx 的流式模型至少有三种命名：
        ``encoder-epoch-99-avg-1.onnx``（zh-14M）、
        ``encoder-epoch-20-avg-1-chunk-16-left-128.int8.onnx``（multi-zh-hans）、
        ``encoder.int8.onnx``（2025 版）。写死其中一种意味着换模型要改代码，而换模型是
        用户该能做的事 —— 尤其在发现当前那个把「检查运行状态」听成「起床先生信息」之后。

        ``decoder`` 通常没有 int8 build（它太小，量化没意义），所以 int8 优先是**偏好而不是
        要求**：找不到 int8 就用 fp32。
        """
        int8 = sorted(self.model_dir.glob(f"{role}*.int8.onnx"))
        plain = [
            path for path in sorted(self.model_dir.glob(f"{role}*.onnx"))
            if not path.name.endswith(".int8.onnx")
        ]
        order = (int8, plain) if self.prefer_int8 else (plain, int8)
        for group in order:
            if group:
                # 同一组里挑名字最短的：多个 epoch 的 checkpoint 并存时，带更多后缀的
                # 那个通常是变体而不是主文件。
                return min(group, key=lambda path: len(path.name))
        return None

    @property
    def available(self) -> bool:
        return all(
            self._find(role) is not None for role in ("encoder", "decoder", "joiner")
        ) and (self.model_dir / "tokens.txt").is_file()

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(
                False, str(self.model_dir), {"reason": "streaming asr model files not found"}
            )
        encoder = self._find("encoder")
        decoder = self._find("decoder")
        joiner = self._find("joiner")
        assert encoder is not None and decoder is not None and joiner is not None
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            self._recognizer = sherpa.OnlineRecognizer.from_transducer(
                tokens=str(self.model_dir / "tokens.txt"),
                encoder=str(encoder),
                decoder=str(decoder),
                joiner=str(joiner),
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
            True,
            str(self.model_dir),
            {
                "engine": "sherpa-onnx",
                "provider": self.execution_provider,
                # 用的是哪个文件要报出来：换模型之后「它到底加载了哪个」是第一个要问的。
                "encoder": encoder.name,
            },
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
        endpoint = self._recognizer.is_endpoint(stream)
        return AsrResult(text=self._partial(stream), is_endpoint=endpoint)

    def _partial(self, stream: Any) -> str:
        """当前的中间结果，取不出来就是空字符串。

        **为什么要吞掉这个异常。** ``multi-zh-hans-2023-12-12``（BPE 建模）在流中途调
        ``get_result`` 会抛 ``ValueError: vector too long`` —— 而这个调用发生在**音频回调
        线程**上，抛出去的结果是 capture 记一次 callback error、重建 KWS 流、回到唤醒模式。
        表现是「唤醒命中了好几次，但一句话都没转写出来」，实测过。

        吞掉它是安全的，因为**最终文本不走这条路**：``capture._recognize`` 只用
        ``is_endpoint``，真正的文本来自 ``finalize()``（那是 ``input_finished`` 之后取的，
        状态完整，同一个模型上从没抛过）。中间结果只用于「球上显示实时转写」那类展示，
        少几帧不影响正确性。

        计数留着：一个总是取不出中间结果的模型说明它不适合做实时显示，而那是可观测的事实
        而不是要静默接受的。
        """
        try:
            return self._recognizer.get_result(stream)
        except Exception:  # noqa: BLE001 - 见上：这个调用在音频回调线程上
            self.partial_errors += 1
            return ""

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
