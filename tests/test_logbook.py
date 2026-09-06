"""运行日志：环形缓冲、游标、凭据过滤，以及派发器往里写的那几条。

这份日志存在的理由是一个具体的报告形状：「``route=tool ok=false tool=fs.read 0ms``」——
事件流按契约不带参数（它扇出到唤醒球和每一个消费者），所以「哪个 path 被谁拒了」在事件里
查不到。分工在**扇出面**，不在详细程度。

证据等级：AUTO。
"""

from __future__ import annotations

import pytest

from core.console.logbook import MAX_VALUE_CHARS, Logbook


def test_entries_come_back_after_the_cursor():
    """游标而不是时间戳：两条同毫秒的日志用时间戳会漏读一条。"""
    log = Logbook()
    log.write("turn", "第一条")
    log.write("turn", "第二条")

    first = log.read(0)
    assert [entry["message"] for entry in first["entries"]] == ["第一条", "第二条"]

    log.write("turn", "第三条")
    second = log.read(first["next"])
    assert [entry["message"] for entry in second["entries"]] == ["第三条"]


def test_reading_twice_with_the_same_cursor_is_idempotent():
    log = Logbook()
    log.write("turn", "x")

    assert log.read(0)["entries"] == log.read(0)["entries"]


def test_the_ring_drops_the_oldest_and_says_how_many():
    """裁掉之后要说出来 —— 不然那段空白看起来像「什么都没发生」。"""
    log = Logbook(max_entries=3)
    for index in range(6):
        log.write("turn", f"第 {index} 条")

    page = log.read(1)

    assert [entry["message"] for entry in page["entries"]] == ["第 3 条", "第 4 条", "第 5 条"]
    assert page["dropped"] == 2
    assert page["total"] == 6


def test_a_fresh_reader_is_not_told_things_were_dropped():
    """``cursor=0`` 是「从头给我」，那时候没有「中间断了」这回事。"""
    log = Logbook(max_entries=2)
    for index in range(5):
        log.write("turn", str(index))

    assert log.read(0)["dropped"] == 0


def test_a_credential_shaped_field_name_is_replaced_not_masked():
    """打码会把前缀留下，而前缀足够定位是哪个服务商的哪个 key。"""
    log = Logbook()
    log.write("tool", "x", arguments={"path": "a.md", "api_key": "sk-real-value"})

    fields = log.read(0)["entries"][0]["fields"]

    assert fields["arguments"]["path"] == "a.md"
    assert "sk-real-value" not in str(fields)
    assert fields["arguments"]["api_key"] == "（凭据，未记录）"


@pytest.mark.parametrize("name", ["token", "AUTH_TOKEN", "password", "apiKey", "my_secret"])
def test_every_sensitive_marker_is_caught(name):
    log = Logbook()
    log.write("tool", "x", arguments={name: "value-that-must-not-appear"})

    assert "value-that-must-not-appear" not in str(log.read(0)["entries"][0]["fields"])


def test_a_long_value_is_truncated_and_says_how_much_was_cut():
    """工具输出可能是整个文件，日志不是它的去处。"""
    log = Logbook()
    log.write("tool", "x", output="a" * (MAX_VALUE_CHARS + 50))

    value = log.read(0)["entries"][0]["fields"]["output"]

    assert len(value) < MAX_VALUE_CHARS + 50
    assert "还有 50 字" in value


def test_nested_and_list_values_survive():
    log = Logbook()
    log.write("agent", "x", agents=["claude", "relay"], plan={"mode": "race"})

    fields = log.read(0)["entries"][0]["fields"]

    assert fields["agents"] == ["claude", "relay"]
    assert fields["plan"] == {"mode": "race"}


def test_an_unknown_level_falls_back_to_info_rather_than_raising():
    """写日志的地方在音频线程和 pump 线程上，一个拼错的级别不该让那一轮失败。"""
    log = Logbook()
    log.write("turn", "x", level="catastrophic")

    assert log.read(0)["entries"][0]["level"] == "info"


def test_clear_empties_it_but_keeps_the_cursor_moving_forward():
    """清空后序号不重置：一个还拿着旧游标的客户端不该突然又看到「新」条目。"""
    log = Logbook()
    log.write("turn", "before")
    cursor = log.read(0)["next"]

    log.clear()
    log.write("turn", "after")

    page = log.read(cursor)
    assert [entry["message"] for entry in page["entries"]] == ["after"]


def test_the_limit_caps_one_page_without_losing_the_rest():
    log = Logbook()
    for index in range(10):
        log.write("turn", str(index))

    first = log.read(0, limit=4)
    assert len(first["entries"]) == 4

    second = log.read(first["next"], limit=100)
    assert len(second["entries"]) == 6


# -- 派发器往里写的那几条 -------------------------------------------------------


def test_the_dispatcher_records_the_tool_arguments_that_events_cannot_carry():
    """**这是这份日志存在的理由。** 「route=tool ok=false tool=fs.read 0ms」缺的正是
    「哪个 path」，而事件契约不带它。"""
    from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher
    from core.dispatch.intent import RuleBasedIntentResolver
    from core.tools import open_tools
    from core.agents.contract import Task

    log = Logbook()
    dispatcher = Dispatcher(
        router=DefaultRouter(()),
        aggregator=DefaultAggregator(),
        resolver=RuleBasedIntentResolver(),
        tool_runner=open_tools(),
        on_detail=log.write,
    )

    dispatcher.dispatch(Task(id="t-1", text="读一下 一下"))

    entries = {entry["source"]: entry for entry in log.read(0)["entries"]}
    # 「读一下 一下」现在落 agent（path 形状不像路径），所以记的是意图那一条。
    assert entries["intent"]["fields"]["kind"] == "agent"


def test_the_dispatcher_records_a_real_tool_call_with_its_path(tmp_path, monkeypatch):
    from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher
    from core.dispatch.intent import RuleBasedIntentResolver
    from core.tools import open_tools
    from core.agents.contract import Task

    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    log = Logbook()
    dispatcher = Dispatcher(
        router=DefaultRouter(()),
        aggregator=DefaultAggregator(),
        resolver=RuleBasedIntentResolver(),
        # ``enabled`` 要显式给：``open_tools(config)`` 用传进来的那份，不合并 DEFAULTS ——
        # 合并的是 ``load_tools_config()``，而这里刻意不读本机的 config/tools.toml。
        tool_runner=open_tools({"fs": {"enabled": True, "roots": [str(tmp_path)]}}),
        on_detail=log.write,
    )

    dispatcher.dispatch(Task(id="t-1", text="读一下 notes.md"))

    by_source = {entry["source"]: entry for entry in log.read(0)["entries"]}
    assert by_source["intent"]["fields"]["arguments"] == {"path": "notes.md"}
    assert by_source["tool"]["fields"]["arguments"] == {"path": "notes.md"}
    assert by_source["tool"]["fields"]["ok"] is True


def test_a_broken_log_sink_never_changes_the_turn():
    """日志失败不能改变一轮的结果。计数进 ``sink_failures``，那是它唯一的痕迹。"""
    from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher
    from core.dispatch.intent import RuleBasedIntentResolver
    from core.agents.contract import Task

    def explode(*args, **kwargs):
        raise RuntimeError("log is on fire")

    dispatcher = Dispatcher(
        router=DefaultRouter(()),
        aggregator=DefaultAggregator(),
        resolver=RuleBasedIntentResolver(),
        on_detail=explode,
    )

    result = dispatcher.dispatch(Task(id="t-1", text="你好"))

    assert result.route == "none"  # 没有 agent 可用，但这一轮走完了
    assert dispatcher.sink_failures >= 1
