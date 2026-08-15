"""The ACP adapter, against mock JSON-RPC peers.

Every test drives a real child process -- ``sys.executable -c`` -- because the
properties worth checking only exist once two pipes and a handshake are
involved: the prompt crossing intact, an error in the handshake, a timeout
killing a child, an abandoned generator reaping one.

**Evidence level: SIM.** A Python snippet is not an agent. A real ACP agent
completing one turn through this adapter is REAL-AGENT and is still owed
(ADR 003 blocker).
"""

from __future__ import annotations

import sys

import pytest

from core.agents.acp import AcpAgentAdapter, AcpAgentError
from core.agents.contract import Task


def task(text: str = "hello", **kwargs) -> Task:
    return Task(id=kwargs.pop("id", "t-1"), text=text, **kwargs)


def agent(code: str, **kwargs) -> AcpAgentAdapter:
    return AcpAgentAdapter(name="mock", command=sys.executable, args=("-c", code), **kwargs)


def spoken(chunks) -> str:
    return "".join(chunk.text for chunk in chunks if chunk.kind == "text")


# A conforming peer: initialize -> session/new -> session/prompt, echoing the
# prompt back inside two agent_message_chunk updates, then stopReason.
ACP_ECHO = """
import json, sys
def send(obj): print(json.dumps(obj), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    mid = req.get("id"); method = req.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1,"agentCapabilities":{}}})
    elif method == "session/new":
        send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":"s-1"}})
    elif method == "session/prompt":
        sid = req["params"]["sessionId"]
        text = ""
        for part in req["params"]["prompt"]:
            if isinstance(part, dict) and part.get("type") == "user":
                text = part.get("content", "")
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hello "}}}})
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":text}}}})
        send({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
"""


def test_describe_declares_the_acp_kind():
    adapter = agent(ACP_ECHO)

    assert adapter.describe().kind == "acp"
    assert adapter.check()["available"] is True


def test_the_handshake_streams_text_and_echoes_the_prompt():
    chunks = list(agent(ACP_ECHO).stream(task("world")))

    assert [chunk.kind for chunk in chunks] == ["text", "text", "done"]
    assert spoken(chunks) == "hello world"
    assert chunks[-1].error is None
    assert chunks[-1].elapsed_ms is not None


def test_a_chinese_prompt_crosses_the_process_boundary_intact():
    chunks = list(agent(ACP_ECHO).stream(task("讲个笑话")))

    assert spoken(chunks).strip() == "hello 讲个笑话"


def test_context_lines_are_rendered_into_the_prompt():
    item = task("summarise", context=("user prefers short answers",))

    chunks = list(agent(ACP_ECHO).stream(item))

    assert "user prefers short answers" in spoken(chunks)


def test_a_missing_command_is_a_failed_chunk():
    adapter = AcpAgentAdapter(name="mock", command="definitely-not-a-command")

    chunks = list(adapter.stream(task()))

    assert chunks[-1].kind == "done"
    assert chunks[-1].error is not None


def test_an_error_during_initialize_is_a_failed_chunk():
    peer = """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if req.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":req["id"],"error":{"code":-32600,"message":"nope"}}), flush=True)
"""
    chunks = list(agent(peer).stream(task()))

    assert chunks[-1].kind == "done"
    assert chunks[-1].error == "nope"


def test_a_session_without_a_session_id_is_a_failed_chunk():
    peer = """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    mid = req.get("id"); method = req.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1,"agentCapabilities":{}}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{}}), flush=True)
"""
    chunks = list(agent(peer).stream(task()))

    assert chunks[-1].kind == "done"
    assert chunks[-1].error == "session/new returned no sessionId"


def test_a_timeout_is_a_failed_chunk():
    sleeper = "import time; time.sleep(30)"
    chunks = list(agent(sleeper, timeout_s=0.5).stream(task()))

    assert chunks[-1].kind == "done"
    assert chunks[-1].error is not None
    assert "timed out" in chunks[-1].error


def test_cancel_terminates_an_inflight_turn():
    slow = """
import json, sys, time
def send(obj): print(json.dumps(obj), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    mid = req.get("id"); method = req.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1,"agentCapabilities":{}}})
    elif method == "session/new":
        send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":"s-1"}})
    elif method == "session/prompt":
        sid = req["params"]["sessionId"]
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"first"}}}})
        time.sleep(30)
"""
    adapter = agent(slow)
    gen = adapter.stream(task())

    assert next(gen).kind == "text"
    adapter.cancel("t-1")
    rest = list(gen)

    assert rest[-1].kind == "done"
    assert rest[-1].error == "cancelled"


def test_a_misconfigured_adapter_is_refused_at_construction():
    with pytest.raises(AcpAgentError):
        AcpAgentAdapter(name="mock", command="")

