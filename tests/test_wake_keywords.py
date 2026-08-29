"""唤醒词表：中文 -> 音素行，以及拦在写入之前的那几道校验。

**为什么第一条测试是「逐字节复现出厂词表」**：模型出厂的 `keywords.txt` 就是训练时用的
那一份，所以它是这里唯一的真值。逐字节一致意味着 `to_ppinyin` 的音素切分与训练一致 ——
换成断言「生成的行看起来像拼音」的话，一个把 `ǎo` 切成 `ǎ o` 的实现照样能过，而它产出的
词永远唤不醒。

其余的测试都在拦「写得出但唤不醒」这一类：非汉字、太短、音素不在 token 表里。这三种在
sherpa-onnx 那边都是**静默跳过**，所以必须在写入之前拦掉。

证据等级：AUTO。真的对着麦克风喊一个自定义词是 REAL-MIC。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.audio.config import model_paths
from core.audio.keywords import (
    MAX_KEYWORDS,
    MAX_KEYWORD_CHARS,
    MIN_KEYWORD_CHARS,
    KeywordError,
    check_keyword,
    keyword_line,
    load_tokens,
    parse_keywords,
    read_keywords,
    render_keywords,
    to_ppinyin,
    write_keywords,
)

MODEL_DIR = model_paths()["kws_dir"]
SHIPPED = MODEL_DIR / "keywords.txt"

needs_model = pytest.mark.skipif(
    not SHIPPED.is_file(), reason="KWS model (and its keywords.txt) is not present"
)


@pytest.fixture
def tokens() -> frozenset[str]:
    return load_tokens(MODEL_DIR)


@needs_model
def test_the_shipped_table_is_reproduced_byte_for_byte(tokens):
    """The one assertion that says the phoneme split matches training.

    A weaker check ("the line looks like pinyin") would pass an implementation that
    splits ``ǎo`` into ``ǎ o``, and every keyword it wrote would be unwakeable.
    """
    words = read_keywords(SHIPPED)

    assert len(words) == 8
    assert render_keywords(words, tokens) == SHIPPED.read_text(encoding="utf-8")


@needs_model
def test_every_shipped_word_is_made_of_tokens_the_model_knows(tokens):
    """Sanity on the fixture itself: if this fails, ``tokens.txt`` and
    ``keywords.txt`` disagree and no conclusion below means anything."""
    for word in read_keywords(SHIPPED):
        assert all(phoneme in tokens for phoneme in check_keyword(word, tokens))


def test_a_line_carries_the_original_text_after_an_at_sign():
    line = keyword_line("小沃小沃")

    phonemes, _, word = line.rpartition("@")
    assert word == "小沃小沃"
    assert phonemes.strip().split() == to_ppinyin("小沃小沃")


@pytest.mark.parametrize(
    "word, message",
    [
        ("hello", "非汉字"),
        ("小沃 hi", "非汉字"),
        ("小沃1", "非汉字"),
        ("你好", "太短"),
        ("一二三四五六七八九十十一", "字"),
        ("", "不能为空"),
    ],
)
def test_words_that_would_never_wake_are_refused(word, message):
    with pytest.raises(KeywordError, match=message):
        check_keyword(word)


@needs_model
def test_a_word_whose_phonemes_are_not_in_the_token_table_is_refused(tokens):
    """sherpa-onnx skips tokens it does not know **silently**, so a word built from
    them is a keyword that exists in the file and never fires."""
    fake = frozenset({"n", "ǐ"})
    with pytest.raises(KeywordError, match="token 表"):
        check_keyword("小沃小沃", fake)
    # ...and the same word passes against the real table.
    assert check_keyword("小沃小沃", tokens)


def test_parse_reads_the_original_text_not_the_phonemes():
    """The phoneme half is derived data. Treating it as the truth would show a file
    whose text was hand-edited (and phonemes were not) as "changed", while the audio
    still matches the old sound."""
    text = "n ǐ h ǎo w èn w èn @你好问问\n# 注释\n\nx iǎo w ò x iǎo w ò @小沃小沃\n"

    assert parse_keywords(text) == ["你好问问", "小沃小沃"]


def test_a_raw_table_without_at_signs_is_read_too():
    """``keywords_raw.txt`` ships in exactly this shape."""
    assert parse_keywords("你好问问\n小艺小艺\n") == ["你好问问", "小艺小艺"]


def test_a_duplicate_is_refused_rather_than_deduplicated():
    """Silently dropping it would report a count the user did not ask for; the same
    word twice does not make it easier to wake, so saying so is the useful answer."""
    with pytest.raises(KeywordError, match="重复"):
        render_keywords(["小沃小沃", "小沃小沃"])


def test_an_empty_table_is_refused():
    with pytest.raises(KeywordError, match="不能是空的"):
        render_keywords([])


def test_too_many_words_are_refused():
    with pytest.raises(KeywordError, match=str(MAX_KEYWORDS)):
        render_keywords([f"沃克第{n}号" for n in range(MAX_KEYWORDS + 1)])


@needs_model
def test_a_write_then_read_round_trips(tmp_path):
    path = tmp_path / "keywords.txt"

    written = write_keywords(path, ["小沃小沃", "你好沃克"], MODEL_DIR)

    assert written == ["小沃小沃", "你好沃克"]
    assert read_keywords(path) == ["小沃小沃", "你好沃克"]
    assert path.read_text(encoding="utf-8").count("\n") == 2


@needs_model
def test_one_bad_word_leaves_the_old_table_untouched(tmp_path):
    """All or nothing: a half-written table is harder to diagnose than the old one,
    because waking becomes "some words work" while the user believes they submitted
    a whole list."""
    path = tmp_path / "keywords.txt"
    write_keywords(path, ["小沃小沃"], MODEL_DIR)
    before = path.read_bytes()

    with pytest.raises(KeywordError):
        write_keywords(path, ["你好沃克", "nope"], MODEL_DIR)

    assert path.read_bytes() == before


def test_missing_file_reads_as_no_words(tmp_path):
    assert read_keywords(tmp_path / "absent.txt") == []


def test_a_missing_token_table_disables_the_token_check_but_not_the_others(tmp_path):
    """Unreadable ``tokens.txt`` is an environment problem; turning it into "you
    cannot edit keywords at all" would leave a machine unable to fix itself."""
    assert load_tokens(tmp_path) == frozenset()
    assert check_keyword("小沃小沃", frozenset())
    with pytest.raises(KeywordError, match="太短"):
        check_keyword("你好", frozenset())


def test_the_limits_are_the_ones_the_console_advertises():
    """The page shows these three numbers; a drift between them and the checker would
    make the form reject what it told the user was allowed."""
    assert MIN_KEYWORD_CHARS == 3
    assert MAX_KEYWORD_CHARS == 10
    assert MAX_KEYWORDS == 20
