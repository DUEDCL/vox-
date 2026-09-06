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
        rule1_silence: float = 2.4,
        rule2_silence: float = 1.2,
        rule3_utterance: float = 20.0,
        hotwords_file: str | Path = "",
        hotwords_score: float = 1.5,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.execution_provider = provider
        self.enable_endpoint_detection = enable_endpoint_detection
        self.decoding_method = decoding_method
        #: 端点检测的三条规则，秒。**它们此前写死在 ``load()`` 里，提上来是为了能被量。**
        #:
        #: - ``rule1``：一个字都没解出来时要等多久才算一段结束（唤醒后没人开口那条路）。
        #: - ``rule2``：**已经说出字之后**要静多久才算说完。这一条直接算进每一轮的延迟 ——
        #:   人说完最后一个字，到派发开始之间的那段纯等待就是它。
        #: - ``rule3``：一段话的硬上限，防止一个人一直说导致永不结束。
        #:
        #: 降 ``rule2`` 是省延迟最直接的一刀，但它有真实代价：句子中间的停顿（「帮我看看……
        #: 那个配置文件」）会被当成说完，于是后半句落进下一轮或者干脆丢掉。所以默认值不靠
        #: 感觉调 —— 见 `docs/research/prototype-results.md` 里那张按真录音测完整度的表。
        self.rule1_silence = float(rule1_silence)
        self.rule2_silence = float(rule2_silence)
        self.rule3_utterance = float(rule3_utterance)
        #: 热词表（上下文偏置）。**这是提高中文转写准确率最直接的一刀，而且是离线的。**
        #:
        #: 使用者两次报「语音转文字还是不够精准」，实测里最典型的一条是「帮我打开网易云音乐」
        #: 被听成「**试了**给我打开网易云音乐」—— 一个通用模型对固定说法和专名的先验不够。
        #: 热词把这些词的路径在解码时加分（`hotwords_score`），代价只有解码时那一点开销。
        #:
        #: **必须配 `modified_beam_search`。** greedy 解码**静默忽略**热词 ——
        #: sherpa-onnx 不会为此报错，表现就是「配了热词但一点变化都没有」。所以下面 `load()`
        #: 里在有热词时强制换解码方式，而不是让人自己记得同时改两个键。
        self.hotwords_file = Path(hotwords_file) if str(hotwords_file).strip() else None
        self.hotwords_score = float(hotwords_score)
        #: int8 优先。同一个模型的 int8 build 在 CPU 上快一倍多，而这是个常驻负载；
        #: fp32 只在没有 int8 时用。
        self.prefer_int8 = prefer_int8
        #: 取不出中间结果的次数。见 ``_partial`` —— 不是零不代表坏，但一直涨说明这个模型
        #: 不适合做实时转写显示。
        self.partial_errors = 0
        #: 实际生效的解码方式与热词表，由 ``load()`` 填。**报出来而不是让人猜** ——
        #: 「热词配了没生效」这件事唯一的读数就是这两个值。
        self.active_decoding = self.decoding_method
        self.active_hotwords = ""
        #: 热词表加载失败的原因（空 = 没失败）。**必须报出来**：失败时会退回不带热词，
        #: 而那时转写照常工作，所以没有这一行的话「热词为什么没生效」查不下去。
        self.hotwords_error = ""
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

    def _build(self, sherpa: Any, encoder: Path, decoder: Path, joiner: Path, method: str, hotwords: str) -> Any:
        """构造识别器。抽出来只为一件事：带热词失败时能原样再试一次不带热词的。"""
        return sherpa.OnlineRecognizer.from_transducer(
            tokens=str(self.model_dir / "tokens.txt"),
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            num_threads=self.num_threads,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=self.enable_endpoint_detection,
            rule1_min_trailing_silence=self.rule1_silence,
            rule2_min_trailing_silence=self.rule2_silence,
            rule3_min_utterance_length=self.rule3_utterance,
            decoding_method=method,
            hotwords_file=hotwords,
            hotwords_score=self.hotwords_score,
            provider=self.execution_provider,
        )

    def _token_inventory(self) -> set[str]:
        """模型能输出的 token 集合。读 ``tokens.txt``。

        存在的理由：**热词表里一个模型写不出的字会让整行失效**，而 sherpa-onnx 对此只在
        C++ 层打一串 `Cannot find ID for token …` 的日志 —— 那些行在 Python 侧完全看不见，
        表现是「配了热词，一半没用，而且不知道是哪一半」。
        """
        tokens = self.model_dir / "tokens.txt"
        inventory: set[str] = set()
        try:
            for line in tokens.read_text(encoding="utf-8").splitlines():
                piece = line.rsplit(" ", 1)
                if piece and piece[0]:
                    inventory.add(piece[0])
        except OSError:
            return set()
        return inventory

    def _usable_hotwords(self) -> tuple[str, list[str]]:
        """把热词表过滤成**这个模型写得出**的那些行，返回 (临时文件路径, 被跳过的行)。

        为什么不直接把不能用的词从 `config/hotwords.txt` 里删掉：那张表是给人写的，而「哪些
        字能写」取决于当前加载的是哪个模型。换一个字表更全的模型时，被删掉的词不会自己回来。
        所以磁盘上那份保持完整，过滤发生在加载时，被跳过的行**报出来**。

        实测（multi-zh-hans-2023-12-12）：字表只有 1426 个汉字，21 行热词里 8 行含它写不出的
        字 —— 「沃」「酷」「哔」「哩」「抖」「浏」「览」「唤」「纹」。
        """
        if self.hotwords_file is None or not self.hotwords_file.is_file():
            return "", []
        inventory = self._token_inventory()
        try:
            lines = self.hotwords_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return "", []
        keep: list[str] = []
        skipped: list[str] = []
        for line in lines:
            body = line.strip()
            # **`#` 注释不能留。** sherpa-onnx 的热词解析器不支持注释，一行 `#` 会让整个
            # `from_transducer` 抛 `invalid stof argument` —— 而那时的症状是**完全不转写**，
            # 比「热词没生效」严重得多。所以注释在这里被剥掉，说明写在 config/voice.toml。
            if not body or body.startswith("#"):
                continue
            chars = body.split()
            if inventory and any(char not in inventory for char in chars):
                missing = "".join(char for char in chars if char not in inventory)
                skipped.append(f"{''.join(chars)}（模型写不出「{missing}」）")
                continue
            keep.append(" ".join(chars))
        if not keep:
            return "", skipped
        import tempfile  # noqa: PLC0415 - 只在真的用热词时才需要

        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hotwords.txt", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write("\n".join(keep) + "\n")
        return handle.name, skipped

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
            # 热词只在 modified_beam_search 下生效，greedy 会**静默忽略**它们。所以这里由
            # 代码决定解码方式，而不是让人自己记得同时改两个键 —— 后者的失败模式是
            # 「配了热词但一点变化都没有」，而那一句排查起来要先知道这条约束才想得到。
            hotwords = ""
            method = self.decoding_method
            if self.hotwords_file is not None and self.hotwords_file.is_file():
                hotwords, self.hotwords_skipped = self._usable_hotwords()
                if hotwords:
                    method = "modified_beam_search"
            self._recognizer = self._build(sherpa, encoder, decoder, joiner, method, hotwords)
            self.active_decoding = method
            self.active_hotwords = hotwords
        except Exception as exc:  # noqa: BLE001 - reported, never raised here
            # **热词坏了不能把识别器一起带走。** sherpa-onnx 的热词表里一个不合法的行会让
            # 整个 `from_transducer` 抛（实测：文件里放 `#` 注释 → `invalid stof argument`），
            # 而那时的症状是**完全不转写** —— 比「热词没生效」严重得多，也更难联想到热词。
            # 所以带热词失败时退回不带热词再试一次，并把原因记在 `hotwords_error` 里。
            if hotwords:
                self.hotwords_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._recognizer = self._build(
                        sherpa, encoder, decoder, joiner, self.decoding_method, ""
                    )
                    self.active_decoding = self.decoding_method
                    self.active_hotwords = ""
                except Exception as retry_exc:  # noqa: BLE001
                    self._recognizer = None
                    return ProviderStatus(
                        False,
                        str(self.model_dir),
                        {"reason": f"streaming asr load failed: {retry_exc}"},
                    )
            else:
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
                # **热词生效了没有**，这是唯一的读数。`decoding` 会在有热词时自动变成
                # modified_beam_search —— 看到它还是 greedy 就说明热词表没被找到。
                "decoding": self.active_decoding,
                "hotwords": Path(self.active_hotwords).name if self.active_hotwords else "",
                # 热词表坏了时这一行是唯一的线索。空 = 没出问题。
                "hotwords_error": self.hotwords_error,
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
