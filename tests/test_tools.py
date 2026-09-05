"""Tools: config, the three capabilities, and the one funnel they share.

All AUTO. The shell test runs ``git --version`` -- a real subprocess, but a
harmless allow-listed one, which is what makes it AUTO rather than SIM: nothing is
mocked, and nothing is at risk.

The security matrix lives next door in ``test_tool_security.py``; this file covers
behaviour that is supposed to work.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.events import AGENT_SCHEMA_PATH, allowed_types, validate_event
from core.memory import MemoryRecaller, MemoryWriter, SqliteMemoryStore
from core.tools import (
    DefaultToolPolicy,
    FsReadTool,
    ShellRunTool,
    ToolRequest,
    ToolResult,
    ToolRunner,
    ToolsConfigError,
    WebSearchTool,
    load_tools_config,
    open_tools,
)


@pytest.fixture()
def config(tmp_path):
    """A gate rooted at a temporary directory, with the shell off."""
    return {
        "fs": {
            "enabled": True,
            "roots": [str(tmp_path)],
            "max_bytes": 64,
            "denied_names": [".env", "*.pem", "*secret*"],
            "denied_dirs": ["enrollment", "memory"],
        },
        "web": {
            "enabled": True,
            "blocked_domains": ["ads.example"],
            "max_results": 2,
            "snippet_chars": 20,
        },
        "shell": {
            "enabled": False,
            "allow": ["git --version"],
            "require_confirmation": True,
            "require_verified_speaker": True,
            "timeout_s": 10,
            "max_output_bytes": 200,
        },
    }


@pytest.fixture()
def runner(config, tmp_path):
    events: list[dict] = []
    tools = ToolRunner(policy=DefaultToolPolicy(config), on_event=events.append)
    tools.register(FsReadTool(config))
    tools.register(WebSearchTool(config, backend=fake_search))
    tools.register(ShellRunTool(config))
    return tools, events


def fake_search(query, limit):
    """A backend that also returns the things the tool must throw away."""
    return [
        {
            "title": "Result one",
            "url": "https://docs.example/one",
            "snippet": "a snippet that is quite a bit longer than the cap",
            "content": "THE ENTIRE PAGE BODY",
        },
        {"title": "Ad", "url": "https://ads.example/x", "snippet": "buy"},
        {"title": "Result two", "url": "https://docs.example/two", "snippet": "second"},
        {"title": "Result three", "url": "https://docs.example/three", "snippet": "third"},
    ]


# -- configuration -----------------------------------------------------------


def test_the_shipped_config_opens_the_shell_behind_three_gates():
    """**2026-09-03 出厂配置把 shell 打开了** —— 使用者点名要「能直接执行终端命令」。

    所以这里断言的不再是「关着」，而是**开着的那三个条件**：白名单非空且只读、白名单内
    仍要确认、未验证的说话人进不来。少任何一条，这个 true 就不可接受。
    危险模式在代码里（不可配置），那一条由 tests/test_tool_security.py 管。
    """
    config = load_tools_config()

    assert config["shell"]["enabled"] is True
    assert config["shell"]["allow"], "开着但白名单是空的"
    assert config["shell"]["require_confirmation"] is True
    assert config["shell"]["require_verified_speaker"] is True
    assert config["fs"]["enabled"] is True


def test_a_missing_config_file_still_keeps_the_shell_shut(tmp_path):
    """The degraded path is not the moment to hand out command execution."""
    config = load_tools_config(tmp_path / "absent.toml")
    assert config["shell"]["enabled"] is False
    assert config["shell"]["allow"] == []


def test_an_unknown_key_is_an_error_not_a_shrug(tmp_path):
    """A misspelt denied_names would silently widen the sandbox."""
    path = tmp_path / "tools.toml"
    path.write_text("[fs]\ndenied_name = [\"*.pem\"]\n", encoding="utf-8")

    with pytest.raises(ToolsConfigError, match="unknown config key"):
        load_tools_config(path)


def test_a_wrongly_typed_value_is_an_error(tmp_path):
    path = tmp_path / "tools.toml"
    path.write_text("[shell]\nenabled = \"yes\"\n", encoding="utf-8")

    with pytest.raises(ToolsConfigError, match="boolean"):
        load_tools_config(path)


def test_an_unreadable_config_is_an_error(tmp_path):
    path = tmp_path / "tools.toml"
    path.write_text("[fs\nenabled = true\n", encoding="utf-8")

    with pytest.raises(ToolsConfigError, match="unreadable"):
        load_tools_config(path)


# -- fs.read -----------------------------------------------------------------


def test_reading_a_file_inside_the_sandbox_works(runner, tmp_path):
    tools, _events = runner
    (tmp_path / "notes.md").write_text("第一段", encoding="utf-8")

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "notes.md"}))

    assert result.ok is True
    assert result.output == "第一段"
    assert result.audit["path"] == "notes.md"


def test_a_file_over_the_cap_is_truncated_not_refused(runner, tmp_path):
    """A long log is still useful; refusing it would only hide it."""
    tools, _events = runner
    (tmp_path / "big.txt").write_text("x" * 200, encoding="utf-8")

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "big.txt"}))

    assert result.ok is True
    assert len(result.output) == 64
    assert result.audit["truncated"] is True


def test_a_binary_file_is_refused(runner, tmp_path):
    """A .wav read as text would arrive as mojibake in the reply and the store."""
    tools, _events = runner
    (tmp_path / "clip.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "clip.wav"}))

    assert result.ok is False
    assert result.error == "not a text file"


def test_a_missing_file_says_so(runner):
    tools, _events = runner
    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "absent.md"}))
    assert (result.ok, result.error) == (False, "no such file")


def test_fs_describe_reports_its_limits(config):
    described = FsReadTool(config).describe()
    assert described["name"] == "fs.read"
    assert described["max_bytes"] == 64


# -- web.search --------------------------------------------------------------


def test_search_returns_titles_urls_and_snippets(runner):
    tools, _events = runner
    result = tools.run(ToolRequest(tool="web.search", arguments={"query": "sherpa"}))

    assert result.ok is True
    assert "Result one" in result.output
    assert "https://docs.example/one" in result.output


def test_search_never_returns_page_text(runner):
    """A searched page must not be able to put instructions in the context."""
    tools, _events = runner
    result = tools.run(ToolRequest(tool="web.search", arguments={"query": "sherpa"}))

    assert "THE ENTIRE PAGE BODY" not in result.output


def test_a_long_snippet_is_capped(runner):
    tools, _events = runner
    result = tools.run(ToolRequest(tool="web.search", arguments={"query": "sherpa"}))

    first = result.output.splitlines()[1].strip()
    assert first == "a snippet that is qui"[:20]


def test_a_blocked_domain_is_dropped_silently(runner):
    tools, _events = runner
    result = tools.run(ToolRequest(tool="web.search", arguments={"query": "sherpa"}))

    assert "ads.example" not in result.output
    assert result.audit["hosts"] == ["docs.example"]


def test_max_results_caps_the_hit_count(runner):
    """The backend offers four; the config permits two."""
    tools, _events = runner
    result = tools.run(
        ToolRequest(tool="web.search", arguments={"query": "sherpa", "limit": 99})
    )

    assert result.audit["hits"] == 2


def test_search_without_a_backend_says_so(config):
    """Reporting unavailability beats pretending, and beats a cloud default."""
    tool = WebSearchTool(config, backend=None)
    result = tool.run(ToolRequest(tool="web.search", arguments={"query": "x"}))

    assert (result.ok, result.error) == (False, "no search backend is configured")


def test_a_backend_that_raises_does_not_take_the_turn_down(config):
    def explode(query, limit):
        raise RuntimeError("network down")

    tool = WebSearchTool(config, backend=explode)
    result = tool.run(ToolRequest(tool="web.search", arguments={"query": "x"}))

    assert result.ok is False
    assert result.error == "search backend failed: RuntimeError"


def test_an_empty_query_is_refused(runner):
    tools, _events = runner
    result = tools.run(ToolRequest(tool="web.search", arguments={"query": "   "}))
    assert (result.ok, result.error) == (False, "query is required")


# -- shell.run ---------------------------------------------------------------


def enabled_shell(config):
    """The same config with the shell switched on, as the user would have to."""
    config = {section: dict(values) for section, values in config.items()}
    config["shell"]["enabled"] = True
    return config


def test_an_allow_listed_command_runs_when_confirmed(config):
    """A real subprocess -- ``git --version`` -- so this is AUTO, not SIM."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    tool = ShellRunTool(enabled_shell(config))

    result = tool.run(
        ToolRequest(
            tool="shell.run",
            arguments={"command": "git --version", "confirmed": True},
            speaker="due",
        )
    )

    assert result.ok is True, result.error
    assert "git version" in result.output
    assert result.audit["exit_code"] == 0


def test_shell_output_is_capped(config):
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    config = enabled_shell(config)
    config["shell"]["max_output_bytes"] = 4
    tool = ShellRunTool(config)

    result = tool.run(
        ToolRequest(
            tool="shell.run",
            arguments={"command": "git --version", "confirmed": True},
            speaker="due",
        )
    )

    assert len(result.output) == 4


def test_shell_describe_states_that_there_is_no_interpreter(config):
    described = ShellRunTool(config).describe()
    assert described["shell_interpreter"] is False
    assert described["enabled"] is False


# -- the runner --------------------------------------------------------------


def test_every_event_the_runner_emits_is_in_the_platform_contract(runner, tmp_path):
    tools, events = runner
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "a.md"}))

    permitted = allowed_types(AGENT_SCHEMA_PATH)
    assert [event["type"] for event in events] == ["tool.requested", "tool.executed"]
    for event in events:
        assert event["type"] in permitted
        validate_event(event, AGENT_SCHEMA_PATH)


def test_a_refusal_emits_requested_then_refused(runner):
    tools, events = runner
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "../outside.md"}))

    assert [event["type"] for event in events] == ["tool.requested", "tool.refused"]
    assert events[-1]["payload"]["reason"] == "path is outside the sandbox"


def test_events_carry_decisions_not_content(runner, tmp_path):
    """The file's text belongs in the result, not in every log that sees the stream."""
    tools, events = runner
    (tmp_path / "a.md").write_text("SENSITIVE BODY TEXT", encoding="utf-8")
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "a.md"}))

    serialised = json.dumps(events, ensure_ascii=False)
    assert "SENSITIVE BODY TEXT" not in serialised


def test_the_confirmation_event_does_show_the_command(config):
    """A prompt that hides what it is about to run is worse than none (FR-6.13)."""
    events: list[dict] = []
    tools = ToolRunner(policy=DefaultToolPolicy(enabled_shell(config)), on_event=events.append)
    tools.register(ShellRunTool(enabled_shell(config)))

    result = tools.run(
        ToolRequest(
            tool="shell.run", arguments={"command": "git --version"}, speaker="due"
        )
    )

    assert result.needs_confirmation is True
    assert events[-1]["type"] == "tool.confirm_required"
    assert events[-1]["payload"]["command"] == "git --version"
    assert tools.confirmations == 1


def test_an_unregistered_but_permitted_tool_is_refused_not_crashed(config):
    tools = ToolRunner(policy=DefaultToolPolicy(config))
    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "x.md"}))
    assert (result.ok, result.error) == (False, "tool is not registered")


def test_a_tool_that_raises_is_reported_not_propagated(config):
    class Broken:
        name = "fs.read"

        def describe(self):
            return {"name": self.name}

        def run(self, request):
            raise ValueError("boom")

    tools = ToolRunner(policy=DefaultToolPolicy(config))
    tools.register(Broken())

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "x.md"}))

    assert result.ok is False
    assert result.error == "ValueError: boom"


def test_a_tool_exception_message_does_not_reach_the_event(config):
    class Broken:
        name = "fs.read"

        def describe(self):
            return {"name": self.name}

        def run(self, request):
            raise ValueError("secret output and C:/private/file.txt")

    events: list[dict] = []
    tools = ToolRunner(policy=DefaultToolPolicy(config), on_event=events.append)
    tools.register(Broken())

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "x.md"}))

    assert result.error == "ValueError: secret output and C:/private/file.txt"
    refused = events[-1]
    assert refused["type"] == "tool.refused"
    assert refused["payload"]["reason"] == "tool failed"
    serialised = json.dumps(events, ensure_ascii=False)
    assert "secret output" not in serialised
    assert "C:/private/file.txt" not in serialised


def test_event_sink_failure_does_not_change_the_tool_result(config, tmp_path):
    class SinkFailure:
        def __call__(self, _event):
            raise RuntimeError("bridge is gone")

    tools = ToolRunner(policy=DefaultToolPolicy(config), on_event=SinkFailure())
    tools.register(FsReadTool(config))
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "a.md"}))

    assert result.ok is True
    assert result.output == "hi"
    assert tools.sink_failures == 2


def test_every_decision_lands_in_the_long_layer(config, tmp_path):
    store = SqliteMemoryStore(":memory:")
    writer = MemoryWriter(store)
    tools = ToolRunner(policy=DefaultToolPolicy(config), memory_writer=writer)
    tools.register(FsReadTool(config))
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")

    tools.run(ToolRequest(tool="fs.read", arguments={"path": "a.md"}))
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "../outside.md"}))

    rows = MemoryRecaller(store).recall("fs.read", scope="long", limit=10)
    texts = [row.text for row in rows]
    assert any("executed" in text for text in texts)
    assert any("refused" in text for text in texts)
    assert tools.audit_dropped == 0
    store.close()


def test_an_audit_row_the_memory_filter_refuses_is_counted_not_retried(config):
    class Refusing:
        def write_audit(self, text, *, tags=()):
            return None

    tools = ToolRunner(policy=DefaultToolPolicy(config), memory_writer=Refusing())
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "../outside.md"}))
    assert tools.audit_dropped == 1


def test_a_writer_that_raises_does_not_fail_the_call(config, tmp_path):
    class Exploding:
        def write_audit(self, text, *, tags=()):
            raise RuntimeError("db is locked")

    tools = ToolRunner(policy=DefaultToolPolicy(config), memory_writer=Exploding())
    tools.register(FsReadTool(config))
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")

    result = tools.run(ToolRequest(tool="fs.read", arguments={"path": "a.md"}))

    assert result.ok is True
    assert tools.audit_dropped == 1


def test_runner_describe_counts_without_naming_arguments(runner, tmp_path):
    tools, _events = runner
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "a.md"}))
    tools.run(ToolRequest(tool="fs.read", arguments={"path": "../outside.md"}))

    report = tools.describe()

    assert report["executed"] == 1
    assert report["refused"] == 1
    assert report["registered"] == ["fs.read", "shell.run", "web.search"]
    assert report["audit_attached"] is False
    assert "a.md" not in json.dumps(report)


# -- wiring ------------------------------------------------------------------


def test_open_tools_leaves_the_shell_unregistered_while_it_is_off(config):
    """``shell.run`` 不在里面，而「简单的事平台自己做」那几个在。

    断言的是 ``shell.run`` 的缺席和其余的在场，不是一个精确的集合 —— 加一个不碰文件、
    不出网的工具（``time.now``）不该让这条测试变红，而 ``shell.run`` 溜进来必须。
    """
    tools = open_tools(config)

    registered = tools.describe()["registered"]
    assert "shell.run" not in registered
    assert {"fs.read", "web.search", "time.now", "app.open", "web.open"} <= set(registered)


def test_open_tools_registers_the_shell_once_it_is_on(config):
    tools = open_tools(enabled_shell(config))
    assert "shell.run" in tools.describe()["registered"]


def test_the_shipped_wiring_reports_what_the_gate_enforces():
    report = open_tools().describe()
    # 2026-09-03：出厂配置打开了 shell（见上面那条）。这里要看的是**报告如实**——
    # 一个说「关着」而其实开着的就绪清单比没有清单更糟。
    assert report["policy"]["shell_enabled"] is True
    assert report["policy"]["shell_allow_count"] > 0
    assert report["policy"]["dangerous_patterns"] >= 13
    # **开着就要有一条警告。** 这句话读起来像在自责，但它是对的：白名单再窄，
    # 语音转写听错一句仍然可能落在一条能跑的命令上。就绪清单要印它。
    assert any("shell.run is enabled" in line for line in report["policy"]["warnings"])


def test_importing_the_package_starts_nothing(config):
    """No subprocess, no socket: a tool exists but has not been asked to act."""
    tools = open_tools(config)
    assert tools.executed == 0
    assert WebSearchTool(config).describe()["backend_configured"] is False


def test_every_registered_tool_can_get_past_the_policy_gate():
    """**「注册了」不等于「跑得动」，而两者分岔时所有测试都是绿的。**

    2026-09-05 实测：`weather.now` 已经 `register()` 了、已经在 `core/agents/skills.REGISTERED`
    里、已经在控制台页面上列出来了，而 `contract.TOOL_NAMES`（政策门的入口白名单）里没有它
    —— 于是每一次调用都回 `unknown tool`，而当时那个工具自己的 12 条测试全绿：它们验的是
    「在不在清单里」，不是「过不过那道门」。

    这一条是那个缺口的通用护栏：**注册了什么就必须能过门**。加一个新工具而忘了 `TOOL_NAMES`，
    这里会红，而且红在「加工具」那一次而不是在真机上。

    MCP 那一族除外：它们的名字在运行时才知道，门里有自己的分支（`mcp.` 前缀）。
    """
    from core.tools.contract import TOOL_NAMES

    registered = set(open_tools().describe()["registered"])
    missing = sorted(
        name for name in registered if not name.startswith("mcp.") and name not in TOOL_NAMES
    )

    assert missing == [], (
        "这些工具注册了但政策门不认它们，每次调用都会回 unknown tool —— "
        "把它们加进 core/tools/contract.py 的 TOOL_NAMES：" + ", ".join(missing)
    )
