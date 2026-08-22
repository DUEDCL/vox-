"""The EvoX bridge adapter.

The point of this file is that wrapping did not weaken anything: the transport's
own checks still run on every turn, the token never reaches a diagnostic, and a
bridge failure arrives as a ``done`` chunk instead of an exception the dispatcher
would have to catch.

Evidence level: AUTO for the wrapping, SIM for the failure paths (a stub
transport and a real ``LocalEvoXTransport`` pointed at addresses it must refuse).
A turn through a running EvoX bridge is REAL-EVOX and still owed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.agents.contract import Task
from core.agents.evox import EvoXAgentAdapter
from core.session_bridge import BridgeError, LocalEvoXTransport


@dataclass
class StubTransport:
    """Records what it was asked, answers what it was told to."""

    reply: dict[str, Any] | None = None
    error: str | None = None
    cancel_error: str | None = None
    on_send: Any = None
    sent: list[tuple[str, str | None]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def send(self, text: str, *, session_id: str | None = None) -> dict[str, Any]:
        self.sent.append((text, session_id))
        if self.on_send is not None:
            self.on_send()
        if self.error is not None:
            raise BridgeError(self.error)
        return dict(self.reply or {"turn_id": "bridge-7", "reply": "好的"})

    def cancel(self, turn_id: str) -> dict[str, Any]:
        self.cancelled.append(turn_id)
        if self.cancel_error is not None:
            raise BridgeError(self.cancel_error)
        return {}


def task(text: str = "今天天气怎么样", **kwargs) -> Task:
    return Task(id=kwargs.pop("id", "t-1"), text=text, **kwargs)


# --- one blocking turn, two chunks -------------------------------------------


def test_a_turn_is_one_text_chunk_and_one_done():
    """Not incremental, and the shape says so rather than pretending."""
    adapter = EvoXAgentAdapter(StubTransport())

    chunks = list(adapter.stream(task()))

    assert [chunk.kind for chunk in chunks] == ["text", "done"]
    assert chunks[0].text == "好的"
    assert chunks[1].error is None
    assert chunks[1].elapsed_ms is not None


def test_the_rendered_prompt_and_session_reach_the_bridge():
    transport = StubTransport()
    item = task("总结一下", session_id="s-9", context=("上次讲到向量检索",))

    list(EvoXAgentAdapter(transport).stream(item))

    assert transport.sent == [
        ("Context:\n- 上次讲到向量检索\n\n总结一下", "s-9")
    ]


def test_a_reply_free_response_yields_no_empty_text_chunk():
    transport = StubTransport(reply={"turn_id": "bridge-7"})

    chunks = list(EvoXAgentAdapter(transport).stream(task()))

    assert [chunk.kind for chunk in chunks] == ["done"]
    assert chunks[0].error is None


def test_a_bridge_failure_is_a_done_chunk_not_a_raise():
    transport = StubTransport(error="EvoX conversation bridge failed: refused")

    chunks = list(EvoXAgentAdapter(transport).stream(task()))

    assert [chunk.kind for chunk in chunks] == ["done"]
    assert chunks[0].error == "EvoX conversation bridge failed: refused"


# --- cancellation ------------------------------------------------------------


def test_cancel_translates_the_task_id_into_the_bridge_turn_id():
    transport = StubTransport()
    adapter = EvoXAgentAdapter(transport)
    stream = adapter.stream(task())
    next(stream)  # the bridge has answered, so its turn id is known

    adapter.cancel("t-1")

    assert transport.cancelled == ["bridge-7"]
    list(stream)


def test_cancel_for_a_finished_turn_makes_no_request():
    """An id the bridge never heard of is a round trip worth not making."""
    transport = StubTransport()
    adapter = EvoXAgentAdapter(transport)
    list(adapter.stream(task()))

    adapter.cancel("t-1")

    assert transport.cancelled == []


def test_a_cancel_during_the_blocking_send_is_applied_when_it_can_be():
    """The bridge only reveals its turn id when ``send`` returns, so a cancel
    made mid-request is remembered and applied one moment later."""
    transport = StubTransport()
    adapter = EvoXAgentAdapter(transport)
    transport.on_send = lambda: adapter.cancel("t-1")

    chunks = list(adapter.stream(task()))

    assert [chunk.error for chunk in chunks] == ["cancelled"]
    assert transport.cancelled == ["bridge-7"]


def test_cancel_before_the_first_chunk_sends_nothing():
    transport = StubTransport()
    adapter = EvoXAgentAdapter(transport)
    adapter.cancel("t-1")

    chunks = list(adapter.stream(task()))

    assert [chunk.error for chunk in chunks] == ["cancelled"]
    assert transport.sent == []


def test_a_refused_cancel_is_counted_not_raised():
    transport = StubTransport(cancel_error="404")
    adapter = EvoXAgentAdapter(transport)
    stream = adapter.stream(task())
    next(stream)

    adapter.cancel("t-1")

    assert adapter.cancel_failures == 1
    list(stream)


# --- the security posture survives the wrapping ------------------------------


@pytest.mark.parametrize(
    "base_url, token, expected",
    [
        ("http://localhost:8765", "", "VOX_VOICE_BRIDGE_TOKEN is required"),
        ("http://example.com", "secret", "loopback"),
        ("http://user:pass@localhost:8765", "secret", "must not contain credentials"),
    ],
)
def test_the_transports_own_checks_still_run_through_the_adapter(
    base_url, token, expected
):
    """Wrapping, not porting: every refusal comes from ``session_bridge`` itself,
    so it cannot be weakened from the adapter."""
    adapter = EvoXAgentAdapter(LocalEvoXTransport(base_url, token))

    chunks = list(adapter.stream(task()))

    assert [chunk.kind for chunk in chunks] == ["done"]
    assert expected in chunks[0].error
    assert "pass" not in chunks[0].error
    assert "secret" not in chunks[0].error


def test_check_reports_configuredness_without_echoing_the_token():
    adapter = EvoXAgentAdapter(LocalEvoXTransport("http://127.0.0.1:8765", "sk-secret"))

    report = adapter.check()

    assert report["available"] is True
    assert report["endpoint"] == "http://127.0.0.1:8765"
    assert "sk-secret" not in repr(report)


def test_check_strips_credentials_out_of_the_endpoint():
    adapter = EvoXAgentAdapter(LocalEvoXTransport("http://u:p@localhost:8765", "t"))

    assert adapter.check()["endpoint"] == "http://localhost:8765"


def test_check_names_the_missing_variable():
    report = EvoXAgentAdapter(LocalEvoXTransport("http://localhost:8765", "")).check()

    assert report["available"] is False
    assert report["reason"] == "VOX_VOICE_BRIDGE_TOKEN is not set"


# --- declarations ------------------------------------------------------------


def test_describe_declares_the_evox_kind():
    adapter = EvoXAgentAdapter(StubTransport(), capabilities={"chat", "memory"})

    descriptor = adapter.describe()

    assert descriptor.kind == "evox"
    assert descriptor.name == "evox"
    assert descriptor.capabilities == frozenset({"chat", "memory"})
    assert descriptor.cost == 1, "the local bridge is the cheapest backend there is"


def test_from_env_wires_a_transport_without_contacting_it(monkeypatch):
    monkeypatch.setenv("VOX_VOICE_BRIDGE_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("VOX_VOICE_BRIDGE_TOKEN", "sk-env")

    adapter = EvoXAgentAdapter.from_env(name="evox-local")

    assert adapter.name == "evox-local"
    assert adapter.check() == {
        "name": "evox-local",
        "kind": "evox",
        "available": True,
        "endpoint": "http://127.0.0.1:9000",
        "cancel_failures": 0,
    }
