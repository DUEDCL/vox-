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

## 落盘前要做两件后处理

**归一化。** 同一个 MeloTTS 模型对不同短句给出的音量差得很远 —— 本机实测（同一次进程、
同一 speaker/speed）「说吧我听着」peak 0.091，「你说吧」peak 0.239，2.6 倍；更早那批里
「咋了」是 0.035、「请说」0.026，而「嗯」直接是 **0.000**（合出来是一段静音）。一个音量
在各次唤醒之间差 4 倍的确认音听起来像坏了，而不是像随机。所以每个文件按峰值归一到同一
目标，播放侧就不必知道这件事。

**尾部淡出 + 补静音。** 模型给的波形结尾没有余量，播完最后一个样本就是硬边。归一化之后
这条更明显（音量抬高了 7 倍，硬边也抬高了 7 倍），表现就是「戛然而止、不自然」。做法是
最后 25 ms 走一个余弦淡出，再补 120 ms 静音；开头补 20 ms，免得设备在第一个样本上爆音。

## 哪些句子能用是量出来的，不是挑好听的

判据：把合成出来的音频**喂回本项目的 ASR**，看识别成什么。识别不回原文说明音素本身就不对，
那么人也听不清。实测（每句合成 3 次，归一化后再喂，排除「太轻所以识别不出」这个混杂因素）：

| 句子 | 回读 | 结论 |
|---|---|---|
| 你说吧 / 我听着呢 / 什么事呀 / 说吧我听着 | **3/3** | 采用 |
| 我在呢 | 0/3（听成「我在」） | 只丢了句末轻声的「呢」，是流式端点的弱点而不是合成缺陷；想用可以放回来 |
| 我在这儿 | 0/3（听成「我在这」） | 同上 |
| 嗯哼 | 0/3 —— 听成 **「你好」** | **弃用**。一句唤醒确认音说出来是「你好」就是坏的 |
| 嗯 | 合成结果 peak **0.000** | 弃用。整段静音 |
| 咋了 / 请说 / 你说 / 请讲 | 0/3，peak 0.026–0.066 | 弃用。两字的非词汇性叹词这个模型做不好 |

规律很清楚：**三字以上、有实词内容的短句可靠，一到两字的叹词不可靠。** 这个模型练的是朗读
语料，「嗯」「咋」这类在里面几乎不出现。

## 随机而不是轮转

轮转会让人听出顺序，而听出顺序之后它就变成了一个计数器而不是一个应答。随机里可能连续
两次同一句，那是可接受的代价。
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Any, Sequence

#: 分隔符：中英文逗号、分号、竖线都收。用户从哪抄来的就用哪种，挑一个当唯一合法的
#: 会把「为什么我配的第二句没生效」变成猜谜。
_SEPARATORS = "，,；;|、"

#: 出厂的四句。两条硬要求：**短**（这一声要在人开口说下一句之前放完）、**回读得回原文**
#: （见模块头那张表 —— 上一版的「嗯哼」合出来被识别成「你好」，「咋了」只有 0.035 的峰值）。
DEFAULT_ACKS = "你说吧，我听着呢，什么事呀，说吧我听着"

#: 归一化目标峰值。0.7 而不是 1.0：留 3 dB 余量，避免播放链上任何增益把它推到削波。
ACK_TARGET_PEAK = 0.7

#: 头尾补的静音与尾部淡出长度（秒）。淡出治「硬边」，补静音治「设备在最后一个样本上截断」。
ACK_LEAD_S = 0.02
ACK_TAIL_S = 0.12
ACK_FADE_S = 0.025

#: 播确认音时输入侧静音窗的上限与尾巴（秒）。见 `core/audio/capture.py` 的 `mute_for`。
#:
#: 上限只是保险丝：真正决定窗口长度的是「播放（阻塞）返回之后再压一个 ``ACK_MUTE_TAIL_S``」。
#: 5 秒足够盖住最长那句（实测 1.56 s）加上首次合成落盘的时间，而万一播放线程死掉，
#: 系统最多聋 5 秒而不是永远。
ACK_MUTE_CAP_S = 5.0
#: 播完之后再压一小段：扬声器的尾音、房间残响、以及声卡输出缓冲里还没走完的样本。
ACK_MUTE_TAIL_S = 0.25


def parse_acks(raw: str) -> tuple[str, ...]:
    """配置里那一行 -> 一组短句。空的返回空元组（= 关掉这个功能）。"""
    text = str(raw or "")
    for sep in _SEPARATORS[1:]:
        text = text.replace(sep, _SEPARATORS[0])
    return tuple(part.strip() for part in text.split(_SEPARATORS[0]) if part.strip())


def cache_name(text: str, *, voice: str = "", instruction: str = "") -> str:
    """一句话 -> 缓存文件名。哈希而不是原文：原文里有标点和空格，落到文件名上要转义，
    而转义规则本身又成了一件要对齐的事。

    ``voice`` 与 ``instruction`` 都进哈希，**两个都是必需的**：2026-08-29 起合成可以走
    本机 VITS 也可以走云端（`tts.provider` / `tts.voice` / `tts.instruction`）。缓存最早
    只按文本算哈希 —— 换了音色文件名不变，于是播出来仍然是上一把声音；`instruction`
    是同一个坑再深一层：**音色没变、语气变了，文件名照样不变**，表现是「把语气调温柔了
    但那四句还是原来的腔」。

    两个都为空时保持与最早的缓存同名，所以本机那条路的既有文件不会作废。
    """
    parts = [part for part in (voice, instruction) if part]
    seed = "\x00".join([*parts, text]) if parts else text
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"ack-{digest}.wav"


def polish(samples: Any, sample_rate: int, *, target_peak: float = ACK_TARGET_PEAK) -> Any:
    """把一段合成结果修成能直接播的确认音：归一化 + 尾部淡出 + 头尾补静音。

    在**落盘前**做而不是播放时做：这几个文件是缓存，一次算好比每次播放都算一遍便宜，
    而且播放侧因此不需要知道确认音和别的音频有什么不同。

    全静音的输入原样返回（不做除零的归一化）—— 那种情况本身是个应该被看见的失败，
    见模块头那张表里的「嗯」。
    """
    import numpy as np

    audio = np.asarray(samples, dtype="float32").reshape(-1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1e-6:
        audio = audio * (float(target_peak) / peak)

    fade = min(int(sample_rate * ACK_FADE_S), audio.size)
    if fade > 1:
        # 余弦淡出而不是线性：线性在起点有个斜率突变，仍然听得出一个「咔」。
        ramp = (1.0 + np.cos(np.linspace(0.0, math.pi, fade))) * 0.5
        audio = audio.copy()
        audio[-fade:] *= ramp.astype("float32")

    lead = np.zeros(int(sample_rate * ACK_LEAD_S), dtype="float32")
    tail = np.zeros(int(sample_rate * ACK_TAIL_S), dtype="float32")
    return np.concatenate([lead, audio, tail])


class AckLibrary:
    """预生成的唤醒确认音。合成器和播放后端都是注入的，所以测试里两个都能是假的。"""

    def __init__(
        self,
        texts: Sequence[str],
        *,
        tts: Any = None,
        cache_dir: str | Path | None = None,
        playback: Any = None,
        voice: str = "",
        instruction: str = "",
    ) -> None:
        self.texts = tuple(texts)
        self.tts = tts
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.playback = playback
        #: 进缓存文件名的音色与语气标识。换任一个都必须换文件名 —— 见 cache_name 的注释。
        self.voice = str(voice or "")
        self.instruction = str(instruction or "")
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
            path = self.cache_dir / cache_name(
                text, voice=self.voice, instruction=self.instruction
            )
            if path.is_file():
                ready.append(path)
                continue
            if self.tts is None:
                continue
            try:
                audio = self.tts.synthesize(text)
                _write_wav(path, polish(audio.samples, audio.sample_rate), audio.sample_rate)
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


__all__ = [
    "ACK_FADE_S",
    "ACK_LEAD_S",
    "ACK_MUTE_CAP_S",
    "ACK_MUTE_TAIL_S",
    "ACK_TAIL_S",
    "ACK_TARGET_PEAK",
    "DEFAULT_ACKS",
    "AckLibrary",
    "cache_name",
    "parse_acks",
    "polish",
]
