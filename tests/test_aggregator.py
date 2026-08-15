"""Stream aggregation tests (ADR 005).

Three modes. ``single`` passes one stream through untouched; ``race`` keeps the
first stream to **produce a chunk** and stops the rest; ``fanout`` buffers all of
them and emits one synthetic ``done``.

Two properties are load-bearing beyond the happy paths:

- **Exactly one ``done`` terminates a merged stream.** The dispatcher uses it to
  decide the turn ended, so a forwarded per-agent ``done`` in ``fanout`` would
  end the turn early and a missing one would hang it.
- **A loser is stopped, not drained.** Prompt shutdown belongs to the adapter's
  ``cancel()``, which only the dispatcher can reach -- so what is asserted here
  is that the loser's chunks never reach the consumer, which is this layer's
  actual job.

Streams are plain generators with an event to gate them, so ordering is
deterministic rather than timing-dependent. Threads are real, so a hang here is
a genuine deadlock rather than a mocked one.
"""

from __future__ import annotations

import threading

import pytest

from core.agents.contract import AgentChunk
from core.dispatch.aggregator import DEFAULT_MAX_CONCURRENT, DefaultAggregator


def text(*words: str) -> list[AgentChunk]:
    return [AgentChunk(kind="text", text=word) for word in words]


def stream(
    *chunks: AgentChunk,
    started: threading.Event | None = None,
    gate: threading.Event | None = None,
) -> object:
    """A generator over ``chunks``, optionally gated before the first yield."""

    def generator():
        if started is not None:
            started.set()
        if gate is not None:
            gate.wait(timeout=5.0)
        yield from chunks

    return generator()


def collect(merged) -> list[AgentChunk]:
    return list(merged)


# -- validation ---------------------------------------------------------------


def test_max_concurrent_must_be_at_least_one():
    with pytest.raises(ValueError, match="max_concurrent must be at least 1"):
        DefaultAggregator(max_concurrent=0)


def test_the_default_concurrency_is_small_on_purpose():
    assert DefaultAggregator().max_concurrent == DEFAULT_MAX_CONCURRENT


# -- degenerate inputs --------------------------------------------------------


def test_no_streams_merges_to_nothing():
    assert collect(DefaultAggregator().merge("race", [])) == []


def test_one_stream_passes_through_unchanged():
    chunks = [*text("你好"), AgentChunk(kind="done", elapsed_ms=42, tokens=7)]
    out = collect(DefaultAggregator().merge("fanout", [stream(*chunks)]))
    assert out == chunks


def test_a_single_stream_that_raises_lets_the_bug_escape():
    """An adapter is contractually required to turn errors into a ``done`` chunk.

    An exception reaching here is a bug in the adapter, not an agent failing, so
    swallowing it would hide the bug. This asserts the passthrough does not.
    """

    def broken():
        yield AgentChunk(kind="text", text="半句")
        raise RuntimeError("adapter bug")

    with pytest.raises(RuntimeError, match="adapter bug"):
        collect(DefaultAggregator().merge("single", [broken()]))


# -- race ---------------------------------------------------------------------


def test_race_yields_only_the_winner_and_the_loser_never_reaches_the_consumer():
    winner_started = threading.Event()
    loser_gate = threading.Event()
    fast = stream(
        *text("快"), AgentChunk(kind="done", elapsed_ms=10), started=winner_started
    )
    slow = stream(*text("慢"), AgentChunk(kind="done"), gate=loser_gate)
    merged = DefaultAggregator().merge("race", [fast, slow])
    out = collect_then_release(merged, loser_gate)
    assert [chunk.text for chunk in out if chunk.kind == "text"] == ["快"]
    assert "慢" not in "".join(chunk.text for chunk in out)


def collect_then_release(merged, gate: threading.Event) -> list[AgentChunk]:
    """Drain the merged stream, releasing the gated loser once it is done.

    Releasing after the drain keeps the winner deterministic; releasing it at all
    keeps the drain thread from sitting on the gate for the full timeout.
    """
    try:
        return collect(merged)
    finally:
        gate.set()


def test_race_emits_exactly_one_done():
    fast = stream(*text("a", "b"), AgentChunk(kind="done", elapsed_ms=5))
    gate = threading.Event()
    slow = stream(*text("c"), AgentChunk(kind="done"), gate=gate)
    out = collect_then_release(DefaultAggregator().merge("race", [fast, slow]), gate)
    assert sum(1 for chunk in out if chunk.kind == "done") == 1


def test_a_stream_that_produces_nothing_cannot_win():
    """Winning on completion rather than on first chunk would pick the empty one."""
    empty = stream()
    real = stream(*text("答案"), AgentChunk(kind="done"))
    out = collect(DefaultAggregator().merge("race", [empty, real]))
    assert [chunk.text for chunk in out if chunk.kind == "text"] == ["答案"]


def test_every_stream_empty_merges_to_nothing():
    """What the dispatcher reads as "no agent produced anything"."""
    out = collect(DefaultAggregator().merge("race", [stream(), stream()]))
    assert out == []


def test_a_failing_winner_keeps_its_error_rather_than_falling_back():
    """``race`` picks the first to *speak*, and a failure chunk is speaking.

    Silently switching to the loser would make the merged stream a retry
    mechanism, and the dispatcher's outcome bookkeeping would report a success
    for a turn the winner failed.
    """
    gate = threading.Event()
    loud = stream(AgentChunk(kind="done", error="exit status 1"))
    other = stream(*text("我本来能答"), AgentChunk(kind="done"), gate=gate)
    out = collect_then_release(DefaultAggregator().merge("race", [loud, other]), gate)
    assert len(out) == 1
    assert out[0].kind == "done"
    assert out[0].error == "exit status 1"


def test_an_adapter_that_raises_is_reported_as_a_done_chunk_not_a_crash():
    """One buggy agent must not take the whole turn down at this layer."""

    def broken():
        raise RuntimeError("adapter bug")
        yield  # pragma: no cover - unreachable, makes this a generator

    gate = threading.Event()
    other = stream(*text("正常"), AgentChunk(kind="done"), gate=gate)
    out = collect_then_release(
        DefaultAggregator().merge("race", [broken(), other]), gate
    )
    errors = [chunk.error for chunk in out if chunk.kind == "done"]
    assert errors and "RuntimeError: adapter bug" in errors[0]


def test_single_with_several_streams_races_rather_than_fanning_out():
    """Falling through to fanout would degrade first-token latency silently."""
    gate = threading.Event()
    first = stream(*text("一"), AgentChunk(kind="done"))
    second = stream(*text("二"), AgentChunk(kind="done"), gate=gate)
    out = collect_then_release(
        DefaultAggregator().merge("single", [first, second]), gate
    )
    assert [chunk.text for chunk in out if chunk.kind == "text"] == ["一"]


def test_an_unknown_mode_races_rather_than_fanning_out():
    gate = threading.Event()
    first = stream(*text("一"), AgentChunk(kind="done"))
    second = stream(*text("二"), AgentChunk(kind="done"), gate=gate)
    out = collect_then_release(
        DefaultAggregator().merge("turbo", [first, second]), gate
    )
    assert [chunk.text for chunk in out if chunk.kind == "text"] == ["一"]


# -- fanout -------------------------------------------------------------------


def test_fanout_concatenates_in_agent_order_not_completion_order():
    """Interleaving would splice half-sentences from two agents together."""
    gate = threading.Event()
    first = stream(*text("甲一", "甲二"), AgentChunk(kind="done"), gate=gate)
    second = stream(*text("乙一"), AgentChunk(kind="done"))
    merged = DefaultAggregator().merge("fanout", [first, second])
    gate.set()  # let the first agent run; it still comes out first
    out = collect(merged)
    assert [chunk.text for chunk in out if chunk.kind == "text"] == [
        "甲一",
        "甲二",
        "乙一",
    ]


def test_fanout_folds_per_agent_done_into_one_synthetic_terminal():
    out = collect(
        DefaultAggregator().merge(
            "fanout",
            [
                stream(*text("a"), AgentChunk(kind="done", elapsed_ms=100, tokens=3)),
                stream(*text("b"), AgentChunk(kind="done", elapsed_ms=250, tokens=4)),
            ],
        )
    )
    dones = [chunk for chunk in out if chunk.kind == "done"]
    assert len(dones) == 1
    assert dones[0] is out[-1]
    # Elapsed is the slowest agent's, tokens are the sum: the turn cost both.
    assert dones[0].elapsed_ms == 250
    assert dones[0].tokens == 7
    assert dones[0].error is None


def test_fanout_drops_a_failed_agent_and_keeps_the_rest():
    out = collect(
        DefaultAggregator().merge(
            "fanout",
            [
                stream(*text("坏答案"), AgentChunk(kind="done", error="exit status 1")),
                stream(*text("好答案"), AgentChunk(kind="done", elapsed_ms=50)),
            ],
        )
    )
    assert [chunk.text for chunk in out if chunk.kind == "text"] == ["好答案"]
    assert out[-1].kind == "done"
    assert out[-1].error is None  # not every agent failed, so the turn succeeded


def test_fanout_reports_the_first_error_only_when_every_agent_failed():
    out = collect(
        DefaultAggregator().merge(
            "fanout",
            [
                stream(AgentChunk(kind="done", error="first")),
                stream(AgentChunk(kind="done", error="second")),
            ],
        )
    )
    assert len(out) == 1
    assert out[-1].error == "first"


def test_fanout_tokens_are_None_rather_than_zero_when_nothing_reported_any():
    """``0`` would read as "counted, and it was zero"."""
    out = collect(
        DefaultAggregator().merge(
            "fanout",
            [stream(*text("a"), AgentChunk(kind="done")), stream(AgentChunk(kind="done"))],
        )
    )
    assert out[-1].tokens is None


def test_fanout_forwards_tool_call_chunks():
    """Only ``done`` is folded. A tool call is content the dispatcher must see."""
    call = AgentChunk(kind="tool_call", tool="fs.read", arguments={"path": "a.txt"})
    out = collect(
        DefaultAggregator().merge(
            "fanout", [stream(call, AgentChunk(kind="done")), stream(AgentChunk(kind="done"))]
        )
    )
    assert out[0] == call


# -- concurrency limit --------------------------------------------------------


def test_max_concurrent_bounds_simultaneous_drains():
    """Each stream is a thread plus, for ``cli``, a subprocess."""
    live = []
    peak = [0]
    lock = threading.Lock()
    release = threading.Event()

    def counted(word: str):
        def generator():
            with lock:
                live.append(word)
                peak[0] = max(peak[0], len(live))
            release.wait(timeout=5.0)
            yield AgentChunk(kind="text", text=word)
            yield AgentChunk(kind="done")
            with lock:
                live.remove(word)

        return generator()

    aggregator = DefaultAggregator(max_concurrent=2)
    merged = aggregator.merge("fanout", [counted("a"), counted("b"), counted("c")])
    release.set()
    collect(merged)
    assert peak[0] <= 2
