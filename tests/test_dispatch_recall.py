"""Memory-recall injection into the agent path (ADR 004 + ADR 005).

The dispatcher augments an agent-bound task with recalled facts and recent
turns before routing. This file pins that: the recaller is consulted only on
the agent path, the recalled text lands in ``Task.context``, a failing recaller
leaves the turn intact, and no recaller means no context.

Evidence level: AUTO (fake recaller, adapter, router and runner; no subprocess,
no socket, no database).
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import Mock

from core.agents.contract import AgentChunk, Task
from core.dispatch.contract import DispatchPlan, Intent
from core.dispatch.dispatcher import Dispatcher
from core.memory.contract import MemoryRecord


class FakeAdapter:
    """Records the task it streamed, so the injected context is visible."""

    def __init__(self, *chunks: AgentChunk) -> None:
        self.chunks = chunks
        self.tasks: list[Task] = []

    def stream(self, task: Task) -> Iterator[AgentChunk]:
        self.tasks.append(task)
        yield from self.chunks

    def cancel(self, turn_id: str) -> None:
        pass


class FixedRouter:
    def __init__(self, plan: DispatchPlan) -> None:
        self._plan = plan

    def plan(self, task: Task) -> DispatchPlan:
        return self._plan

    def record(self, agent: str, *, ok: bool, elapsed_ms: int) -> None:
        pass

    def release(self, agent: str) -> None:
        pass


class PassthroughAggregator:
    def merge(self, mode: str, streams) -> Iterator[AgentChunk]:
        for stream in streams:
            yield from stream


class FakeRecaller:
    """Returns fixed facts and turns; ``raises`` names the methods to break."""

    def __init__(self, *, facts=(), turns=(), raises=()) -> None:
        self.facts_data = tuple(facts)
        self.turns_data = tuple(turns)
        self.raises = frozenset(raises)
        self.facts_called = 0
        self.turns_called = 0

    def facts(self, text: str):
        self.facts_called += 1
        if "facts" in self.raises:
            raise RuntimeError("db locked")
        return tuple(
            MemoryRecord(id=f"f-{i}", scope="mid", kind="fact", text=t)
            for i, t in enumerate(self.facts_data)
        )

    def recent_turns(self, *, session_id=None, limit=12):
        self.turns_called += 1
        if "turns" in self.raises:
            raise RuntimeError("db locked")
        return tuple(
            MemoryRecord(id=f"t-{i}", scope="short", kind="turn", text=t)
            for i, t in enumerate(self.turns_data)
        )


class FixedResolver:
    def resolve(self, text: str) -> Intent:
        return Intent(kind="tool", tool="fs.read", arguments={"path": "a.txt"})


def build(*, recaller=None, resolver=None, tool_runner=None) -> Dispatcher:
    return Dispatcher(
        FixedRouter(DispatchPlan(mode="single", agents=("claude",))),
        PassthroughAggregator(),
        resolver=resolver,
        tool_runner=tool_runner,
        memory_recaller=recaller,
    )


def task(text: str = "hello", context=()) -> Task:
    return Task(id="t-1", text=text, context=tuple(context))


def done() -> AgentChunk:
    return AgentChunk(kind="done")


def test_recalled_facts_and_turns_land_in_the_agent_task():
    recaller = FakeRecaller(facts=("用户喜欢短答案",), turns=("刚才问过天气",))
    adapter = FakeAdapter(done())
    dispatcher = build(recaller=recaller)

    dispatcher.dispatch(task("再问一次"), {"claude": adapter})

    received = adapter.tasks[0]
    assert "用户喜欢短答案" in received.context
    assert "刚才问过天气" in received.context
    assert received.text == "再问一次"


def test_existing_context_is_preserved_not_replaced():
    recaller = FakeRecaller(facts=("一条事实",))
    adapter = FakeAdapter(done())
    dispatcher = build(recaller=recaller)

    dispatcher.dispatch(task("q", context=("已有上下文",)), {"claude": adapter})

    assert adapter.tasks[0].context == ("已有上下文", "一条事实")


def test_no_recaller_means_the_task_is_unchanged():
    adapter = FakeAdapter(done())
    dispatcher = build()

    dispatcher.dispatch(task("q", context=("c",)), {"claude": adapter})

    assert adapter.tasks[0].context == ("c",)


def test_a_failing_recaller_does_not_break_the_turn():
    recaller = FakeRecaller(raises=("facts", "turns"))
    adapter = FakeAdapter(done())
    dispatcher = build(recaller=recaller)

    result = dispatcher.dispatch(task("q"), {"claude": adapter})

    assert adapter.tasks[0].context == ()
    assert result.ok is True


def test_a_tool_intent_skips_recall():
    recaller = FakeRecaller(facts=("不应被读到",))
    runner = Mock()
    runner.run.return_value = Mock(ok=True, output="out", error=None, needs_confirmation=False)
    dispatcher = build(recaller=recaller, resolver=FixedResolver(), tool_runner=runner)

    result = dispatcher.dispatch(task("读一下 a.txt"), {"claude": FakeAdapter(done())})

    assert result.route == "tool"
    assert recaller.facts_called == 0
    assert recaller.turns_called == 0


def test_stream_also_injects_recall():
    recaller = FakeRecaller(facts=("一条事实",))
    adapter = FakeAdapter(done())
    dispatcher = build(recaller=recaller)

    list(dispatcher.stream(task("q"), {"claude": adapter}))

    assert "一条事实" in adapter.tasks[0].context

