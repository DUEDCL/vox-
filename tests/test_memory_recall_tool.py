"""`memory.recall` —— agent 主动翻记忆。

这个工具是「有记忆的助手」与「问答机器」的分界线：在它之前记忆只以**被动召回**到达模型
（`Dispatcher._recall_context()` 按当前这句话去查），于是「我上次说想买的那个东西叫什么」
必然答不了 —— 使用者这句话里没有那个东西的名字。

所以这里的每一条钉的都是同一件事的一面：**查得到要念得出，查不到不是失败，个人数据不进审计。**

Evidence level: AUTO（假 recaller，不开数据库、不打网络）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import open_tools
from core.tools.contract import ToolRequest
from core.tools.memory_recall import MAX_LIMIT, SCOPES, MemoryRecallTool


@dataclass(frozen=True)
class Row:
    id: str
    scope: str
    kind: str
    text: str
    created_at: str = "2026-09-01T14:30:00"
    tags: tuple[str, ...] = ()


class FakeRecaller:
    """按 scope 返回预置的行。记下被问过哪几层 —— `long` 一次都不该出现。"""

    def __init__(self, rows=None, *, boom: bool = False) -> None:
        self.rows = rows or {}
        self.boom = boom
        self.asked: list[tuple[str, str, int]] = []

    def recall(self, query, *, scope=None, kind=None, limit=None):
        if self.boom:
            raise RuntimeError("database is locked")
        self.asked.append((query, str(scope), int(limit or 0)))
        return tuple(self.rows.get(scope, ()))


def _run(recaller, **arguments):
    tool = MemoryRecallTool(recaller)
    return tool.run(ToolRequest(tool="memory.recall", arguments=arguments, origin="agent"))


def test_a_hit_comes_back_as_one_speakable_line_per_record():
    """带上层名与时间，因为「什么时候说的」常常就是问题本身。"""
    recaller = FakeRecaller({"mid": [Row("1", "mid", "fact", "他想买一台二手 ThinkPad")]})

    result = _run(recaller, query="买什么")

    assert result.ok
    assert "他想买一台二手 ThinkPad" in result.output
    assert "记住的" in result.output
    assert "2026-09-01 14:30" in result.output


def test_nothing_found_is_a_success_not_a_failure():
    """`ok=False` 会让模型以为记忆坏了然后向使用者道歉。**「没找到」是一个正确的答案**，
    它该被原样转达 —— 那也是使用者最需要知道的那一件事。"""
    result = _run(FakeRecaller(), query="不存在的东西")

    assert result.ok
    assert "没有" in result.output
    assert result.audit["hits"] == 0


def test_an_empty_query_is_refused_rather_than_searched_for_nothing():
    result = _run(FakeRecaller(), query="   ")

    assert not result.ok
    assert result.audit["decision"] == "refused"


def test_a_broken_memory_store_fails_loudly_without_raising():
    """记忆坏了不该让这一轮崩 —— 但也不能报成「没找到」，那会让模型据一个假的空结果下结论。"""
    result = _run(FakeRecaller(boom=True), query="随便")

    assert not result.ok
    assert "记忆库读不了" in (result.error or "")
    assert "RuntimeError" in (result.error or "")


def test_the_audit_never_carries_the_recalled_text():
    """记忆是个人数据，而审计层是 `long` scope、会被长期保留 —— 把查到的原文写进去等于
    把同一段个人数据抄进另一层。计数与长度够诊断了。"""
    secret = "他的门禁密码是 4471"
    recaller = FakeRecaller({"mid": [Row("1", "mid", "fact", secret)]})

    result = _run(recaller, query="密码")

    assert secret in result.output, "正文要回给模型"
    assert secret not in repr(result.audit), "但一个字都不许进审计"
    assert result.audit == {"decision": "executed", "hits": 1, "query_len": 2}


def test_the_audit_layer_is_never_searched():
    """`long` 是工具审计与派发统计（谁跑了什么、成功率）。一句「搜一下我的历史」
    不该把命令执行记录念出来。"""
    recaller = FakeRecaller({"mid": [Row("1", "mid", "fact", "x")]})

    _run(recaller, query="历史")

    assert "long" not in {scope for _q, scope, _n in recaller.asked}
    assert "long" not in SCOPES


@pytest.mark.parametrize(
    "given, want",
    [(1, 1), (3, 3), (999, MAX_LIMIT), (0, 1), (-5, 1), ("四", 5), (None, 5)],
)
def test_the_limit_is_clamped_not_trusted(given, want):
    """语音的上限不是数据库的上限：念五条以上没人听得完，而模型拿到二十条只会用前几条 ——
    中间那些纯粹是延迟与费用。"""
    recaller = FakeRecaller({"mid": [Row(str(i), "mid", "fact", f"第{i}条") for i in range(20)]})

    _run(recaller, query="都有什么", limit=given)

    assert recaller.asked[0][2] == want


def test_records_are_deduplicated_across_scopes():
    """同一句话可能既在短期层又被提炼进中期层。念两遍是一个能听出来的缺陷。"""
    same = Row("dup", "mid", "fact", "他不喝咖啡")
    recaller = FakeRecaller({"mid": [same], "short": [same]})

    result = _run(recaller, query="咖啡")

    assert result.output.count("他不喝咖啡") == 1
    assert result.audit["hits"] == 1


def test_the_tool_is_absent_when_memory_is_not_attached():
    """给一个查不到任何东西的 `memory.recall` 比没有它更糟：模型会用它，
    然后据「记忆里没有」下结论 —— 而真相是记忆根本没接上。"""
    runner = open_tools({}, mcp=False)

    assert "memory.recall" not in runner.tools


def test_the_tool_is_absent_when_the_config_switches_memory_off():
    """开关关掉就该退回「这个工具不存在」，而不是留一个每次都被门拒掉的名字 ——
    `describe()["registered"]` 是「这台机器能做什么」的答案，它不该列出做不到的事。"""
    off = open_tools({"memory": {"enabled": False}}, memory_recaller=FakeRecaller(), mcp=False)
    on = open_tools({"memory": {"enabled": True}}, memory_recaller=FakeRecaller(), mcp=False)

    assert "memory.recall" not in off.tools
    assert "memory.recall" in on.tools
