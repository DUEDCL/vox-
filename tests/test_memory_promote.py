"""不用说「记住」它也记得住 —— 而且不是什么都记。

使用者的要求：「我把我的个人网站告诉他，下次对话他就能直接记住，而不是我说了『给我记住我的
个人网站』他才会记住。」

这里钉两件事，而它们分开是全部的重点：**抽得出来**，和**够不够证据进长期层**。只抽不筛的
助手会把一次口误、一个临时决定固化成它对你的认识 —— Hermes 引的测量说例行记忆保存把短期
污染固化为长期记忆的比例最高到 91%。

证据等级：AUTO（纯函数 + 假 writer / store）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.memory.contract import MemoryRecord
from core.memory.promote import (
    CANDIDATE_KIND,
    CORRECTION,
    EXPLICIT,
    REPEATED,
    Candidate,
    MemoryPromoter,
    extract,
)


# ---------------------------------------------------------------------- 抽取


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我的个人网站是 duchenlin.com", "个人网站：duchenlin.com"),
        ("我的博客是 blog.example.com", "个人网站：blog.example.com"),
        ("我叫杜沉麟", "名字：杜沉麟"),
        ("我住在北京", "所在地：北京"),
        ("我在北京", "所在地：北京"),
        ("我平时用的是 VS Code", "常用的：VS Code"),
        ("我不喜欢很长的回答", "不喜欢：很长的回答"),
        ("我喜欢简短的回答", "偏好：简短的回答"),
        ("我生日是 3 月 5 日", "生日：3 月 5 日"),
        ("我从事软件开发", "职业：软件开发"),
    ],
)
def test_a_first_person_statement_is_extracted(text, expected):
    """**使用者说出来就算**，不需要他再说一句「记住」。这就是他要的那件事。"""
    got = extract(text)

    assert [item.statement for item in got] == [expected]
    assert got[0].channel == EXPLICIT, "一条自述本身就是显式陈述"


@pytest.mark.parametrize(
    "text",
    [
        "我的网站是什么",  # 问句：存下来之后助手会相信他的网站叫「什么」
        "你的网站是什么",  # 第二人称：这不是关于使用者的事实
        "帮我打开网易云音乐",  # 一条请求
        "读一下 README",
        "今天想吃辣的",  # 临时的，不是稳定事实
        "我在写一个语音助手",  # 裸「在」+ 长尾巴 —— 会被抽成「所在地：写一个语音助手」
        "我是杜沉麟",  # 裸「是」—— 会被抽成「职业：杜沉麟」
        "",
    ],
)
def test_things_that_must_not_be_remembered(text):
    """**抽错一条比没抽到贵得多** —— 它看起来是对的，而且会一直影响后面每一轮。

    最后两条是实测抓出来的：裸的「在」和裸的「是」都太松。
    """
    assert extract(text) == []


def test_a_standing_instruction_is_kept_whole():
    """「以后都用中文回我」不匹配任何具体字段，但它就是可复述的那一句。"""
    got = extract("以后都用中文回我")

    assert got[0].statement == "以后都用中文回我"
    assert got[0].kind == "project_rule"
    assert got[0].channel == EXPLICIT


def test_a_correction_is_a_stronger_channel_than_a_plain_statement():
    """使用者纠正了刚才那次 —— 那比第一次陈述更可信，因为他为此专门说了一句话。"""
    got = extract("不是这个，我的网站是 duchenlin.com")

    assert got[0].channel == CORRECTION


def test_one_key_wins_per_sentence():
    """同一句话里同一个字段只留第一条 —— 后面那个多半是同一件事的复述。"""
    got = extract("我叫杜沉麟，我叫小杜")

    assert len(got) == 1


def test_a_transcription_fragment_is_not_a_fact():
    """转写会截断。「我的网站是的」抽出来的值是个语气词，那不是事实。"""
    assert extract("我的网站是的") == []


# ---------------------------------------------------------------------- 闸门


class FakeWriter:
    def __init__(self) -> None:
        self.facts: list[tuple[str, tuple[str, ...]]] = []
        self.candidates: list[str] = []

    def write_fact(self, text, *, tags=(), session_id=None):
        del session_id
        self.facts.append((text, tuple(tags)))
        return "f-1"

    def write_candidate(self, text, *, key="", session_id=None):
        del key, session_id
        self.candidates.append(text)
        return "c-1"


class FakeStore:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def list_records(self, *, scope=None, kind=None, session_id=None, limit=50, newest_first=True):
        del scope, kind, session_id, limit, newest_first
        return tuple(self.rows)


def candidate_row(statement: str, session: str) -> MemoryRecord:
    return MemoryRecord(
        id=f"c-{session}", scope="short", kind=CANDIDATE_KIND, text=statement, session_id=session
    )


def test_an_explicit_statement_goes_straight_to_the_long_layer():
    writer = FakeWriter()
    promoter = MemoryPromoter(writer=writer, store=FakeStore())

    promoted = promoter.observe("我的个人网站是 duchenlin.com", session_id="s1")

    assert [item.statement for item in promoted] == ["个人网站：duchenlin.com"]
    assert writer.facts[0][0] == "个人网站：duchenlin.com"
    assert promoter.promoted == 1


def test_the_promotion_reason_is_stored_so_it_can_be_audited():
    """「这条记忆凭什么在这里」要能回答。一条无从追溯的长期记忆没法审计也没法回滚。"""
    writer = FakeWriter()
    MemoryPromoter(writer=writer, store=FakeStore()).observe("我叫杜沉麟", session_id="s1")

    tags = writer.facts[0][1]
    assert f"channel:{EXPLICIT}" in tags
    assert "about:name" in tags


def test_a_fact_already_known_is_not_written_twice():
    """重复的事实会让召回时同一句话出现三遍，而那三遍会挤掉别的记忆。"""

    class Recaller:
        def facts(self, query, *, limit=None):
            del query, limit
            return (MemoryRecord(id="f", scope="mid", kind="fact", text="名字：杜沉麟"),)

    writer = FakeWriter()
    promoter = MemoryPromoter(writer=writer, recaller=Recaller(), store=FakeStore())

    promoter.observe("我叫杜沉麟", session_id="s1")

    assert writer.facts == []


def test_a_candidate_seen_in_two_sessions_is_promoted():
    """跨会话重复 —— Hermes 那三条通道里的第二条。说过两次的事更可能是真的。"""
    writer = FakeWriter()
    store = FakeStore([candidate_row("偏好：坐在窗边", "s-old")])
    promoter = MemoryPromoter(writer=writer, store=store)
    # 一个不带自述形状的候选：这里直接构造，因为抽取器只产出 explicit 的那些。
    promoted = promoter._decide(
        Candidate(key="like", statement="偏好：坐在窗边", kind="user_preference"),
        session_id="s-new",
    )

    assert promoted is not None and promoted.channel == REPEATED
    assert writer.facts[0][1][0] == f"channel:{REPEATED}"


def test_a_candidate_seen_only_once_is_parked_not_promoted():
    """**宁紧勿松。** 它可能是真的，但它现在还不该改变助手的行为。"""
    writer = FakeWriter()
    promoter = MemoryPromoter(writer=writer, store=FakeStore())

    promoted = promoter._decide(
        Candidate(key="like", statement="偏好：坐在窗边", kind="user_preference"),
        session_id="s1",
    )

    assert promoted is None
    assert writer.facts == []
    assert writer.candidates == ["偏好：坐在窗边"]
    assert promoter.parked == 1


def test_a_broken_store_does_not_take_the_turn_down():
    """记忆是增强不是对话的前提 —— 这一层的每个失败都必须是静默的。"""

    class Broken:
        def write_fact(self, *a, **k):
            raise RuntimeError("disk is full")

    promoter = MemoryPromoter(writer=Broken(), store=FakeStore())

    assert promoter.observe("我叫杜沉麟", session_id="s1") == []
    assert "disk is full" in promoter.last_error


def test_the_runtime_wires_the_promoter_and_logs_what_it_learned():
    """接线本身要被钉住：漏接的症状是「它就是不记得」，而那和抽取器坏了长得一样。"""
    from types import SimpleNamespace

    from vox_plugin.runtime import VoiceRuntime

    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    writer = FakeWriter()
    runtime.promoter = MemoryPromoter(writer=writer, store=FakeStore())
    written: list[str] = []
    runtime.logbook = SimpleNamespace(
        write=lambda source, message, **fields: written.append(message)
    )

    runtime._remember_facts("我的个人网站是 duchenlin.com")

    assert writer.facts[0][0] == "个人网站：duchenlin.com"
    assert any("记住了" in message for message in written), written
