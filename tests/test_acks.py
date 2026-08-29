"""唤醒确认音：文本解析、缓存命名、预生成、随机播放。

这个功能存在的理由是一段几百毫秒的空白：唤醒球从隐藏到显示要时间，而人说完唤醒词就会
接着说下一句。没有反馈的话他会重复喊 —— 而重复的第二遍会落进已经开着的识别器。

证据等级：AUTO（合成器和播放后端都是假的）。真的听到那一声是 REAL。
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pytest

from core.audio.acks import DEFAULT_ACKS, AckLibrary, cache_name, parse_acks


class FakeAudio:
    def __init__(self, seconds: float = 0.5, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.samples = np.zeros(int(seconds * sample_rate), dtype=np.float32)
        self.elapsed_ms = 1


class FakeTts:
    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def synthesize(self, text: str, **kwargs):
        del kwargs
        self.calls.append(text)
        if text == self.fail_on:
            raise RuntimeError("boom")
        return FakeAudio()


class FakePlayback:
    def __init__(self) -> None:
        self.played: list[int] = []

    def play(self, samples, sample_rate) -> None:
        self.played.append(int(sample_rate))

    def stop(self) -> None:
        pass


@pytest.mark.parametrize("sep", ["，", ",", "；", ";", "|", "、"])
def test_every_separator_is_accepted(sep):
    """用户从哪抄来的就用哪种分隔符。挑一个当唯一合法的会把「为什么第二句没生效」
    变成猜谜。"""
    assert parse_acks(f"嗯哼{sep}我在呢") == ("嗯哼", "我在呢")


def test_blank_entries_and_padding_are_dropped():
    assert parse_acks("  嗯哼 ，， 我在呢 ,") == ("嗯哼", "我在呢")


def test_an_empty_setting_turns_the_feature_off():
    assert parse_acks("") == ()
    assert parse_acks("  ，； ") == ()


def test_the_shipped_default_parses_to_four_short_lines():
    lines = parse_acks(DEFAULT_ACKS)
    assert len(lines) == 4
    # 短是硬要求：这一声要在人开口说下一句之前放完。
    assert all(len(line) <= 6 for line in lines)


def test_the_cache_name_is_derived_from_the_text():
    """哈希而不是原文：原文带标点和空格，落到文件名上要转义，而转义规则本身又成了
    一件要对齐的事。改一个字就是另一个文件，所以配置改了旧缓存自然不再被用到。"""
    first = cache_name("嗯哼")
    assert first.startswith("ack-") and first.endswith(".wav")
    assert cache_name("嗯哼") == first
    assert cache_name("嗯哼。") != first


def test_ensure_synthesises_each_line_once_and_caches_it(tmp_path):
    tts = FakeTts()
    library = AckLibrary(("嗯哼", "我在呢"), tts=tts, cache_dir=tmp_path)

    first = library.ensure()
    second = library.ensure()

    assert len(first) == 2
    assert first == second
    # 第二次不再合成：本机一句要 500–900 ms，而这几句是固定的。
    assert tts.calls == ["嗯哼", "我在呢"]
    assert all(path.is_file() for path in first)


def test_one_line_that_cannot_be_synthesised_does_not_take_the_rest_down(tmp_path):
    """应答音是体验增强。一句合成不出来只该少那一句，不该让唤醒这条路失败。"""
    tts = FakeTts(fail_on="我在呢")
    library = AckLibrary(("嗯哼", "我在呢", "咋了"), tts=tts, cache_dir=tmp_path)

    ready = library.ensure()

    assert len(ready) == 2
    assert "我在呢" in library.failed


def test_a_missing_tts_still_plays_what_was_cached_last_time(tmp_path):
    """「模型这次没加载起来」不该让已经落盘的应答音也消失。"""
    AckLibrary(("嗯哼",), tts=FakeTts(), cache_dir=tmp_path).ensure()

    later = AckLibrary(("嗯哼", "我在呢"), tts=None, cache_dir=tmp_path)

    assert len(later.ensure()) == 1


def test_play_picks_one_and_reports_which(tmp_path):
    playback = FakePlayback()
    library = AckLibrary(
        ("嗯哼", "我在呢"), tts=FakeTts(), cache_dir=tmp_path, playback=playback
    )

    played = library.play(rng=random.Random(0))

    assert played.startswith("ack-")
    assert playback.played == [16000]


def test_play_is_silent_and_safe_when_there_is_nothing_to_play(tmp_path):
    """调用点在唤醒的关键路径上，所以这个方法不抛 —— 什么都没有就什么都不做。"""
    assert AckLibrary((), cache_dir=tmp_path).play() == ""
    assert AckLibrary(("嗯哼",), tts=None, cache_dir=tmp_path).play() == ""
    assert AckLibrary(("嗯哼",), tts=FakeTts(), cache_dir=None).play() == ""


def test_a_playback_failure_is_recorded_not_raised(tmp_path):
    class Broken:
        def play(self, samples, sample_rate):
            raise OSError("no audio device")

    library = AckLibrary(
        ("嗯哼",), tts=FakeTts(), cache_dir=tmp_path, playback=Broken()
    )

    assert library.play() == ""
    assert library.failed


def test_describe_reports_counts_and_never_audio(tmp_path):
    library = AckLibrary(("嗯哼",), tts=FakeTts(), cache_dir=tmp_path)

    view = library.describe()

    assert view["texts"] == ["嗯哼"]
    assert len(view["cached"]) == 1
    assert view["cache_dir"] == str(tmp_path)
    assert "samples" not in view
