"""The CLI adapter, against mock subprocesses.

Every test here drives a real child process -- ``sys.executable -c`` -- because
the properties worth checking only exist once two pipes and a process are
involved: a line arriving before the next one is written, a timeout killing a
child, an abandoned generator reaping one.

**Evidence level: SIM.** A Python snippet is not an agent. `claude -p` and
`opencode run` each completing one real turn through this adapter is REAL-AGENT,
and is still owed (ADR 003 blocker).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from core.agents.environment import SPEECH_BRIEF
from core.agents.cli import (
    PROMPT_PLACEHOLDER,
    CliAgentAdapter,
    CliAgentError,
    _terminate,
    spawn_target,
)
from core.agents.contract import Task

ECHO_PROMPT = "import sys; print(sys.argv[1])"
PROMPT_LENGTH = "import sys; print(len(sys.argv[1]))"
CONTEXT_PROBE = (
    "import sys; lines = sys.argv[1].splitlines(); print(lines[0].split(']')[-1].strip());"
    " print(lines[1])"
)
ENV_PROBE = (
    "import os; print(os.environ.get('VOX_TEST_TOKEN', 'absent'));"
    " print(os.environ.get('VOX_TEST_PLAIN', 'absent'))"
)
SLEEPER = "import time; print('one', flush=True); time.sleep(30)"


def agent(code: str, **kwargs) -> CliAgentAdapter:
    """An adapter whose "agent" is a Python snippet reading the prompt from argv."""
    return CliAgentAdapter(
        name="mock", command=sys.executable, args=("-c", code), **kwargs
    )


def task(text: str = "hello", **kwargs) -> Task:
    return Task(id=kwargs.pop("id", "t-1"), text=text, **kwargs)


def spoken(chunks) -> str:
    return "".join(chunk.text for chunk in chunks if chunk.kind == "text")


def body(chunks) -> str:
    """回显里去掉开头那段语音提示（``environment.SPEECH_BRIEF``）。

    这些测试问的是「用户那句话有没有原样穿过进程边界」，而 2026-09-01 起 CLI 后端的 prompt
    最前面多了一段固定的语音提示（它此前完全不知道回答会被念出来，于是答了两段 130 字，
    念完 30 秒）。把它剥掉再断言，好让这些测试继续只回答自己那个问题；提示本身有专门的
    测试钉住，见 ``test_the_cli_prompt_leads_with_the_speech_brief``。

    **按 ``]`` 切而不是按提示原文切。** 子进程的 stdout 用它自己的代码页（本机 cp936），
    于是回显里的中文已经不是原来那些字节了 —— 拿中文去匹配必然失败。``]`` 是 ASCII，
    而提示是唯一一个方括号块。
    """
    text = spoken(chunks)
    _brief, sep, rest = text.partition("]")
    return (rest if sep else text).lstrip()


# --- text streaming ----------------------------------------------------------


def test_text_mode_yields_one_chunk_per_line():
    chunks = list(agent("print('one'); print('two'); print('three')").stream(task()))

    assert [chunk.kind for chunk in chunks] == ["text", "text", "text", "done"]
    assert spoken(chunks).splitlines() == ["one", "two", "three"]
    assert chunks[-1].error is None
    assert chunks[-1].elapsed_ms is not None


def test_the_prompt_arrives_as_the_last_argument():
    chunks = list(agent(ECHO_PROMPT).stream(task("tell me a joke")))

    assert body(chunks).strip() == "tell me a joke"


def test_a_chinese_prompt_crosses_the_process_boundary_intact():
    """Length rather than text: the child's own stdout encoding is its business,
    but the argument it was handed must not have been mangled on the way in."""
    chunks = list(agent(PROMPT_LENGTH).stream(task("讲个笑话")))

    # 提示自己也有长度，所以这里比的是「用户那 4 个字」之外的总长减去提示长度。
    # 分隔符按通道走：命令行参数这条路是一个空格（不能有换行，见 `_prompt_for`）。
    assert int(spoken(chunks).strip()) == len(SPEECH_BRIEF) + 1 + 4


def test_context_lines_precede_the_request():
    item = task("summarise", context=("user prefers short answers",))

    chunks = list(agent(CONTEXT_PROBE).stream(item))

    # 走命令行参数这条路时提示与 prompt 之间只有一个空格（不能有换行，见 `_prompt_for`），
    # 所以第 0 行是「提示] Context:」，探针把 `]` 之前的部分切掉。
    assert spoken(chunks).splitlines() == [
        "Context:",
        "- user prefers short answers",
    ]


def test_the_placeholder_puts_the_prompt_where_config_asked():
    adapter = CliAgentAdapter(
        name="mock", command="agent", args=("--prompt", f"<{PROMPT_PLACEHOLDER}>", "-q")
    )

    assert adapter.build_argv("hi") == ["agent", "--prompt", "<hi>", "-q"]


def test_without_a_placeholder_the_prompt_is_appended():
    adapter = CliAgentAdapter(name="mock", command="claude", args=("-p",))

    assert adapter.build_argv("hi") == ["claude", "-p", "hi"]


# --- jsonl streaming ---------------------------------------------------------

JSONL_TURN = """
import json
for line in [
    {"type": "text", "text": "part one "},
    {"type": "progress"},
    {"type": "tool_use", "name": "fs.read", "input": {"path": "notes.md"}},
    {"type": "result", "usage": {"output_tokens": 12}},
]:
    print(json.dumps(line))
print("banner: not json at all")
"""

JSONL_ERROR = """
import json
print(json.dumps({"type": "text", "text": "thinking"}))
print(json.dumps({"type": "error", "message": "not logged in"}))
"""


def test_jsonl_mode_extracts_text_tool_calls_and_tokens():
    adapter = agent(JSONL_TURN, output="jsonl")

    chunks = list(adapter.stream(task()))

    assert [chunk.kind for chunk in chunks] == ["text", "tool_call", "done"]
    assert chunks[0].text == "part one "
    assert chunks[1].tool == "fs.read"
    assert chunks[1].arguments == {"path": "notes.md"}
    assert chunks[2].tokens == 12
    assert chunks[2].error is None
    assert adapter.unparsed == 1, "a banner line is noise, not a failure"


def test_a_reported_error_survives_a_zero_exit():
    """An agent that prints its failure and exits 0 must not read as success."""
    chunks = list(agent(JSONL_ERROR, output="jsonl").stream(task()))

    assert [chunk.kind for chunk in chunks] == ["text", "done"]
    assert chunks[-1].error == "not logged in"


# --- failure is a chunk ------------------------------------------------------

FAILING = (
    "import sys; print('partial');"
    " print('boom: not logged in', file=sys.stderr); sys.exit(3)"
)


def test_a_nonzero_exit_reports_the_last_stderr_line():
    chunks = list(agent(FAILING).stream(task()))

    assert spoken(chunks).strip() == "partial"
    assert chunks[-1].error == "exit 3: boom: not logged in"


def test_a_missing_command_is_a_chunk_not_a_raise():
    adapter = CliAgentAdapter(name="ghost", command="evox-no-such-command")

    chunks = list(adapter.stream(task()))

    assert [chunk.kind for chunk in chunks] == ["done"]
    assert chunks[0].error == "'evox-no-such-command' is not on PATH"


def test_a_timeout_terminates_the_child_and_says_so():
    adapter = agent(SLEEPER, timeout_s=0.6)
    started = time.perf_counter()

    chunks = list(adapter.stream(task()))

    assert chunks[-1].error == "timed out after 0.6s"
    assert time.perf_counter() - started < 10, "the child outlived its timeout"


def test_terminate_swallows_a_second_wait_timeout():
    class StuckProcess:
        def __init__(self):
            self.killed = False

        def terminate(self):
            pass

        def kill(self):
            self.killed = True

        def wait(self, *, timeout):
            raise subprocess.TimeoutExpired("stuck", timeout)

    process = StuckProcess()
    _terminate(process)
    assert process.killed is True


def test_the_output_cap_stops_the_stream():
    adapter = agent("for _ in range(5000): print('x' * 80)", max_output_bytes=500)

    chunks = list(adapter.stream(task()))

    assert chunks[-1].error == "output exceeded 500 characters"
    assert len(spoken(chunks)) <= 500


# --- cancellation and reaping ------------------------------------------------


def test_cancel_ends_an_in_flight_turn():
    adapter = agent(SLEEPER)
    stream = adapter.stream(task())

    assert next(stream).text.strip() == "one"
    adapter.cancel("t-1")
    rest = list(stream)

    assert [chunk.kind for chunk in rest] == ["done"]
    assert rest[0].error == "cancelled"


def test_cancel_before_the_first_chunk_does_not_spawn():
    """The distinguishable error proves it: a spawn attempt would have reported
    the missing command instead."""
    adapter = CliAgentAdapter(name="ghost", command="evox-no-such-command")
    adapter.cancel("t-1")

    chunks = list(adapter.stream(task()))

    assert [chunk.error for chunk in chunks] == ["cancelled"]


def test_cancel_is_safe_after_the_turn_is_over():
    adapter = agent("print('done')")
    list(adapter.stream(task()))

    adapter.cancel("t-1")  # no raise, and nothing left behind

    assert adapter._live == {}


def test_abandoning_the_stream_kills_the_child():
    adapter = agent(SLEEPER)
    stream = adapter.stream(task())
    next(stream)
    process = adapter._live["t-1"]

    stream.close()

    assert process.poll() is not None, "a lost race must not leave a subprocess"
    assert adapter._live == {}


# --- environment -------------------------------------------------------------


def test_credential_shaped_variables_are_not_inherited(monkeypatch):
    monkeypatch.setenv("VOX_TEST_TOKEN", "sk-leak")
    monkeypatch.setenv("VOX_TEST_PLAIN", "fine")

    chunks = list(agent(ENV_PROBE).stream(task()))

    assert spoken(chunks).split() == ["absent", "fine"]


def test_a_named_variable_is_passed_through_on_purpose(monkeypatch):
    monkeypatch.setenv("VOX_TEST_TOKEN", "sk-explicit")
    adapter = agent(ENV_PROBE, env_passthrough=("VOX_TEST_TOKEN",))

    chunks = list(adapter.stream(task()))

    assert spoken(chunks).split() == ["sk-explicit", "absent"]


# --- declarations ------------------------------------------------------------


def test_describe_reports_the_router_inputs():
    adapter = CliAgentAdapter(
        name="claude",
        command="claude",
        capabilities={"code"},
        cost=4,
        latency_ms=2500,
        timeout_s=90.0,
    )

    descriptor = adapter.describe()

    assert descriptor.name == "claude"
    assert descriptor.kind == "cli"
    assert descriptor.capabilities == frozenset({"code"})
    assert (descriptor.cost, descriptor.latency_ms, descriptor.timeout_s) == (
        4,
        2500,
        90.0,
    )


def test_check_separates_available_from_missing():
    assert agent("pass").check()["available"] is True

    report = CliAgentAdapter(name="ghost", command="evox-no-such-command").check()

    assert report["available"] is False
    # 原话要点名**两处**都找过了。只说「不在 PATH 上」会把人送去 PATH 里翻一个本来就
    # 不该在 PATH 里的东西 —— npm 在 Windows 上装全局包时不改 PATH，见 cli.py 的
    # `_extra_search_dirs`。
    assert "PATH" in report["reason"]
    assert "evox-no-such-command" in report["reason"]


def test_a_command_only_in_the_npm_global_dir_is_still_found(tmp_path, monkeypatch):
    """**这一条是 2026-09-01 那次「所有代码请求都失败」的正解。**

    `claude.cmd` 装在 `%APPDATA%\\npm`，而那个目录既不在用户 PATH 也不在系统 PATH 里
    （npm 在 Windows 上装全局包时不会去改 PATH，所以这是默认状态）。能力闸门会把
    「帮我改一下这个函数」正确地判给 claude，然后这一轮失败 —— 而 `check()` 报
    「不在 PATH 上」，读的人于是去查 PATH。
    """
    npm = tmp_path / "npm"
    npm.mkdir()
    shim = npm / "faux-agent.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    report = CliAgentAdapter(name="faux", command="faux-agent").check()

    assert report["available"] is True
    # 解析后的绝对路径必须报出来：不然「它到底跑的是哪个文件」成了猜测。
    # 大小写不比：``shutil.which`` 用 PATHEXT 里的写法补后缀（Windows 上是 ``.CMD``）。
    assert report["path"].casefold() == str(shim).casefold()


def test_the_fallback_never_overrides_an_explicit_path(tmp_path, monkeypatch):
    """带目录分隔的命令不走后备：调用方已经说清要哪个文件，再去别处找就是替它改主意。"""
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "faux-agent.cmd").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    explicit = CliAgentAdapter(name="faux", command="./faux-agent").check()

    assert explicit["available"] is False


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"command": ""}, "command is required"),
        ({"command": "claude", "output": "sse"}, "output must be one of"),
    ],
)
def test_misconfiguration_raises_at_construction(kwargs, message):
    """Config mistakes raise; runtime failures do not. The two must not swap."""
    with pytest.raises(CliAgentError, match=message):
        CliAgentAdapter(name="bad", **kwargs)


# --- Windows batch shims -----------------------------------------------------
#
# npm installs ``claude`` as ``claude.cmd``, and CreateProcess runs a batch file
# through cmd.exe. Python quotes arguments for the C runtime, not for cmd.exe, so
# an unquoted ``&`` in a dictated prompt would start a second command. These are
# the tests for that seam (BatBadBut).

SHIM = r"C:\npm\claude.cmd"


@pytest.fixture
def shim(monkeypatch):
    monkeypatch.setattr("core.agents.cli.which", lambda command: SHIM)


def test_a_batch_shim_gets_every_argument_quoted(shim):
    command, problem = spawn_target(["claude", "-p", "tell me a joke & thanks"])

    assert problem is None
    assert command == '"C:\\npm\\claude.cmd" "-p" "tell me a joke & thanks"'


@pytest.mark.parametrize("argument", ['say "hi"', "%PATH%"])
def test_a_batch_shim_refuses_what_cmd_would_reinterpret(shim, argument):
    command, problem = spawn_target(["claude", argument])

    assert problem is not None
    assert "batch shim" in problem


def test_a_trailing_backslash_cannot_escape_the_closing_quote(shim):
    command, problem = spawn_target(["claude", "C:\\project\\"])

    assert problem is None
    assert command.endswith('"C:\\project\\\\"')


def test_a_normal_executable_stays_an_argv_list():
    command, problem = spawn_target([sys.executable, "-c", "pass"])

    assert problem is None
    assert isinstance(command, list)
    assert command[1:] == ["-c", "pass"]


@pytest.mark.skipif(os.name != "nt", reason="batch shims are a Windows path")
def test_a_real_batch_shim_receives_the_argument_intact(tmp_path, monkeypatch):
    """The end of the argument's journey: through cmd.exe, into ``%1``, and back
    out without ``goodbye`` having been run as a command of its own."""
    (tmp_path / "evoxshim.cmd").write_text("@echo off\r\n@echo %1\r\n", encoding="ascii")
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = CliAgentAdapter(name="shim", command="evoxshim")

    chunks = list(adapter.stream(task("hello & goodbye")))

    # **这一条是「提示必须是一行」的守卫。** 多行提示走不了 cmd.exe 的命令行，
    # 于是这条路会整体失败 —— 而失败的形状是「这个 agent 不响应」。
    assert body(chunks).strip().strip('"').endswith("hello & goodbye")
    assert chunks[-1].error is None


# --- prompt via stdin: the newline hazard the shim path cannot carry ----------

STDIN_ECHO = "import sys; sys.stdout.write(sys.stdin.read())"
STDIN_SHAPE = (
    "import sys; data = sys.stdin.read(); lines = data.splitlines();"
    " print(len(lines)); print(lines[-1]); print(len(sys.argv) - 1)"
)


def test_prompt_stdin_keeps_the_prompt_out_of_the_command_line():
    adapter = agent(STDIN_ECHO, prompt_stdin=True)

    assert adapter.build_argv("anything") == [sys.executable, "-c", STDIN_ECHO]


def test_a_multiline_prompt_reaches_the_child_intact_over_stdin():
    """The regression this option exists for.

    A prompt with newlines is exactly what memory recall produces, and on Windows
    the ``claude`` on PATH is a ``.cmd`` shim -- cmd.exe command lines cannot span
    lines, so the child received only ``Context:`` and answered a request nobody
    made. The turn still reported success, which makes it a silently wrong answer
    rather than a failure. ``_CMD_UNSAFE`` refuses ``"`` and ``%`` for the same
    reason; a newline is the same hazard, and stdin sidesteps the whole quoting
    problem instead of adding a third refused character.
    """
    adapter = agent(STDIN_SHAPE, prompt_stdin=True)

    chunks = list(
        adapter.stream(task("the real question", context=("recalled one", "recalled two")))
    )

    lines = spoken(chunks).splitlines()
    # 语音提示 1 行 + 空行 + Context: + 两条 + 空行 + 那句话
    assert lines[0] == "7"
    assert lines[1] == "the real question"
    # ...and nothing was passed on the command line
    assert lines[2] == "0"
    assert chunks[-1].error is None


def test_stdin_is_closed_so_a_child_that_reads_to_eof_finishes():
    """Not closing it is a hang, and a hang arrives as a timeout -- which reads as
    "this agent is slow" rather than "we left the pipe open"."""
    adapter = agent(STDIN_ECHO, prompt_stdin=True, timeout_s=15.0)

    chunks = list(adapter.stream(task("done reading")))

    assert "done reading" in spoken(chunks)
    assert chunks[-1].error is None


def test_the_default_still_passes_the_prompt_as_an_argument():
    """``prompt_stdin`` is opt-in: an agent that only accepts argv must not change."""
    adapter = agent(ECHO_PROMPT)

    assert adapter.build_argv("p") == [sys.executable, "-c", ECHO_PROMPT, "p"]
    assert body(list(adapter.stream(task("plain")))).strip() == "plain"


def test_prompt_stdin_and_a_placeholder_are_refused_together():
    """Both would mean the prompt is in two places, or that one of them is empty."""
    with pytest.raises(CliAgentError, match="prompt_stdin"):
        CliAgentAdapter(
            name="mock",
            command=sys.executable,
            args=("-c", ECHO_PROMPT, PROMPT_PLACEHOLDER),
            prompt_stdin=True,
        )


# --- where the agent runs, which decides what it can see ----------------------


def test_a_relative_agent_cwd_resolves_against_the_repo_not_the_process():
    """A bare CLI reads the ``CLAUDE.md``, the git state and whatever it globs in its
    working directory. Resolving against the process cwd would make "where Vox was
    started from" change the agent's field of view, which is not a property of the
    launcher."""
    from pathlib import Path

    from core.agents.registry import build_adapter
    from core.tools.policy import workspace_root

    adapter = build_adapter(
        {"name": "a", "kind": "cli", "command": sys.executable, "cwd": ".agent-scratch"}
    )

    assert Path(adapter.cwd).is_absolute()
    assert Path(adapter.cwd) == workspace_root() / ".agent-scratch"
    # 相对路径这条路仍然支持，但**出厂配置不用它**：见
    # ``test_the_shipped_agent_cwd_is_outside_the_repository``。


def test_a_tilde_in_the_agent_cwd_is_expanded_and_created(tmp_path, monkeypatch):
    """``~`` 与 ``%VAR%`` 要展开，目录不存在要建出来。

    **这不是便利功能，是隔离的前提。** 真隔离要求路径落在仓库之外，而仓库之外的路径不能
    写死在一个进版本控制的配置文件里 —— 它得是 `~/.vox/...` 这种形状。

    建目录同样不是可选的：``Popen`` 遇到不存在的 cwd 直接失败，而那一轮到达调用方的形状
    是「cannot start 'claude'」—— 读起来像命令没装。
    """
    from pathlib import Path

    from core.agents.registry import build_adapter

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    adapter = build_adapter(
        {"name": "a", "kind": "cli", "command": sys.executable, "cwd": "~/.vox/agent-workspace"}
    )

    resolved = Path(adapter.cwd)
    assert resolved == tmp_path / ".vox" / "agent-workspace"
    assert resolved.is_dir(), "目录必须被建出来，否则 Popen 会报成「命令没装」"


def test_the_shipped_agent_cwd_is_outside_the_repository():
    """**出厂配置里每个 CLI 后端的工作目录都必须在仓库之外。**

    2026-09-01 实测：`cwd` 设在仓库**内部**（`.agent-workspace`）时 `claude` 的回答里出现了
    本仓库的 `git status` —— 它点名了当时正在改的 `cli.py` 与 `environment.py`。git 从 cwd
    往上找仓库根，所以「放在仓库里的子目录」根本不是一道边界。

    这一条钉的是出厂值而不是机制：机制（相对路径按仓库根解析）仍然支持，只是不该被用来
    做隔离。
    """
    from pathlib import Path

    from core.agents.registry import build_adapter, enabled_entries, load_agents_config
    from core.tools.policy import workspace_root

    repo = workspace_root().resolve()
    checked = 0
    for entry in enabled_entries(load_agents_config()):
        if entry.get("kind") != "cli" or "cwd" not in entry:
            continue
        checked += 1
        resolved = Path(build_adapter(entry).cwd).resolve()
        assert repo not in resolved.parents and resolved != repo, (
            f"{entry['name']} 的工作目录在仓库里：{resolved}"
        )
    assert checked, "出厂配置里应当至少有一个带 cwd 的 CLI 后端"


def test_an_absolute_agent_cwd_is_left_alone(tmp_path):
    from pathlib import Path

    from core.agents.registry import build_adapter

    adapter = build_adapter(
        {"name": "a", "kind": "cli", "command": sys.executable, "cwd": str(tmp_path)}
    )

    assert Path(adapter.cwd) == tmp_path


def test_the_shipped_claude_entry_does_not_run_in_the_repo_root():
    """The regression: with no ``cwd`` the child inherits Vox's, which is the repo
    root -- and a bare ``claude`` there answers about *this project* rather than about
    what the user said. Observed as "你好" coming back as a Vox status report.

    **上一版这里还要求 cwd 在仓库之内**（`workspace_root() in parents`），而 2026-09-01
    实测证明那个方向是错的：在仓库内部的子目录里 git 照样往上找到仓库根，`claude` 的回答
    里出现了本仓库正在改的文件名。现在只要求「不是仓库根」，而「必须在仓库之外」由
    ``test_the_shipped_agent_cwd_is_outside_the_repository`` 钉。
    """
    from pathlib import Path

    from core.agents.registry import build_adapter, load_agents_config
    from core.tools.policy import workspace_root

    entries = {entry["name"]: entry for entry in load_agents_config()["agents"]}
    adapter = build_adapter(entries["claude"])

    assert adapter.cwd is not None, "the shipped claude entry must pin a working directory"
    assert Path(adapter.cwd) != workspace_root()


# --------------------------------------------------- 语音提示（2026-09-01 的延迟修正）


def test_the_cli_prompt_leads_with_the_speech_brief():
    """CLI 后端**此前完全不知道回答会被念出来**。

    它是本机进程，操作系统和工作目录自己知道，所以那一整段 system prompt 对它是噪音 ——
    但「这是语音」它猜不到。实测：`claude` 对「帮我改一下这个函数」答了两段约 130 字，
    而这把音色约 4.3 字/秒，念完 30 秒。那不是它的错。
    """
    adapter = agent(ECHO_PROMPT, prompt_stdin=True)

    prompt = adapter._prompt_for(task("帮我改一下这个函数"))

    assert prompt.startswith(SPEECH_BRIEF)
    assert prompt.endswith("帮我改一下这个函数")
    # 约束的是**汇报**而不是干活：说「40 字以内」会让一个真该动手的后端少做事。
    assert "干活照常" in SPEECH_BRIEF


def test_the_speech_brief_is_a_single_line():
    """**多行提示会让走命令行参数那条路整体失败。**

    cmd.exe 的命令行不能跨行，而 `.cmd` shim 就走 cmd.exe —— 一个带换行的提示等于让
    ``%1`` 只剩提示、用户那句话整段消失，而回合照样报成功。这和 `prompt_stdin` 那段注释
    记的是同一个坑（记忆召回一接上就有换行，于是第二轮起答非所问）。
    """
    assert "\n" not in SPEECH_BRIEF
    assert "\r" not in SPEECH_BRIEF


def test_the_separator_follows_the_channel():
    """走 stdin 用空行（读起来清楚），走命令行参数用一个空格（不能有换行）。"""
    over_stdin = agent(ECHO_PROMPT, prompt_stdin=True)._prompt_for(task("问一句"))
    over_argv = agent(ECHO_PROMPT)._prompt_for(task("问一句"))

    assert over_stdin == f"{SPEECH_BRIEF}\n\n问一句"
    assert over_argv == f"{SPEECH_BRIEF} 问一句"
