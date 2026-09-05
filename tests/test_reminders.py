"""`timer.remind` —— 唯一让 Vox **主动开口**的工具。

其余九个都是被动的（使用者说一句，它做一件事，回一句话）。提醒是时间到了没人问它、它自己说
—— 那是「一个会应答的程序」和「一个助手」之间的差别，也是这些测试要守的东西：

* **一定会来**（落盘，Vox 重启不丢；原子写，写坏一半不会让所有提醒消失）
* **只来一次**（取走即删）
* **迟到也来，但要说清迟了**（静默丢掉会让人再也不用这个功能）
* **播报走完整回合**（球会亮、事件齐、日志里看得见）

Evidence level: AUTO（临时文件 + 假 plugin，不出声、不开麦克风）。
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import open_tools
from core.tools.contract import ToolRequest
from core.tools.reminders import (
    MAX_PENDING,
    STALE_HOURS,
    Reminder,
    ReminderStore,
    TimerRemindTool,
)


@pytest.fixture
def store(tmp_path):
    return ReminderStore(tmp_path / "reminders.json")


#: 固定的「现在」。**不用真时钟** —— 一条在 23:59 跑起来会失败的测试等于没有那条测试。
NOW = datetime(2026, 9, 5, 12, 0, 0)


def _tool(store):
    return TimerRemindTool({}, store=store, clock=lambda: NOW)


def _run(tool, **arguments):
    return tool.run(ToolRequest(tool="timer.remind", arguments=arguments, origin="voice"))


def test_a_relative_reminder_lands_at_the_right_minute(store):
    result = _run(_tool(store), after_minutes=20, text="关火")

    assert result.ok
    assert "12:20" in result.output
    assert store.load()[0].text == "关火"


def test_an_absolute_time_wins_over_a_relative_one(store):
    """一个具体时刻比一个相对量更接近使用者的意思 —— 和 `system.volume` 的
    `level`/`delta` 同一条。"""
    _run(_tool(store), at="14:30", after_minutes=5, text="开会")

    assert store.load()[0].due_at() == NOW.replace(hour=14, minute=30)


def test_a_time_already_past_means_tomorrow(store):
    """说「六点提醒我」的人在中午说的是今天下午，在晚上八点说的是明天早上。"""
    _run(_tool(store), at="06:00", text="起床")

    assert store.load()[0].due_at() == (NOW + timedelta(days=1)).replace(hour=6, minute=0)


@pytest.mark.parametrize(
    "arguments, because",
    [
        ({"text": "关火"}, "没说什么时候"),
        ({"after_minutes": 5}, "没说提醒什么"),
        ({"after_minutes": -5, "text": "关火"}, "没说什么时候"),
        ({"after_minutes": True, "text": "关火"}, "没说什么时候"),
    ],
)
def test_half_a_reminder_is_refused(store, arguments, because):
    """`{"after_minutes": true}` 那一条钉的是 `bool` 不是分钟数 —— `float(True)` 是 1.0，
    于是「提醒我」会变成「一分钟后提醒我」。"""
    result = _run(_tool(store), **arguments)

    assert not result.ok
    assert because in (result.error or "")


def test_listing_says_how_long_is_left(store):
    tool = _tool(store)
    _run(tool, after_minutes=20, text="关火")
    _run(tool, at="14:30", text="开会")

    result = _run(tool)

    assert "关火" in result.output and "开会" in result.output
    assert "还有 20 分钟" in result.output


def test_an_empty_list_is_a_success_not_a_failure(store):
    """「现在没有提醒」是一个正确的答案，该被原样念出来。"""
    result = _run(_tool(store))

    assert result.ok
    assert "没有提醒" in result.output


def test_cancelling_matches_on_the_text(store):
    tool = _tool(store)
    _run(tool, after_minutes=20, text="关火")
    _run(tool, after_minutes=40, text="倒垃圾")

    assert _run(tool, cancel="关火").ok
    assert [row.text for row in store.load()] == ["倒垃圾"]


def test_cancelling_something_that_is_not_there_lists_what_is(store):
    tool = _tool(store)
    _run(tool, after_minutes=20, text="关火")

    result = _run(tool, cancel="开会")

    assert not result.ok
    assert "关火" in (result.error or ""), "说不出「有哪些」的失败让人没有下一步"


def test_the_pending_count_is_capped(store):
    """上限不是数据库限制，是「念得完」—— 十条提醒里第七条到期时使用者早就忘了他设过它。
    也顺带挡住一个把工具当循环用的模型。"""
    tool = _tool(store)
    for index in range(MAX_PENDING):
        assert _run(tool, after_minutes=index + 1, text=f"第{index}件").ok

    result = _run(tool, after_minutes=99, text="再一件")

    assert not result.ok
    assert str(MAX_PENDING) in (result.error or "")
    assert len(store.load()) == MAX_PENDING


def test_the_text_never_reaches_the_audit(store):
    """提醒是使用者说的话，而审计层长期保留 —— 和 `memory.recall` 同一条立场。"""
    result = _run(_tool(store), after_minutes=5, text="门禁密码 4471")

    assert "4471" not in repr(result.audit)
    assert result.audit["pending"] == 1


# -- 到期与播报 ---------------------------------------------------------------


def test_taking_due_reminders_removes_them(store):
    """**取走即删。** 一条被念过的提醒如果还留在文件里，下一次 tick 会再念一遍。"""
    store.add(Reminder(uuid.uuid4().hex, (NOW - timedelta(seconds=5)).isoformat(), "该出门了"))
    store.add(Reminder(uuid.uuid4().hex, (NOW + timedelta(hours=2)).isoformat(), "晚点那件"))

    fresh, stale = store.take_due(NOW)

    assert [row.text for row in fresh] == ["该出门了"]
    assert stale == []
    assert [row.text for row in store.load()] == ["晚点那件"]


def test_a_reminder_that_is_very_late_is_dropped_with_a_note(store):
    """Vox 关着的时候到期了 —— 迟一点的**仍然要念**（静默丢掉会让人再也不用这个功能），
    但一个三天前的「记得倒垃圾」念出来只是噪声。"""
    store.add(Reminder(uuid.uuid4().hex,
                       (NOW - timedelta(hours=STALE_HOURS + 3)).isoformat(), "三天前的事"))
    store.add(Reminder(uuid.uuid4().hex, (NOW - timedelta(minutes=30)).isoformat(), "半小时前"))

    fresh, stale = store.take_due(NOW)

    assert [row.text for row in fresh] == ["半小时前"]
    assert [row.text for row in stale] == ["三天前的事"]
    assert store.load() == [], "两种都取走了 —— 迟太久的也不该留下来下次再判一遍"


def test_nothing_due_touches_nothing(store):
    store.add(Reminder(uuid.uuid4().hex, (NOW + timedelta(hours=1)).isoformat(), "以后"))

    assert store.take_due(NOW) == ([], [])
    assert len(store.load()) == 1


def test_a_corrupt_file_does_not_lose_the_tool(store):
    """读不动就当没有提醒。抛出去会让 `pump` 每 2 秒炸一次。"""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert store.load() == []
    assert store.add(Reminder("x", NOW.isoformat(), "还能写")) == 1


def test_rows_without_a_time_or_a_text_are_skipped(store):
    """手改过的文件里可能有半条。**跳过而不是抛** —— 一条坏行不该让其余的提醒消失。"""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps([
        {"id": "a", "at": NOW.isoformat(), "text": "好的那条"},
        {"id": "b", "at": "", "text": "没有时间"},
        {"id": "c", "at": NOW.isoformat(), "text": "   "},
        "这不是对象",
    ]), encoding="utf-8")

    assert [row.text for row in store.load()] == ["好的那条"]


def test_the_write_is_atomic(store):
    """一个被写坏一半的提醒文件会让**所有**提醒消失，而不只是这一条。"""
    store.add(Reminder(uuid.uuid4().hex, NOW.isoformat(), "一"))
    store.add(Reminder(uuid.uuid4().hex, NOW.isoformat(), "二"))

    assert not store.path.with_suffix(".tmp").exists(), "临时文件必须已经被 replace 掉"
    assert len(json.loads(store.path.read_text(encoding="utf-8"))) == 2


def test_announcing_goes_through_a_whole_turn():
    """**主动开口走和普通回答同一条路径。** 绕过状态机自己发几个事件会让这一次播报在唤醒球
    和日志里凭空消失，而「它自己说了一句」和「它没反应」必须能分开。

    第一版漏了 `_reach_listening()`：空闲时状态机在 IDLE，而 `submit_text` 只在 LISTENING
    合法，于是 `announce` 抛 `text can only be submitted while listening` —— 而 tick 把异常
    吞进日志，提醒**已经从文件里取走了**，既没被念出来也不会再来。
    """
    from vox_plugin.runtime import VoiceRuntime

    steps: list[str] = []

    class FakePlugin:
        tts = None

        def submit_text(self, text):
            steps.append(f"submit:{text}")

        def end_conversation(self):
            steps.append("end")

    runtime = VoiceRuntime(with_desktop=False, with_memory=False, visible=False)
    runtime.plugin = FakePlugin()
    runtime._reach_listening = lambda: steps.append("listening")
    runtime._complete_and_watch_tts = lambda reply: steps.append(f"speak:{reply}")
    runtime._mute_input = lambda seconds: None
    runtime._hide_now = lambda: steps.append("hide")

    runtime.announce("提醒你：该喝水了")

    assert steps == [
        "listening",
        "submit:（提醒到点）提醒你：该喝水了",
        "speak:提醒你：该喝水了",
        "end",
        "hide",
    ]
    assert runtime.turns == 1


def test_an_empty_announcement_does_nothing():
    from vox_plugin.runtime import VoiceRuntime

    runtime = VoiceRuntime(with_desktop=False, with_memory=False, visible=False)
    runtime.announce("   ")

    assert runtime.turns == 0


def test_the_tool_is_absent_without_a_store():
    """给一个存不下东西的 `timer.remind` 是最坏的失败形状：它会答「好，二十分钟后提醒你」，
    然后什么都不会发生。"""
    assert "timer.remind" not in open_tools({}, mcp=False).tools


def test_the_config_can_switch_it_off(store):
    """关掉就退回「只会应答」。"""
    off = open_tools({"timer": {"enabled": False}}, reminders=store, mcp=False)
    on = open_tools({"timer": {"enabled": True}}, reminders=store, mcp=False)

    assert "timer.remind" not in off.tools
    assert "timer.remind" in on.tools
