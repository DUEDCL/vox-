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


def test_the_voice_and_the_instruction_are_both_in_the_cache_name():
    """换音色、改语气都必须换文件名。

    这是同一个坑的两层。缓存最早只按文本算哈希 —— 换了音色文件名不变，播出来还是上一把
    声音；`instruction` 更隐蔽：**音色没变、只是语气变了**，文件名照样不变，表现是
    「把语气调温柔了但那四句还是原来的腔」。两个都为空时保持与最早的缓存同名，所以本机
    那条路（VITS 没有 voice / instruction 这两个概念）的既有文件不会作废。
    """
    plain = cache_name("你说吧")
    assert cache_name("你说吧", voice="", instruction="") == plain
    voiced = cache_name("你说吧", voice="longanhuan_v3.6")
    told = cache_name("你说吧", voice="longanhuan_v3.6", instruction="用温柔的语气说")
    assert len({plain, voiced, told}) == 3


def test_changing_the_instruction_regenerates_the_files(tmp_path):
    """端到端的那一条：同一句话、同一音色，只改 instruction，就该是另一个文件。"""
    tts = FakeTts()
    gentle = AckLibrary(("你说吧",), tts=tts, cache_dir=tmp_path, voice="v1", instruction="温柔")
    brisk = AckLibrary(("你说吧",), tts=tts, cache_dir=tmp_path, voice="v1", instruction="干脆")

    first = gentle.ensure()
    second = brisk.ensure()

    assert first[0].name != second[0].name
    assert tts.calls == ["你说吧", "你说吧"], "改了语气必须重新合成，不能复用旧文件"


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


# ------------------------------------------------------------------ polish()
#
# 这一组钉死的是使用者 2026-08-29 报的两件事:「预设回复语音太过于戛然而止了不是很自然」
# 和「有无效语音提示回复」。两条都量出来了:
#
# - 音量:同一个 MeloTTS 模型对不同短句给出 peak 0.026-0.239,近 10 倍。一个音量在各次
#   唤醒之间差这么多的确认音听起来像坏了。
# - 硬边:模型给的波形结尾没有余量,播完最后一个样本就断。归一化之后这条更明显。
#
# 「无效」那一条不是 polish 能修的,是选词:旧的「嗯哼」合出来被 ASR 识别成「你好」,
# 「嗯」合出来 peak=0.000(整段静音)。所以 DEFAULT_ACKS 换成了回读 3/3 的四句。


def test_polish_normalises_every_clip_to_the_same_peak():
    """归一化的意义就是「不同句子听起来一样响」,所以断言的是同一个数字。"""
    from core.audio.acks import ACK_TARGET_PEAK, polish

    quiet = np.full(1600, 0.035, dtype=np.float32)
    loud = np.full(1600, 0.239, dtype=np.float32)
    a = polish(quiet, 16000)
    b = polish(loud, 16000)
    assert float(np.max(np.abs(a))) == pytest.approx(ACK_TARGET_PEAK, abs=1e-4)
    assert float(np.max(np.abs(b))) == pytest.approx(ACK_TARGET_PEAK, abs=1e-4)


def test_polish_leaves_a_silent_tail_so_it_does_not_stop_dead():
    """尾部必须有静音:设备在最后一个样本上截断听起来就是「戛然而止」。"""
    from core.audio.acks import ACK_TAIL_S, polish

    out = polish(np.full(1600, 0.2, dtype=np.float32), 16000)
    tail = out[-int(16000 * ACK_TAIL_S) :]
    assert float(np.max(np.abs(tail))) == 0.0


def test_polish_fades_the_end_instead_of_cutting_it():
    """淡出:结尾前的那一小段必须是递减的,而不是一路满幅然后突然归零。"""
    from core.audio.acks import ACK_FADE_S, ACK_TAIL_S, polish

    out = polish(np.full(16000, 0.2, dtype=np.float32), 16000)
    end = len(out) - int(16000 * ACK_TAIL_S)
    fade = out[end - int(16000 * ACK_FADE_S) : end]
    assert fade[0] > fade[len(fade) // 2] > fade[-1]


def test_polish_pads_the_head_so_the_device_does_not_pop():
    from core.audio.acks import ACK_LEAD_S, polish

    out = polish(np.full(1600, 0.2, dtype=np.float32), 16000)
    head = out[: int(16000 * ACK_LEAD_S)]
    assert float(np.max(np.abs(head))) == 0.0


def test_polish_does_not_divide_by_zero_on_silence():
    """全静音原样通过(只补头尾)。那种情况本身是个该被看见的失败,不是该被放大的信号 ——
    实测「嗯」这个字合出来就是 peak 0.000。"""
    from core.audio.acks import polish

    out = polish(np.zeros(1600, dtype=np.float32), 16000)
    assert float(np.max(np.abs(out))) == 0.0
    assert len(out) > 1600  # 头尾静音仍然补上了


def test_the_shipped_acks_are_all_at_least_three_characters():
    """一到两字的叹词这个 TTS 模型做不好 —— 实测「嗯哼」→「你好」、「嗯」→ 静音、
    「咋了」peak 0.035。出厂词表不该再含那一类。"""
    for text in parse_acks(DEFAULT_ACKS):
        assert len(text) >= 3, f"{text!r} 太短,这个模型做不好这种长度"
