"""Dispatcher tests (ADR 005): intent fork, streaming, bookkeeping, events.

Grouped by what would break in production if the group went red:

- **the fork** -- a tool-shaped utterance must not reach an agent when a runner
  is attached, and must fall through to one when it is not;
- **cancellation** -- every dispatched adapter is cancelled once the merged
  stream ends, including the winner. A ``race`` that leaves the loser running is
  the leak P5's ``finally`` was written to prevent;
- **outcome recording** -- ok/failed reaches the router exactly once per agent,
  and an *abandoned* stream still records, because the turn produced no answer;
- **events** -- every emission is a validated envelope against
  ``contracts/agent-events.schema.json``, and none of them carries the utterance.
  This is the group that was silently broken for the whole of P6: ``_emit``
  called the sink directly, so ``task.completed`` -- a type that is not in the
  enum -- was emitted and nothing could catch it;
- **load slots** -- a planned agent with no adapter is released, not charged.

``needs_confirmation`` gets its own group: it is neither success nor failure, and
speaking it as a failure is the FR-6.13 violation.
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import Mock

import pytest

from core.agents.contract import AgentChunk, Task
from core.dispatch.contract import DispatchPlan, Intent
from core.dispatch.dispatcher import DispatchResult, Dispatcher
from core.events import AGENT_SCHEMA_PATH, allowed_types, validate_event


class FakeAdapter:
    """Records what it was asked to do, so leaks are visible."""

    def __init__(self, *chunks: AgentChunk, raises: BaseException | None = None) -> None:
        self.chunks = chunks
        self.raises = raises
        self.cancelled: list[str] = []
        self.streamed: list[str] = []
        self.closed = False

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        self.streamed.append(task.id)

        def generator():
            try:
                if self.raises is not None:
                    raise self.raises
                yield from self.chunks
            finally:
                self.closed = True

        return generator()

    def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)


class FakeRouter:
    """A router with a fixed plan, recording what was fed back."""

    def __init__(self, plan: DispatchPlan) -> None:
        self._plan = plan
        self.recorded: list[tuple[str, bool, int]] = []
        self.released: list[str] = []

    def score(self, task: Task):  # pragma: no cover - not on the dispatch path
        return ()

    def plan(self, task: Task) -> DispatchPlan:
        return self._plan

    def record(self, agent: str, *, ok: bool, elapsed_ms: int) -> None:
        self.recorded.append((agent, ok, elapsed_ms))

    def release(self, agent: str) -> None:
        self.released.append(agent)


class PassthroughAggregator:
    """Chains the streams in order. Enough for everything but race semantics."""

    def merge(self, mode: str, streams) -> Iterator[AgentChunk]:
        for stream in streams:
            yield from stream


def task(text: str = "写个函数", mode: str = "single") -> Task:
    return Task(id="t-1", text=text, mode=mode)


def build(
    *,
    plan: DispatchPlan | None = None,
    resolver: Any = None,
    tool_runner: Any = None,
    on_event: Any = None,
    aggregator: Any = None,
) -> tuple[Dispatcher, FakeRouter]:
    router = FakeRouter(plan or DispatchPlan(mode="single", agents=("claude",)))
    dispatcher = Dispatcher(
        router,
        aggregator or PassthroughAggregator(),
        resolver=resolver,
        tool_runner=tool_runner,
        on_event=on_event,
    )
    return dispatcher, router


def done(error: str | None = None, elapsed_ms: int = 0) -> AgentChunk:
    return AgentChunk(kind="done", error=error, elapsed_ms=elapsed_ms)


# -- the intent fork ----------------------------------------------------------


def test_no_resolver_means_every_utterance_is_an_agent_turn():
    dispatcher, _ = build()
    assert dispatcher.resolve("读一下 a.txt") == Intent(kind="agent")


def test_a_tool_intent_runs_the_tool_and_no_agent_is_touched():
    runner = Mock()
    runner.run.return_value = Mock(
        ok=True, output="第一行", error=None, needs_confirmation=False
    )
    resolver = Mock()
    resolver.resolve.return_value = Intent(
        kind="tool", tool="fs.read", arguments={"path": "a.txt"}, confidence=1.0
    )
    adapter = FakeAdapter(done())
    dispatcher, router = build(resolver=resolver, tool_runner=runner)
    result = dispatcher.dispatch(task("读一下 a.txt"), {"claude": adapter})
    assert result.route == "tool"
    assert result.tool == "fs.read"
    assert result.ok is True
    assert result.text == "第一行"
    assert adapter.streamed == []
    assert router.recorded == []


def test_a_tool_intent_falls_through_to_an_agent_when_no_runner_is_attached():
    """Tools are opt-in, like memory. Without a runner an agent can still answer."""
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="fs.read")
    adapter = FakeAdapter(AgentChunk(kind="text", text="内容"), done())
    dispatcher, _ = build(resolver=resolver, tool_runner=None)
    result = dispatcher.dispatch(task(), {"claude": adapter})
    assert result.route == "agent"
    assert adapter.streamed == ["t-1"]


def test_the_dispatcher_forwards_the_verified_speaker_and_never_invents_one():
    """P4 left this unwired, so ``shell.run`` was refused by construction.

    Carrying the real name is this stage's job. ``None`` must stay ``None``: the
    dispatcher can forward a verification, never assert one.
    """
    runner = Mock()
    runner.run.return_value = Mock(ok=True, output="", error=None, needs_confirmation=False)
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="shell.run")
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)

    dispatcher.dispatch(task(), {}, speaker="due")
    assert runner.run.call_args[0][0].speaker == "due"
    assert runner.run.call_args[0][0].origin == "voice"

    dispatcher.dispatch(task(), {})
    assert runner.run.call_args[0][0].speaker is None


def test_a_tool_runner_that_raises_becomes_a_failed_turn_not_a_crash():
    runner = Mock()
    runner.run.side_effect = RuntimeError("sandbox is gone")
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="fs.read")
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)
    result = dispatcher.dispatch(task(), {})
    assert result.ok is False
    assert result.route == "tool"
    assert "RuntimeError: sandbox is gone" in result.reason
    assert result.chunks[-1].kind == "done"
    assert "sandbox is gone" in result.chunks[-1].error


def test_a_failed_tool_reports_the_error_on_the_terminal_chunk():
    runner = Mock()
    runner.run.return_value = Mock(
        ok=False, output="", error="path escapes the sandbox", needs_confirmation=False
    )
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="fs.read")
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)
    result = dispatcher.dispatch(task(), {})
    assert result.ok is False
    assert result.chunks[-1].error == "path escapes the sandbox"


def test_a_failed_tool_with_no_error_text_still_terminates_with_an_error():
    """A ``done`` with no error reads as success; a failed tool must not."""
    runner = Mock()
    runner.run.return_value = Mock(ok=False, output="", error=None, needs_confirmation=False)
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="shell.run")
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)
    result = dispatcher.dispatch(task(), {})
    assert result.chunks[-1].error == "tool failed"


# -- needs_confirmation (FR-6.13) --------------------------------------------


def test_a_confirmation_request_is_carried_and_never_auto_confirmed():
    runner = Mock()
    runner.run.return_value = Mock(
        ok=False, output="", error="confirmation required", needs_confirmation=True
    )
    resolver = Mock()
    resolver.resolve.return_value = Intent(
        kind="tool", tool="shell.run", arguments={"command": "git push"}
    )
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)
    result = dispatcher.dispatch(task(), {})
    assert result.needs_confirmation is True
    assert result.ok is False
    # One call: nothing retries on its own with ``confirmed = True``.
    assert runner.run.call_count == 1
    assert runner.run.call_args[0][0].arguments == {"command": "git push"}


def test_an_ordinary_failure_does_not_ask_for_confirmation():
    runner = Mock()
    runner.run.return_value = Mock(
        ok=False, output="", error="not allowed", needs_confirmation=False
    )
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="shell.run")
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)
    assert dispatcher.dispatch(task(), {}).needs_confirmation is False


# -- the agent path ----------------------------------------------------------


def test_a_successful_turn_reports_ok_and_concatenates_the_text():
    adapter = FakeAdapter(
        AgentChunk(kind="text", text="第一"), AgentChunk(kind="text", text="第二"), done()
    )
    dispatcher, router = build()
    result = dispatcher.dispatch(task(), {"claude": adapter})
    assert result.ok is True
    assert result.text == "第一第二"
    assert result.agents == ("claude",)
    assert router.recorded == [("claude", True, result.elapsed_ms)]


def test_an_agent_failure_arrives_as_a_chunk_and_is_recorded_as_failed():
    adapter = FakeAdapter(done(error="exit 1: backend detail"))
    dispatcher, router = build()
    result = dispatcher.dispatch(task(), {"claude": adapter})
    assert result.ok is False
    assert router.recorded == [("claude", False, result.elapsed_ms)]


def test_a_stream_that_ends_without_a_terminal_chunk_is_a_failure():
    """No ``done`` means no complete answer, whatever text arrived."""
    adapter = FakeAdapter(AgentChunk(kind="text", text="半句"))
    dispatcher, router = build()
    result = dispatcher.dispatch(task(), {"claude": adapter})
    assert result.ok is False
    assert router.recorded == [("claude", False, result.elapsed_ms)]


def test_no_available_adapter_is_route_none_and_records_nothing():
    dispatcher, router = build(plan=DispatchPlan(mode="single", agents=("claude",), reason="x"))
    result = dispatcher.dispatch(task(), {})
    assert result.route == "none"
    assert result.ok is False
    assert result.agents == ()
    assert router.recorded == []


def test_an_empty_plan_reports_the_router_reason():
    dispatcher, _ = build(
        plan=DispatchPlan(mode="single", agents=(), reason="all agents tripped")
    )
    result = dispatcher.dispatch(task(), {})
    assert result.reason == "all agents tripped"


def test_a_planless_route_still_has_a_reason_when_the_router_gave_none():
    dispatcher, _ = build(plan=DispatchPlan(mode="single", agents=()))
    assert dispatcher.dispatch(task(), {}).reason == "no agents available"


# -- load slots ---------------------------------------------------------------


def test_a_planned_agent_with_no_adapter_is_released_not_recorded():
    """Otherwise its load score decays monotonically and it stops being routed to.

    ``plan()`` took a slot for every name it returned; a name dropped here is
    never dispatched, so nothing else would ever give the slot back.
    """
    adapter = FakeAdapter(done())
    dispatcher, router = build(
        plan=DispatchPlan(mode="race", agents=("claude", "missing"))
    )
    dispatcher.dispatch(task(), {"claude": adapter})
    assert router.released == ["missing"]
    assert [name for name, _, _ in router.recorded] == ["claude"]


def test_a_router_without_release_does_not_break_dispatch():
    """``release`` is optional on the protocol, so the call is guarded."""
    router = Mock(spec=["score", "plan", "record"])
    router.plan.return_value = DispatchPlan(mode="single", agents=("gone",))
    dispatcher = Dispatcher(router, PassthroughAggregator())
    assert dispatcher.dispatch(task(), {}).route == "none"


def test_a_release_that_raises_does_not_fail_the_turn():
    router = FakeRouter(DispatchPlan(mode="single", agents=("gone",)))
    router.release = Mock(side_effect=RuntimeError("bookkeeping is broken"))
    dispatcher = Dispatcher(router, PassthroughAggregator())
    assert dispatcher.dispatch(task(), {}).route == "none"


# -- cancellation -------------------------------------------------------------


def test_every_dispatched_adapter_is_cancelled_including_the_winner():
    """``cancel()`` is idempotent by contract, so skipping the winner would only
    trade a guaranteed-safe call for a chance of leaking the loser."""
    winner = FakeAdapter(AgentChunk(kind="text", text="快"), done())
    loser = FakeAdapter(AgentChunk(kind="text", text="慢"), done())
    dispatcher, _ = build(plan=DispatchPlan(mode="race", agents=("a", "b")))
    dispatcher.dispatch(task(), {"a": winner, "b": loser})
    assert winner.cancelled == ["t-1"]
    assert loser.cancelled == ["t-1"]


def test_an_adapter_without_cancel_is_skipped_rather_than_crashing():
    adapter = Mock(spec=["stream"])
    adapter.stream.return_value = iter([done()])
    dispatcher, _ = build()
    assert dispatcher.dispatch(task(), {"claude": adapter}).ok is True


def test_a_cancel_that_raises_does_not_mask_the_turn_outcome():
    adapter = FakeAdapter(done())
    adapter.cancel = Mock(side_effect=RuntimeError("process is already gone"))
    dispatcher, _ = build()
    assert dispatcher.dispatch(task(), {"claude": adapter}).ok is True


def test_abandoning_the_stream_still_records_and_cancels():
    """The bookkeeping lives in the generator's ``finally``, so a caller that
    stops reading is treated exactly like one that finished."""
    adapter = FakeAdapter(
        AgentChunk(kind="text", text="一"), AgentChunk(kind="text", text="二"), done()
    )
    dispatcher, router = build()
    stream = dispatcher.stream(task(), {"claude": adapter})
    assert next(stream).text == "一"
    stream.close()
    assert router.recorded and router.recorded[0][1] is False
    assert adapter.cancelled == ["t-1"]


# -- stream() -----------------------------------------------------------------


def test_stream_yields_increments_and_ends_with_the_terminal_chunk():
    adapter = FakeAdapter(AgentChunk(kind="text", text="增量"), done(elapsed_ms=7))
    dispatcher, _ = build()
    out = list(dispatcher.stream(task(), {"claude": adapter}))
    assert [chunk.kind for chunk in out] == ["text", "done"]


def test_stream_reports_no_available_agents_as_a_terminal_chunk():
    dispatcher, _ = build(plan=DispatchPlan(mode="single", agents=(), reason="none up"))
    out = list(dispatcher.stream(task(), {}))
    assert len(out) == 1
    assert out[0].kind == "done"
    assert out[0].error == "none up"


def test_stream_runs_the_tool_path_when_the_intent_is_a_tool():
    runner = Mock()
    runner.run.return_value = Mock(ok=True, output="正文", error=None, needs_confirmation=False)
    resolver = Mock()
    resolver.resolve.return_value = Intent(kind="tool", tool="fs.read")
    dispatcher, _ = build(resolver=resolver, tool_runner=runner)
    out = list(dispatcher.stream(task(), {}))
    assert [chunk.text for chunk in out if chunk.kind == "text"] == ["正文"]
    assert out[-1].kind == "done"


# -- events -------------------------------------------------------------------


def test_every_emitted_event_is_a_valid_envelope():
    """The regression this file exists for.

    ``_emit`` used to call the sink with a bare ``(type, detail)`` pair, so
    nothing checked the type against the contract -- ``task.completed``, which
    is not in the enum, shipped for the whole of P6.
    """
    events: list[dict[str, Any]] = []
    adapter = FakeAdapter(AgentChunk(kind="text", text="回复"), done())
    dispatcher, _ = build(on_event=events.append)
    dispatcher.dispatch(task(), {"claude": adapter})
    assert events
    for event in events:
        validate_event(event, AGENT_SCHEMA_PATH)  # raises if the envelope drifted
        assert event["type"] in allowed_types(AGENT_SCHEMA_PATH)
        assert event["version"] == "1"
        assert set(event) == {"version", "type", "id", "timestamp", "payload"}


def test_a_successful_turn_emits_dispatched_progress_and_done_in_order():
    events: list[dict[str, Any]] = []
    adapter = FakeAdapter(AgentChunk(kind="text", text="回复"), done())
    dispatcher, _ = build(on_event=events.append)
    dispatcher.dispatch(task(), {"claude": adapter})
    assert [event["type"] for event in events] == [
        "task.dispatched",
        "task.progress",
        "task.done",
    ]


def test_task_completed_is_never_emitted_because_it_is_not_in_the_contract():
    assert "task.completed" not in allowed_types(AGENT_SCHEMA_PATH)


def test_progress_is_emitted_once_per_turn_not_once_per_chunk():
    """A streaming reply would otherwise put hundreds of validated envelopes
    into every log and transport."""
    events: list[dict[str, Any]] = []
    adapter = FakeAdapter(*[AgentChunk(kind="text", text=str(i)) for i in range(50)], done())
    dispatcher, _ = build(on_event=events.append)
    dispatcher.dispatch(task(), {"claude": adapter})
    assert [event["type"] for event in events].count("task.progress") == 1


def test_progress_reports_the_dispatched_set_rather_than_guessing_a_winner():
    """``AgentChunk`` has no ``agent`` field, so only the aggregator knows who
    spoke first. Naming a guess would be worse than naming none."""
    events: list[dict[str, Any]] = []
    dispatcher, _ = build(
        plan=DispatchPlan(mode="race", agents=("a", "b")), on_event=events.append
    )
    dispatcher.dispatch(task(), {"a": FakeAdapter(AgentChunk(kind="text", text="快"), done()),
                                "b": FakeAdapter(done())})
    progress = next(e for e in events if e["type"] == "task.progress")
    assert progress["payload"]["agents"] == ["a", "b"]
    assert "agent" not in progress["payload"]
    assert isinstance(progress["payload"]["first_chunk_ms"], int)


def test_a_failed_turn_emits_task_failed_with_a_reason_but_no_text():
    events: list[dict[str, Any]] = []
    adapter = FakeAdapter(done(error="exit status 1"))
    dispatcher, _ = build(on_event=events.append)
    dispatcher.dispatch(task("我的秘密问题"), {"claude": adapter})
    failed = next(e for e in events if e["type"] == "task.failed")
    assert failed["payload"]["error"] == "agent reported failure"
    assert failed["payload"]["task_id"] == "t-1"


def test_task_failed_does_not_publish_agent_error_or_reply_text():
    events: list[dict[str, Any]] = []
    prompt = "用户提示中的秘密"
    reply = "模型回复中的秘密"
    stderr = f"backend echoed {prompt}; generated {reply}"
    adapter = FakeAdapter(done(error=stderr))
    dispatcher, _ = build(on_event=events.append)

    dispatcher.dispatch(task(prompt), {"claude": adapter})

    blob = repr(events)
    failed = next(e for e in events if e["type"] == "task.failed")
    assert failed["payload"]["error"] == "agent reported failure"
    assert prompt not in blob
    assert reply not in blob
    assert stderr not in blob


def test_an_abandoned_stream_names_its_own_failure_mode():
    """Distinct from an agent that reported an error -- the difference is what an
    operator needs to tell a hang from a crash."""
    events: list[dict[str, Any]] = []
    adapter = FakeAdapter(AgentChunk(kind="text", text="半句"))
    dispatcher, _ = build(on_event=events.append)
    dispatcher.dispatch(task(), {"claude": adapter})
    failed = next(e for e in events if e["type"] == "task.failed")
    assert failed["payload"]["error"] == "stream ended without a terminal chunk"


def test_no_event_carries_the_utterance_or_the_reply():
    """These fan out to every log and transport, exactly like ``memory.*``."""
    events: list[dict[str, Any]] = []
    secret = "我的银行卡密码是什么"
    reply = "这是模型的回复正文"
    adapter = FakeAdapter(AgentChunk(kind="text", text=reply), done())
    dispatcher, _ = build(on_event=events.append)
    dispatcher.dispatch(Task(id="t-1", text=secret), {"claude": adapter})
    blob = repr(events)
    assert secret not in blob
    assert reply not in blob


def test_no_sink_attached_still_validates_the_envelope():
    """Validation must not be a side effect of having a listener."""
    adapter = FakeAdapter(done())
    dispatcher, _ = build(on_event=None)
    assert dispatcher.dispatch(task(), {"claude": adapter}).ok is True


def test_dispatched_carries_the_mode_and_the_router_reason():
    events: list[dict[str, Any]] = []
    dispatcher, _ = build(
        plan=DispatchPlan(mode="race", agents=("a",), reason="racing a"),
        on_event=events.append,
    )
    dispatcher.dispatch(task(), {"a": FakeAdapter(done())})
    payload = events[0]["payload"]
    assert payload["mode"] == "race"
    assert payload["reason"] == "racing a"
    assert payload["agents"] == ["a"]


# -- the result object --------------------------------------------------------


def test_text_ignores_non_text_chunks():
    result = DispatchResult(
        route="agent",
        chunks=(
            AgentChunk(kind="text", text="说的"),
            AgentChunk(kind="tool_call", tool="fs.read"),
            AgentChunk(kind="done"),
        ),
    )
    assert result.text == "说的"


def test_a_default_result_is_a_failure():
    """``ok`` defaults to ``False``: a turn is not successful until it says so."""
    assert DispatchResult(route="none").ok is False
    assert DispatchResult(route="none").text == ""
