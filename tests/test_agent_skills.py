"""agent 会用工具 —— 但只能用注册过的那些。

使用者的实测是这个模块存在的理由：他说「帮我打开网易云音乐」（ASR 听成「试了给我打开…」），
正则没命中，落到 agent，agent 回**「好，正在打开网易云音乐。」然后什么都没发生**。

这里钉三件事：**白名单**（`shell.run` 连名字都不给它看）、**格式不可能被误触发**、
以及**执行仍然走同一套闸门**。最后一条是安全边界：这个功能扩大的是「谁能发起」，
不是「什么能被执行」。

证据等级：AUTO（纯函数 + 假 runner / 假 adapter）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agents.contract import AgentChunk, Task
from core.agents.environment import speech_system_prompt
from core.agents.skills import (
    MAX_CALLS,
    REGISTERED,
    manifest,
    parse_calls,
    render_result,
    strip_calls,
)
from core.tools.contract import ToolResult


# ---------------------------------------------------------------------- 清单


def test_the_manifest_only_lists_registered_tools():
    """**白名单，不是「所有工具」。**

    一个能被模型点名的工具集合如果等于「运行时恰好装了什么」，它会随配置漂移 ——
    而没有人在改 `tools.toml` 时想着「这会不会让 agent 多一个能力」。
    """
    text = manifest(["app.open", "shell.run", "web.open"])

    assert "app.open" in text and "web.open" in text
    assert "shell.run" not in text, "shell.run 出现在 agent 看得到的清单里了"


def test_shell_run_is_not_registered_at_all():
    """`shell.run` 要的是一次人工确认，而确认卡是给**使用者说的那句话**准备的界面。
    一个由模型发起、由使用者确认的命令执行，混淆了「谁想跑这条命令」。"""
    assert "shell.run" not in REGISTERED


def test_a_tool_that_is_not_installed_is_not_advertised():
    """印一个这台机器上不存在的工具，模型会去调它然后拿回「没装」—— 那一轮白花 2–20 秒。"""
    text = manifest(["app.open"])

    assert "app.open" in text
    assert "fs.read" not in text


def test_an_empty_toolbox_produces_no_manifest():
    """没有工具时要完全不提这件事：那时的正确行为就是老的「我做不到」。"""
    assert manifest(["nothing.real"]) == ""


def test_the_system_prompt_grows_only_when_tools_exist():
    bare = speech_system_prompt()
    armed = speech_system_prompt(["app.open"])

    assert len(armed) > len(bare)
    assert "vox:tool" in armed and "vox:tool" not in bare


# ---------------------------------------------------------------------- 解析


def test_a_call_is_parsed_with_its_arguments():
    assert parse_calls('⟦vox:tool app.open {"name": "网易云"}⟧') == [
        ("app.open", {"name": "网易云"})
    ]


def test_a_call_is_found_even_with_words_around_it():
    """模型常常在调用旁边写一句「好的我来开」。"""
    calls = parse_calls('好的⟦vox:tool web.open {"query":"bilibili.com"}⟧我来开')

    assert calls == [("web.open", {"query": "bilibili.com"})]


def test_an_unregistered_tool_is_refused_by_the_parser():
    """两道门：清单里没有它，而且就算模型猜到了名字，解析这一层也不认。"""
    assert parse_calls('⟦vox:tool shell.run {"command":"rm -rf /"}⟧') == []


def test_broken_json_degrades_to_a_plain_reply():
    """模型写坏一个花括号不该让整轮失败 —— 那时它就是一句普通回答，
    是这个格式最好的失败模式。"""
    assert parse_calls("⟦vox:tool app.open {坏的 json}⟧") == []


@pytest.mark.parametrize(
    "text",
    [
        "我帮你打开网易云音乐",
        "好，正在打开网易云音乐。",
        "vox:tool app.open",  # 没有那两个方括号
        "[vox:tool app.open {}]",  # 普通方括号不算
        "",
    ],
)
def test_ordinary_speech_never_looks_like_a_call(text):
    """U+27E6/U+27E7 不在中文标点里、不在代码里常见、也不会从语音转写里冒出来 ——
    所以它不可能被误触发。这一条钉的就是那个选择。"""
    assert parse_calls(text) == []


def test_only_one_call_per_turn():
    """延迟预算，不是技术限制：每一轮 agent 是 2–20 秒。"""
    two = '⟦vox:tool app.open {"name":"a"}⟧⟦vox:tool web.open {"query":"b"}⟧'

    assert len(parse_calls(two)) == MAX_CALLS == 1


def test_nested_arguments_are_dropped():
    """值只收标量：一个嵌套结构进到工具参数里，等于让模型决定工具内部的形状。"""
    calls = parse_calls('⟦vox:tool app.open {"name":"网易云","extra":{"a":1}}⟧')

    assert calls == [("app.open", {"name": "网易云"})]


def test_strip_removes_the_call_so_it_is_not_spoken():
    assert strip_calls('好的⟦vox:tool app.open {"name":"x"}⟧') == "好的"


def test_a_result_says_whether_it_worked():
    """**失败必须让模型知道。** 拿不到这一位它会把「拒绝了」当成「做完了」，
    然后向用户汇报成功 —— 那正是这整个模块要修的毛病。"""
    assert "失败" in render_result("app.open", False, "不在白名单里")
    assert "成功" in render_result("app.open", True, "已经打开网易云音乐")


# ------------------------------------------------------------------ 派发回路


class ScriptedAdapter:
    """第一轮回一个工具调用，第二轮回一句汇报。"""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.prompts: list[Task] = []

    def describe(self):
        from core.agents.contract import AgentDescriptor

        return AgentDescriptor(name="relay", kind="http", capabilities=frozenset())

    def stream(self, task):
        self.prompts.append(task)
        text = self.replies.pop(0) if self.replies else ""
        yield AgentChunk(kind="text", text=text)
        yield AgentChunk(kind="done")

    def cancel(self, turn_id):
        return None


class FakeRunner:
    tools = ("app.open", "web.open")

    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls: list = []

    def run(self, request):
        self.calls.append(request)
        return self.result


def _dispatcher(adapter, runner):
    from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher

    router = DefaultRouter((adapter.describe(),))
    return Dispatcher(router, DefaultAggregator(), tool_runner=runner), {"relay": adapter}


def test_a_tool_call_is_executed_and_reported_back():
    """完整回路：agent 要求 → 本机执行 → 结果回给它 → 它用一句话汇报。"""
    adapter = ScriptedAdapter([
        '⟦vox:tool app.open {"name":"网易云"}⟧',
        "网易云音乐已经打开了。",
    ])
    runner = FakeRunner(ToolResult(tool="app.open", ok=True, output="已经打开网易云音乐"))
    dispatcher, adapters = _dispatcher(adapter, runner)

    result = dispatcher.dispatch(Task(id="t-1", text="帮我打开网易云音乐"), adapters)

    assert [request.tool for request in runner.calls] == ["app.open"]
    assert runner.calls[0].arguments == {"name": "网易云"}
    assert result.text == "网易云音乐已经打开了。"
    assert result.ok is True


def test_the_tool_request_carries_no_speaker():
    """**由模型发起的请求不继承使用者的声纹身份。** 那正是 `shell.run` 的
    `require_verified_speaker` 要挡住的东西。"""
    adapter = ScriptedAdapter(['⟦vox:tool app.open {"name":"x"}⟧', "开好了"])
    runner = FakeRunner(ToolResult(tool="app.open", ok=True, output="ok"))
    dispatcher, adapters = _dispatcher(adapter, runner)

    dispatcher.dispatch(Task(id="t-1", text="打开 x"), adapters, speaker="杜")

    assert runner.calls[0].speaker in (None, "")
    assert runner.calls[0].origin == "agent"


def test_a_failed_tool_is_reported_to_the_model_not_hidden():
    adapter = ScriptedAdapter(['⟦vox:tool app.open {"name":"没装的"}⟧', "那个应用没找到。"])
    runner = FakeRunner(ToolResult(tool="app.open", ok=False, error="不在可启动的应用里"))
    dispatcher, adapters = _dispatcher(adapter, runner)

    dispatcher.dispatch(Task(id="t-1", text="打开没装的"), adapters)

    handed_back = "\n".join(adapter.prompts[-1].context)
    assert "失败" in handed_back
    assert "不在可启动的应用里" in handed_back


def test_a_turn_without_a_call_is_untouched():
    """绝大多数轮都没有工具调用 —— 那条路上一次多余的对象构造都不该做。"""
    adapter = ScriptedAdapter(["今天多云，二十三度。"])
    runner = FakeRunner(ToolResult(tool="app.open", ok=True, output="ok"))
    dispatcher, adapters = _dispatcher(adapter, runner)

    result = dispatcher.dispatch(Task(id="t-1", text="今天天气"), adapters)

    assert runner.calls == []
    assert result.text == "今天多云，二十三度。"
    assert len(adapter.prompts) == 1, "没有调用却跑了第二轮"


def test_when_the_second_round_fails_the_tool_result_is_still_spoken():
    """应用其实已经开起来了 —— 这时说「agent 失败了」是最没用的那句话。"""

    class DiesOnSecond(ScriptedAdapter):
        def stream(self, task):
            self.prompts.append(task)
            if len(self.prompts) == 1:
                yield AgentChunk(kind="text", text='⟦vox:tool app.open {"name":"网易云"}⟧')
                yield AgentChunk(kind="done")
            else:
                yield AgentChunk(kind="done", error="timed out after 120s")

    adapter = DiesOnSecond([])
    runner = FakeRunner(ToolResult(tool="app.open", ok=True, output="已经打开网易云音乐"))
    dispatcher, adapters = _dispatcher(adapter, runner)

    result = dispatcher.dispatch(Task(id="t-1", text="打开网易云"), adapters)

    assert "已经打开网易云音乐" in result.text
    assert result.ok is True


def test_no_tool_runner_means_the_call_is_just_text():
    """工具没装时这一层完全让位 —— 不该因为一个 agent 写了调用格式就凭空造出能力。"""
    from core.dispatch import DefaultAggregator, DefaultRouter, Dispatcher

    adapter = ScriptedAdapter(['⟦vox:tool app.open {"name":"x"}⟧'])
    dispatcher = Dispatcher(DefaultRouter((adapter.describe(),)), DefaultAggregator())

    result = dispatcher.dispatch(Task(id="t-1", text="打开 x"), {"relay": adapter})

    assert len(adapter.prompts) == 1
    assert "vox:tool" in result.text, "没有 runner 时原样保留，由上层决定怎么念"
