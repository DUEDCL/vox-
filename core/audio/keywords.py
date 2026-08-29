"""唤醒词表：中文进，音素行出。

## 为什么需要一个转换层

`keywords.txt` 的每一行长这样：``n ǐ h ǎo j ūn g ē @你好军哥`` —— 前半是**拼音声母/
韵母**序列，`@` 后面是显示用的原文。模型的 227 个建模单元就是这套音素（`tokens.txt`
里是 ``zh``/``sh``/``ēng``/``iàn`` 这种），所以自定义唤醒词**不能直接写中文**：写中文
的那一行整行匹配不上，表现是「这个词永远唤不醒」而不是一条报错。

## 与 sherpa-onnx 自带工具的关系

``sherpa_onnx.utils.text2token(tokens_type="ppinyin")`` 做同一件事，但它在函数顶部
**无条件** import ``sentencepiece``（只有 bpe 分支用得到），所以调它要为一个用不上的
分支装一个 C++ 扩展。这里只实现 ppinyin 这一条路径，依赖收到 ``pypinyin`` 一个。

正确性不是靠推断：对模型出厂的 8 个词重新生成，**8/8 与出厂文件逐字节一致**，
由 ``tests/test_wake_keywords.py`` 钉住。出厂词表就是训练时用的那份，逐字节一致
意味着这里的音素切分与训练一致。

## 校验为什么落在 token 表上

一个词写得出拼音不等于模型认得它：英文、数字、生僻音的音素可能不在那 227 个
token 里，而 sherpa-onnx 对认不出的 token 是**静默跳过**，结果又是「这个词唤不醒」。
所以 ``check_keyword`` 把生成出来的每个音素拿去比对 ``tokens.txt`` —— 拦在写入之前，
而不是留到用户对着麦克风喊十遍之后。
"""

from __future__ import annotations

from pathlib import Path

#: 唤醒词的最短汉字数。两个字的词（「你好」）在连续语音里误唤醒率高得没法用；
#: 三个字是 sherpa-onnx 文档给的经验下限，出厂词表里最短的也是三个字（「林美丽」）。
MIN_KEYWORD_CHARS = 3

#: 最长汉字数。太长的词说完之前就超过了 KWS 的解码窗口，反而更难命中。
MAX_KEYWORD_CHARS = 10

#: 一份词表里最多几个词。每个词都要在每一块音频上比对，条数直接进 CPU 占用。
MAX_KEYWORDS = 20


class KeywordError(ValueError):
    """词表写不下去的原因，消息是给界面直接显示的。"""


def to_ppinyin(text: str) -> list[str]:
    """一个词 -> 声母/韵母序列。

    ``pypinyin`` 在这里而不是模块顶层 import：装它只为了生成词表，KWS 推理本身用不到
    它，一个跑现成词表的机器不该因为缺这个包就起不来。
    """
    try:
        from pypinyin import pinyin
        from pypinyin.contrib.tone_convert import to_finals_tone, to_initials
    except ImportError as exc:  # pragma: no cover - 环境缺包才走到
        raise KeywordError(
            "生成唤醒词需要 pypinyin：.venv\\Scripts\\python.exe -m pip install pypinyin"
        ) from exc

    out: list[str] = []
    for syllable in (item[0] for item in pinyin(text)):
        initial = to_initials(syllable, strict=False)
        final = to_finals_tone(syllable, strict=False)
        if not initial and not final:
            # 拼不出声母韵母的（英文、数字、符号）原样留下,让 token 校验去拦它 ——
            # 在这里丢掉会让「词写了但少了一个字」变成静默的。
            out.append(syllable)
            continue
        if initial:
            out.append(initial)
        if final:
            out.append(final)
    return out


def load_tokens(model_dir: str | Path) -> frozenset[str]:
    """模型认识的建模单元。校验用，读不到就返回空集（视作「无法校验」）。"""
    path = Path(model_dir) / "tokens.txt"
    if not path.is_file():
        return frozenset()
    tokens: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            tokens.add(parts[0])
    return frozenset(tokens)


def _is_han(text: str) -> bool:
    """全是 CJK 统一汉字。范围取 [U+4E00, U+9FFF]，与 sherpa-onnx 自己用的一致。"""
    return bool(text) and all("一" <= char <= "鿿" for char in text)


def check_keyword(word: str, tokens: frozenset[str] = frozenset()) -> list[str]:
    """校验一个词并返回它的音素。不合格就抛 ``KeywordError``。

    ``tokens`` 为空时跳过 token 比对而不是放行全部：读不到 ``tokens.txt`` 是环境问题，
    把它变成「词表写不了」会让一台缺文件的机器连改词都改不成。长度和汉字这两道仍然查。
    """
    text = word.strip()
    if not text:
        raise KeywordError("唤醒词不能为空")
    if not _is_han(text):
        raise KeywordError(
            f"「{text}」含非汉字 —— 这个模型的建模单元是汉语拼音的声母/韵母，"
            "英文、数字、标点都拼不出它认识的音"
        )
    if len(text) < MIN_KEYWORD_CHARS:
        raise KeywordError(
            f"「{text}」只有 {len(text)} 个字，太短 —— 少于 {MIN_KEYWORD_CHARS} 个字"
            "在连续说话里误唤醒会非常频繁"
        )
    if len(text) > MAX_KEYWORD_CHARS:
        raise KeywordError(f"「{text}」超过 {MAX_KEYWORD_CHARS} 个字，说完之前就出解码窗口了")
    phonemes = to_ppinyin(text)
    if tokens:
        unknown = sorted({p for p in phonemes if p not in tokens})
        if unknown:
            raise KeywordError(
                f"「{text}」拼出的音素 {unknown} 不在模型的 token 表里 —— "
                "sherpa-onnx 对认不出的 token 是静默跳过，留着它等于留一个永远唤不醒的词"
            )
    return phonemes


def keyword_line(word: str, tokens: frozenset[str] = frozenset()) -> str:
    """一个中文词 -> 词表里的一行（``音素… @原文``）。"""
    return " ".join(check_keyword(word, tokens)) + " @" + word.strip()


def parse_keywords(text: str) -> list[str]:
    """词表文本 -> 中文词列表。

    读的是 ``@`` 后面那半：音素那半是派生数据，把它当真相会让一份手改过原文、
    忘了改音素的文件显示成「改过了」，而实际生效的是旧音。没有 ``@`` 的行整行取用
    （出厂的 ``keywords_raw.txt`` 就是这个形状）。
    """
    words: list[str] = []
    for line in text.splitlines():
        row = line.strip()
        if not row or row.startswith("#"):
            continue
        word = row.rsplit("@", 1)[-1].strip() if "@" in row else row
        if word:
            words.append(word)
    return words


def render_keywords(words: list[str], tokens: frozenset[str] = frozenset()) -> str:
    """中文词列表 -> 整份词表文件的内容。全部词先过校验，一个不合格就不写。

    要么整份换掉，要么一行都不动：写了一半的词表比旧的那份更难查 —— 唤醒行为会变成
    「有的词认、有的不认」，而用户以为自己提交的是一整份。
    """
    seen: list[str] = []
    for word in words:
        text = word.strip()
        if not text:
            continue
        if text in seen:
            raise KeywordError(f"「{text}」重复了 —— 同一个词写两遍不会更容易唤醒")
        seen.append(text)
    if not seen:
        raise KeywordError("词表不能是空的 —— 没有词等于唤不醒，那不如关掉唤醒")
    if len(seen) > MAX_KEYWORDS:
        raise KeywordError(f"最多 {MAX_KEYWORDS} 个词，给了 {len(seen)} 个（每个词都进每一块音频的比对）")
    return "".join(keyword_line(word, tokens) + "\n" for word in seen)


def read_keywords(path: str | Path) -> list[str]:
    """读一份词表里的中文词。文件不存在返回空列表。"""
    file = Path(path)
    if not file.is_file():
        return []
    return parse_keywords(file.read_text(encoding="utf-8"))


def write_keywords(path: str | Path, words: list[str], model_dir: str | Path) -> list[str]:
    """写一份词表，返回落盘的中文词。父目录不存在就建。"""
    tokens = load_tokens(model_dir)
    body = render_keywords(words, tokens)
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body, encoding="utf-8")
    return parse_keywords(body)


__all__ = [
    "MAX_KEYWORDS",
    "MAX_KEYWORD_CHARS",
    "MIN_KEYWORD_CHARS",
    "KeywordError",
    "check_keyword",
    "keyword_line",
    "load_tokens",
    "parse_keywords",
    "read_keywords",
    "render_keywords",
    "to_ppinyin",
    "write_keywords",
]
