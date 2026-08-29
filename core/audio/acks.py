"""唤醒确认音：命中之后先应一声，再开始听。

## 为什么需要它

唤醒球从隐藏到显示要几百毫秒（窗口显示 + 前端上报布局 + Rust 侧定位），而人说完唤醒词
就会接着说下一句。中间那段没有任何反馈的空白会让人**重复喊唤醒词** —— 而重复喊的第二遍
会落进已经开着的识别器，变成请求文本的一部分。一声「嗯哼」把这段空白填掉。

## 为什么预生成

本机实测一句话合成要 500–900 ms（MeloTTS）。唤醒后再合成等于把那段空白拉长一倍，而这
几句是固定的，没有理由每次重算。第一次用到时合成并落盘，之后直接播 wav。

缓存落在 ``.vox/acks/``（gitignored）而不是版本库：它是从配置里那行文本派生的，文本改了
缓存就该失效 —— 文件名带文本哈希正是为此，改一个字就是另一个文件，旧的自然不再被用到。

## 随机而不是轮转

轮转会让人听出顺序，而听出顺序之后它就变成了一个计数器而不是一个应答。随机里可能连续
两次同一句，那是可接受的代价。
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any, Sequence

#: 分隔符：中英文逗号、分号、竖线都收。用户从哪抄来的就用哪种，挑一个当唯一合法的
#: 会把「为什么我配的第二句没生效」变成猜谜。
_SEPARATORS = "，,；;|、"

#: 出厂的四句。短是硬要求：这一声要在人开口说下一句之前放完。
DEFAULT_ACKS = "嗯哼，我在呢，咋了，有什么事吗"


def parse_acks(raw: str) -> tuple[str, ...]:
    """配置里那一行 -> 一组短句。空的返回空元组（= 关掉这个功能）。"""
    text = str(raw or "")
    for sep in _SEPARATORS[1:]:
        text = text.replace(sep, _SEPARATORS[0])
    return tuple(part.strip() for part in text.split(_SEPARATORS[0]) if part.strip())


def cache_name(text: str) -> str:
    """一句话 -> 缓存文件名。哈希而不是原文：原文里有标点和空格，落到文件名上要转义，
    而转义规则本身又成了一件要对齐的事。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"ack-{digest}.wav"


class AckLibrary:
    """预生成的唤醒确认音。合成器和播放后端都是注入的，所以测试里两个都能是假的。"""

    def __init__(
        self,
        texts: Sequence[str],
        *,
        tts: Any = None,
        cache_dir: str | Path | None = None,
        playback: Any = None,
    ) -> None:
        self.texts = tuple(texts)
        self.tts = tts
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.playback = playback
        #: 生成失败的句子。**不抛异常**：应答音是体验增强，一句合成不出来不该让唤醒失败。
        self.failed: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return bool(self.texts) and self.cache_dir is not None

    def ensure(self) -> list[Path]:
        """把还没落盘的合成出来，返回全部可用的 wav 路径。

        缺 TTS 时只返回已经在缓存里的：上一次跑生成过的这次照样能用，而「模型这次没加载
        起来」不该让已经有的应答音也消失。
        """
        if self.cache_dir is None:
            return []
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        ready: list[Path] = []
        for text in self.texts:
            path = self.cache_dir / cache_name(text)
            if path.is_file():
                ready.append(path)
                continue
            if self.tts is None:
                continue
            try:
                audio = self.tts.synthesize(text)
                _write_wav(path, audio.samples, audio.sample_rate)
            except Exception as exc:  # noqa: BLE001 - 增强功能，失败只记录
                self.failed[text] = f"{type(exc).__name__}: {exc}"
                continue
            ready.append(path)
        return ready

    def play(self, *, rng: random.Random | None = None) -> str:
        """随机播一句，返回播的是哪一句（空字符串 = 什么都没播）。

        整个方法不抛：调用点在唤醒的关键路径上，而一句应答音放不出来绝不能让唤醒失败。
        """
        ready = self.ensure()
        if not ready:
            return ""
        chooser = rng or random
        path = chooser.choice(ready)
        try:
            samples, sample_rate = _read_wav(path)
            player = self.playback if self.playback is not None else _default_playback()
            player.play(samples, sample_rate)
        except Exception as exc:  # noqa: BLE001
            self.failed[path.name] = f"{type(exc).__name__}: {exc}"
            return ""
        return path.name

    def describe(self) -> dict[str, Any]:
        """给控制台看的状态。不含音频，只含数量和失败原因。"""
        ready = [path.name for path in self.ensure()]
        return {
            "texts": list(self.texts),
            "cached": ready,
            "cache_dir": str(self.cache_dir) if self.cache_dir else "",
            "failed": dict(self.failed),
        }


def _write_wav(path: Path, samples: Any, sample_rate: int) -> None:
    import soundfile as sf

    sf.write(str(path), samples, sample_rate)


def _read_wav(path: Path) -> tuple[Any, int]:
    import soundfile as sf

    samples, sample_rate = sf.read(str(path), dtype="float32")
    return samples, int(sample_rate)


def _default_playback() -> Any:
    from core.audio.playback import SounddevicePlayback

    return SounddevicePlayback()


__all__ = ["DEFAULT_ACKS", "AckLibrary", "cache_name", "parse_acks"]
