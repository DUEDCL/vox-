"""MCP servers as local tools: the config, the gate, the client, the wrapper.

The load-bearing tests here are the gate ones. An MCP server exposes tools whose
blast radius is whatever their author gave them, so the questions that matter are
"is it off by default", "does it need confirming", and "can a remote name forge a
different tool section". The transport tests come second.

Evidence level: SIM. The fake server below speaks the four shapes this client
uses; no third-party MCP server has completed a call through it.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tools import open_tools
from core.tools.contract import ToolRequest
from core.tools.mcp import (
    McpClient,
    McpConfigError,
    McpError,
    McpServerConfig,
    McpTool,
    load_mcp_config,
    open_mcp_tools,
)
from core.tools.policy import DefaultToolPolicy, load_tools_config

# --------------------------------------------------------------- a fake server


class _Stdin:
    def __init__(self, owner: "FakeProc") -> None:
        self.owner = owner

    def write(self, text: str) -> None:
        self.owner.sent.append(text)
        for line in text.splitlines():
            self.owner.handle(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Stdout:
    def __init__(self, owner: "FakeProc") -> None:
        self.owner = owner

    def readline(self) -> str:
        return self.owner.out.popleft() if self.owner.out else ""

    def close(self) -> None:
        pass


class FakeProc:
    """An in-process MCP server: answers initialize / tools/list / tools/call."""

    def __init__(
        self,
        tools=None,
        *,
        call_text="远端结果",
        is_error=False,
        list_broken=False,
        init_error=False,
        noise=False,
        ask_back=False,
    ) -> None:
        self.tools = tools if tools is not None else [{"name": "read_file", "description": "读"}]
        self.call_text = call_text
        self.is_error = is_error
        self.list_broken = list_broken
        self.init_error = init_error
        self.noise = noise
        self.ask_back = ask_back
        self.sent: list[str] = []
        self.out: deque[str] = deque()
        self.calls: list[tuple[str, dict]] = []
        self.stdin = _Stdin(self)
        self.stdout = _Stdout(self)
        self.terminated = False
        self.killed = False

    # -- process surface ------------------------------------------------------

    def poll(self):
        return 1 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.killed = True

    # -- protocol -------------------------------------------------------------

    def _emit(self, payload) -> None:
        self.out.append(json.dumps(payload, ensure_ascii=False) + "\n")

    def handle(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return
        method = message.get("method")
        rid = message.get("id")
        if method == "initialize":
            if self.init_error:
                self._emit({"jsonrpc": "2.0", "id": rid, "error": {"message": "版本不支持"}})
                return
            self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}},
                }
            )
        elif method == "tools/list":
            if self.noise:
                self.out.append("not json at all\n")
            if self.ask_back:
                self._emit({"jsonrpc": "2.0", "id": 9001, "method": "sampling/createMessage"})
            result = {} if self.list_broken else {"tools": self.tools}
            self._emit({"jsonrpc": "2.0", "id": rid, "result": result})
        elif method == "tools/call":
            params = message.get("params") or {}
            self.calls.append((params.get("name"), dict(params.get("arguments") or {})))
            self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "content": [{"type": "text", "text": self.call_text}],
                        "isError": self.is_error,
                    },
                }
            )


def spawner(proc: FakeProc):
    def spawn(*args, **kwargs):
        del args, kwargs
        return proc

    return spawn


def server(**kwargs) -> McpServerConfig:
    defaults = {"name": "fake", "command": sys.executable, "enabled": True}
    defaults.update(kwargs)
    return McpServerConfig(**defaults)


# --------------------------------------------------------------------- config


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "mcp.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_config_enables_nothing():
    """Out of the box no MCP server runs. If someone flips a default, this says so."""
    config = load_mcp_config()
    assert config["enabled"] is False
    assert config["require_confirmation"] is True
    assert not [s for s in config["servers"] if s.enabled]


def test_a_missing_file_is_not_an_error(tmp_path):
    config = load_mcp_config(tmp_path / "absent.toml")
    assert config["enabled"] is False
    assert config["servers"] == ()


def test_unknown_section_is_refused(tmp_path):
    path = write_config(tmp_path, "[mcpservers]\nenabled = true\n")
    with pytest.raises(McpConfigError, match=r"unknown config section: \[mcpservers\]"):
        load_mcp_config(path)


def test_unknown_top_level_key_is_refused(tmp_path):
    path = write_config(tmp_path, "[mcp]\nenabled = true\nallow_all = true\n")
    with pytest.raises(McpConfigError, match="unknown config key: mcp.allow_all"):
        load_mcp_config(path)


def test_unknown_server_key_is_refused(tmp_path):
    """A misspelled ``allow`` would silently expose every tool the server offers."""
    path = write_config(
        tmp_path,
        '[mcp]\nenabled = true\n[[servers]]\nname = "a"\ncommand = "x"\nallowed = ["read"]\n',
    )
    with pytest.raises(McpConfigError, match=r"unknown config key: servers\[0\].allowed"):
        load_mcp_config(path)


def test_an_integer_is_not_accepted_for_a_boolean(tmp_path):
    path = write_config(tmp_path, "[mcp]\nenabled = 1\n")
    with pytest.raises(McpConfigError, match="mcp.enabled must be a boolean"):
        load_mcp_config(path)


def test_a_server_name_must_be_a_slug(tmp_path):
    """The name becomes part of the tool id, so ``fs.read`` as a server name would
    let a remote entry forge a built-in tool section."""
    path = write_config(
        tmp_path, '[mcp]\nenabled = true\n[[servers]]\nname = "fs.read"\ncommand = "x"\n'
    )
    with pytest.raises(McpConfigError, match="must be a lowercase slug"):
        load_mcp_config(path)


def test_a_server_needs_a_command(tmp_path):
    path = write_config(tmp_path, '[mcp]\nenabled = true\n[[servers]]\nname = "a"\n')
    with pytest.raises(McpConfigError, match=r"servers\[0\].command is required"):
        load_mcp_config(path)


def test_duplicate_server_names_are_refused(tmp_path):
    path = write_config(
        tmp_path,
        '[mcp]\nenabled = true\n[[servers]]\nname = "a"\ncommand = "x"\n'
        '[[servers]]\nname = "a"\ncommand = "y"\n',
    )
    with pytest.raises(McpConfigError, match="duplicate server name"):
        load_mcp_config(path)


def test_a_string_is_not_an_array_of_strings(tmp_path):
    path = write_config(
        tmp_path, '[mcp]\nenabled = true\n[[servers]]\nname = "a"\ncommand = "x"\nallow = "read"\n'
    )
    with pytest.raises(McpConfigError, match="allow must be an array of strings"):
        load_mcp_config(path)


def test_server_timeout_falls_back_to_the_global_one(tmp_path):
    path = write_config(
        tmp_path, '[mcp]\nenabled = true\ntimeout_s = 12\n[[servers]]\nname = "a"\ncommand = "x"\n'
    )
    config = load_mcp_config(path)
    assert config["servers"][0].timeout_s == 12.0


# ----------------------------------------------------------------------- gate


def gate(**mcp) -> DefaultToolPolicy:
    tools = load_tools_config()
    return DefaultToolPolicy(tools, mcp_config=mcp or None)


def ask(policy: DefaultToolPolicy, tool: str, **arguments):
    return policy.check(ToolRequest(tool=tool, arguments=arguments, origin="voice", speaker="due"))


def test_mcp_is_refused_when_no_config_is_attached():
    verdict = ask(gate(), "mcp.fake.read_file", path="a")
    assert verdict is not None and verdict.error == "mcp tools are disabled"


def test_mcp_is_refused_when_the_master_switch_is_off():
    policy = gate(enabled=False, servers=(server(),), require_confirmation=False)
    verdict = ask(policy, "mcp.fake.read_file")
    assert verdict.error == "mcp tools are disabled"


def test_a_disabled_server_is_reported_as_an_unknown_tool():
    """Not "that server is disabled": distinguishing the two would let a caller
    enumerate which servers this machine has configured."""
    policy = gate(enabled=True, servers=(server(enabled=False),), require_confirmation=False)
    assert ask(policy, "mcp.fake.read_file").error == "unknown tool"


def test_an_absent_server_is_an_unknown_tool():
    policy = gate(enabled=True, servers=(server(),), require_confirmation=False)
    assert ask(policy, "mcp.other.read_file").error == "unknown tool"


@pytest.mark.parametrize("name", ["mcp.", "mcp.fake", "mcp.fake.", "mcp..read", "mcp.a.b.c"])
def test_a_malformed_mcp_tool_name_is_an_unknown_tool(name):
    policy = gate(enabled=True, servers=(server(),), require_confirmation=False)
    assert ask(policy, name).error == "unknown tool"


def test_a_tool_off_the_servers_allow_list_is_refused():
    policy = gate(
        enabled=True, servers=(server(allow=("read_file",)),), require_confirmation=False
    )
    assert ask(policy, "mcp.fake.write_file").error == "tool is not on the allow-list"
    assert ask(policy, "mcp.fake.read_file") is None


def test_confirmation_is_the_default_for_a_remote_tool():
    """The same starting point as ``shell.run``, for the same reason: the blast
    radius is whatever the tool's author gave it."""
    policy = gate(enabled=True, servers=(server(),))
    verdict = ask(policy, "mcp.fake.read_file")
    assert verdict.needs_confirmation is True
    assert verdict.error == "confirmation required"


def test_auto_allow_is_the_only_way_to_skip_confirmation():
    policy = gate(enabled=True, servers=(server(auto_allow=("read_file",)),))
    assert ask(policy, "mcp.fake.read_file") is None
    assert ask(policy, "mcp.fake.write_file").needs_confirmation is True


def test_a_truthy_string_does_not_count_as_confirmation():
    """``"confirmed": "no"`` is a truthy string. This exact bug was caught once
    already in ``shell.run``; it must not reappear on a new surface."""
    policy = gate(enabled=True, servers=(server(),))
    verdict = ask(policy, "mcp.fake.read_file", confirmed="no")
    assert verdict is not None and verdict.needs_confirmation is True


def test_a_real_confirmation_passes():
    policy = gate(enabled=True, servers=(server(),))
    assert ask(policy, "mcp.fake.read_file", confirmed=True) is None


def test_an_unknown_origin_is_refused_before_anything_else():
    policy = gate(enabled=True, servers=(server(auto_allow=("read_file",)),))
    verdict = policy.check(
        ToolRequest(tool="mcp.fake.read_file", arguments={}, origin="somewhere")
    )
    assert verdict is not None and verdict.error == "unknown origin"


# --------------------------------------------------------------------- client


def test_the_handshake_lists_the_servers_tools():
    proc = FakeProc(tools=[{"name": "read_file"}, {"name": "list_dir"}])
    client = McpClient(server(), spawn=spawner(proc))

    offered = client.start()

    assert [tool["name"] for tool in offered] == ["read_file", "list_dir"]
    assert client.server_info == {"name": "fake"}
    methods = [json.loads(line)["method"] for line in proc.sent]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]


def test_start_is_idempotent():
    proc = FakeProc()
    client = McpClient(server(), spawn=spawner(proc))
    client.start()
    client.start()
    assert sum(1 for line in proc.sent if '"initialize"' in line) == 1


def test_a_failed_handshake_closes_the_child():
    proc = FakeProc(init_error=True)
    client = McpClient(server(), spawn=spawner(proc))
    with pytest.raises(McpError, match="版本不支持"):
        client.start()
    assert proc.terminated, "a server that refused the handshake must not be left running"


def test_a_tools_list_without_an_array_is_a_failure():
    proc = FakeProc(list_broken=True)
    client = McpClient(server(), spawn=spawner(proc))
    with pytest.raises(McpError, match="no tools array"):
        client.start()


def test_a_tool_with_an_unusable_name_is_dropped():
    """A remote name with a dot in it would forge a different tool section."""
    proc = FakeProc(tools=[{"name": "ok_one"}, {"name": "shell.run"}, {"name": ""}])
    client = McpClient(server(), spawn=spawner(proc))
    assert [tool["name"] for tool in client.start()] == ["ok_one"]


def test_a_noisy_line_on_stdout_is_skipped_not_fatal():
    proc = FakeProc(noise=True)
    client = McpClient(server(), spawn=spawner(proc))
    assert client.start()


def test_a_server_initiated_request_is_refused_explicitly():
    """Left unanswered it would hang the next call instead of failing it."""
    proc = FakeProc(ask_back=True)
    client = McpClient(server(), spawn=spawner(proc))
    client.start()
    replies = [json.loads(line) for line in proc.sent if '"error"' in line]
    assert replies and replies[0]["error"]["code"] == -32601


def test_call_returns_the_text_and_the_error_flag():
    proc = FakeProc(call_text="文件内容")
    client = McpClient(server(), spawn=spawner(proc))
    client.start()

    text, is_error = client.call("read_file", {"path": "a.txt"})

    assert text == "文件内容"
    assert is_error is False
    assert proc.calls == [("read_file", {"path": "a.txt"})]


def test_a_closed_connection_is_an_error_not_a_hang():
    proc = FakeProc()
    client = McpClient(server(), spawn=spawner(proc))
    client.start()
    proc.out.clear()
    proc.handle = lambda line: None  # server stops answering
    with pytest.raises(McpError, match="closed the connection"):
        client.call("read_file", {})


def test_the_child_environment_drops_credentials(monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "secret-value")
    monkeypatch.setenv("KEEP_ME", "fine")
    client = McpClient(server(env_passthrough=("KEEP_ME",)))
    env = client._child_env()
    assert "MY_API_TOKEN" not in env
    assert env["KEEP_ME"] == "fine"
    assert env["PYTHONUTF8"] == "1"


def test_env_passthrough_can_reinstate_a_named_credential(monkeypatch):
    """Naming the variable is the deliberate act; the value still comes from the
    environment and never from the config file."""
    monkeypatch.setenv("MY_API_TOKEN", "secret-value")
    client = McpClient(server(env_passthrough=("MY_API_TOKEN",)))
    assert client._child_env()["MY_API_TOKEN"] == "secret-value"


def test_a_command_not_on_path_is_an_error_with_the_server_name():
    client = McpClient(server(command="definitely-not-a-real-binary-xyz"))
    with pytest.raises(McpError, match="fake:.*not on PATH"):
        client.start()


# ----------------------------------------------------------------------- tool


def built(proc: FakeProc, config: McpServerConfig | None = None, **kwargs) -> McpTool:
    config = config or server()
    client = McpClient(config, spawn=spawner(proc))
    remote = client.start()[0]
    return McpTool(config, remote, client, **kwargs)


def test_the_tool_name_carries_the_server():
    tool = built(FakeProc(tools=[{"name": "read_file", "description": "读一个文件"}]))
    assert tool.name == "mcp.fake.read_file"
    described = tool.describe()
    assert described["server"] == "fake" and described["remote"] == "read_file"
    assert described["description"] == "读一个文件"


def test_the_remote_schema_is_passed_through_not_rewritten():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    tool = built(FakeProc(tools=[{"name": "read_file", "inputSchema": schema}]))
    described = tool.describe()
    assert described["arguments"] == schema["properties"]
    assert described["required"] == ["path"]


def test_running_the_tool_returns_the_remote_text():
    proc = FakeProc(call_text="hello")
    tool = built(proc)
    result = tool.run(ToolRequest(tool=tool.name, arguments={"path": "a"}, origin="voice"))
    assert result.ok is True and result.output == "hello"
    assert result.audit["server"] == "fake" and result.audit["truncated"] is False


def test_a_remote_error_is_not_ok():
    tool = built(FakeProc(is_error=True))
    result = tool.run(ToolRequest(tool=tool.name, origin="voice"))
    assert result.ok is False
    assert result.error == "the remote tool reported an error"


def test_output_is_truncated_to_the_configured_cap():
    """Remote output is untrusted input; the cap is the same posture web.search
    takes toward page text."""
    tool = built(FakeProc(call_text="x" * 5000), max_output_bytes=100)
    result = tool.run(ToolRequest(tool=tool.name, origin="voice"))
    assert len(result.output) == 100
    assert result.audit["truncated"] is True
    assert result.audit["bytes"] == 5000


def test_the_allow_list_is_rechecked_at_run_time():
    """Defence in depth: the same rule the built-in tools follow, so a tool handed
    to a caller that built its own runner is still bounded."""
    proc = FakeProc(tools=[{"name": "write_file"}])
    tool = built(proc, server(allow=("read_file",)))
    result = tool.run(ToolRequest(tool=tool.name, origin="voice"))
    assert result.ok is False
    assert result.error == "tool is not on the server's allow-list"
    assert proc.calls == [], "the call must not reach the server"


def test_non_text_content_is_named_rather_than_dropped():
    proc = FakeProc()
    tool = built(proc)

    def call(name, arguments):
        del name, arguments
        return ("[image]", False)

    tool.client.call = call
    result = tool.run(ToolRequest(tool=tool.name, origin="voice"))
    assert result.output == "[image]"


# -------------------------------------------------------------------- registry


def test_a_disabled_registry_starts_nothing():
    registry = open_mcp_tools({"enabled": False, "servers": (server(),)})
    assert registry.tools == ()
    assert registry.clients == {}


def test_a_server_that_will_not_start_is_a_warning_not_a_failure():
    """MCP is an addition to the local tools, not a prerequisite for them."""
    registry = open_mcp_tools(
        {
            "enabled": True,
            "require_confirmation": True,
            "max_output_bytes": 2000,
            "servers": (server(command="definitely-not-a-real-binary-xyz"),),
        }
    )
    assert registry.tools == ()
    assert registry.warnings and "not on PATH" in registry.warnings[0]


def test_the_registry_wraps_every_allowed_tool():
    proc = FakeProc(tools=[{"name": "read_file"}, {"name": "write_file"}])
    registry = open_mcp_tools(
        {
            "enabled": True,
            "max_output_bytes": 2000,
            "servers": (server(allow=("read_file",)),),
        },
        spawn=spawner(proc),
    )
    assert [tool.name for tool in registry.tools] == ["mcp.fake.read_file"]
    assert registry.describe()["servers"][0]["alive"] is True
    registry.close()
    assert proc.terminated


# -------------------------------------------------------- through open_tools


def test_open_tools_skips_mcp_when_told_to():
    runner = open_tools(mcp=False)
    assert runner.mcp is None
    assert not any(name.startswith("mcp.") for name in runner.tools)


def test_open_tools_registers_mcp_tools_and_closes_them():
    proc = FakeProc(tools=[{"name": "read_file"}])
    registry = open_mcp_tools(
        {"enabled": True, "max_output_bytes": 2000, "servers": (server(),)},
        spawn=spawner(proc),
    )
    runner = open_tools(mcp=registry)
    assert "mcp.fake.read_file" in runner.tools
    assert runner.describe()["mcp"]["tools"] == ["mcp.fake.read_file"]

    runner.close()
    assert proc.terminated
    runner.close()  # idempotent


def test_the_shipped_defaults_register_no_mcp_tool():
    """``open_tools()`` with no arguments reads the real config. Nothing is enabled
    there, so nothing is started -- this is the factory posture."""
    runner = open_tools()
    assert not any(name.startswith("mcp.") for name in runner.tools)
    runner.close()
