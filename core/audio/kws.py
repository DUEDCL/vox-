from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .base import ProviderStatus, ProviderUnavailable

#: 解码束宽。**这是纯召回参数，不改判定标准。**
#:
#: 唤醒词的假设路径要和普通转写路径竞争束里的位置，束太窄时它在信噪比低的地方会先被剪掉 ——
#: 表现是「在安静房间里叫得应，有人说话/开着风扇就叫不应」，而每一层都报告自己健康。
#:
#: **16 而不是 sherpa-onnx 的默认 4。** 2026-09-01 实测（本人三段真录音 + 加白噪声，
#: 正样本 5 次机会；负样本是本人念的另一个唤醒词「你好问问」三遍加纯噪声）：
#:
#: | beam | 干净 | 20dB | 15dB | 10dB | 5dB | 0dB | -5dB | -10dB | 误唤醒 |
#: |---|---|---|---|---|---|---|---|---|---|
#: | **4（旧默认）** | 5/5 | 5/5 | 5/5 | 5/5 | **4/5** | **2/5** | 3/5 | 2/5 | 0 |
#: | 8 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **5/5** | 3/5 | 4/5 | 0 |
#: | **16** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **5/5** | 4/5 | 4/5 | 0 |
#: | 32 | 5/5 | — | — | — | — | 5/5 | 5/5 | 5/5 | 0 |
#:
#: 代价基本为零：每块耗时 1.10 ms（beam 4）→ 1.21 ms（beam 16），都是 100 ms 预算的
#: 1% 左右 —— 这个模型只有 3.3M 参数，固定成本在编码器上，束搜索几乎不花钱。
#:
#: **为什么不取 32**：它的召回更好，但「误唤醒 0」这个数字在只有约 25 秒非唤醒词语音的
#: 样本上统计功效很低，不足以支撑跳到最宽的那一档。想更激进就改 `config/voice.toml` 的
#: `wake.max_active_paths` —— 一次误唤醒的代价被声纹门吃掉（记一条拒绝，不产生动作），
#: 所以这个方向是可以试的，只是要自己测。
DEFAULT_MAX_ACTIVE_PATHS = 16

#: 唤醒词 token 的加分。抬的是「到达关键词的容易程度」，不是「算出来的分算不算通过」。
DEFAULT_KEYWORDS_SCORE = 1.0

#: 出词前要求多少个静音帧。大了更稳、更慢。
DEFAULT_TRAILING_BLANKS = 1


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
        max_active_paths: int = DEFAULT_MAX_ACTIVE_PATHS,
        keywords_score: float = DEFAULT_KEYWORDS_SCORE,
        num_trailing_blanks: int = DEFAULT_TRAILING_BLANKS,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.model_suffix = model_suffix
        self.keywords_file = Path(keywords_file) if keywords_file else self.model_dir / "keywords.txt"
        self.keywords_threshold = keywords_threshold
        self.num_threads = num_threads
        self.execution_provider = provider
        self.max_active_paths = int(max_active_paths)
        self.keywords_score = float(keywords_score)
        self.num_trailing_blanks = int(num_trailing_blanks)
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
                max_active_paths=self.max_active_paths,
                keywords_score=self.keywords_score,
                num_trailing_blanks=self.num_trailing_blanks,
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

    def feed(
        self, stream: Any, samples: Any, sample_rate: int = 16000
    ) -> list[tuple[str, float | None]]:
        """Feed one realtime chunk and return ``(keyword, score)`` per hit.

        ``score`` is ``None``, and that is a deliberate, checked statement rather
        than an omission: sherpa-onnx 1.13.4's ``KeywordResult`` carries only
        ``keyword``, ``timestamps`` and ``tokens`` -- the binding exposes no
        per-hit confidence at all. The previous code reported ``1.0`` here, which
        read like a measurement and was not one.

        The number that reaches ``wake.detected`` is therefore the speaker
        verification similarity, which *is* measured. See ADR 002.
        """
        if self._spotter is None:
            raise ProviderUnavailable("sherpa keyword provider is not loaded")
        stream.accept_waveform(sample_rate, samples)
        hits: list[tuple[str, float | None]] = []
        while self._spotter.is_ready(stream):
            self._spotter.decode_stream(stream)
            result = self._spotter.get_result(stream)
            if result:
                hits.append((result, None))
                self._spotter.reset_stream(stream)
        return hits

    def close(self) -> None:
        """Release native inference state; the Python wrapper has no explicit close."""
        self._spotter = None
