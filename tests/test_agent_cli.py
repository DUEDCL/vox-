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
    "import sys; lines = sys.argv[1].splitlines(); print(lines[0]); print(lines[1])"
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


# --- text streaming ----------------------------------------------------------


def test_text_mode_yields_one_chunk_per_line():
    chunks = list(agent("print('one'); print('two'); print('three')").stream(task()))

    assert [chunk.kind for chunk in chunks] == ["text", "text", "text", "done"]
    assert spoken(chunks).splitlines() == ["one", "two", "three"]
    assert chunks[-1].error is None
    assert chunks[-1].elapsed_ms is not None


def test_the_prompt_arrives_as_the_last_argument():
    chunks = list(agent(ECHO_PROMPT).stream(task("tell me a joke")))

    assert spoken(chunks).strip() == "tell me a joke"


def test_a_chinese_prompt_crosses_the_process_boundary_intact():
    """Length rather than text: the child's own stdout encoding is its business,
    but the argument it was handed must not have been mangled on the way in."""
    chunks = list(agent(PROMPT_LENGTH).stream(task("讲个笑话")))

    assert spoken(chunks).strip() == "4"


def test_context_lines_precede_the_request():
    item = task("summarise", context=("user prefers short answers",))

    chunks = list(agent(CONTEXT_PROBE).stream(item))

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
    assert "not on PATH" in report["reason"]


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

    assert spoken(chunks).strip() == '"hello & goodbye"'
    assert chunks[-1].error is None
