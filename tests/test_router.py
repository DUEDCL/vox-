"""Five-dimension routing tests (ADR 005).

Capability, cost, latency, success rate, load. The first assertion group is the
one that matters most: **capability gates, it does not weigh**. An agent that
declared it cannot do vision must not win a vision task by being cheap and fast,
because then the declaration was decorative.

The rest cover normalisation windows, tie-breaking, the three modes' widths,
load-slot accounting (taken by ``plan()``, given back by ``record()`` or
``release()``), and the two injected collaborators -- breaker and memory --
being optional in both directions.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from core.agents.contract import AgentDescriptor, Task
from core.dispatch.router import (
    COST_MAX,
    COST_MIN,
    DEFAULT_WEIGHTS,
    LATENCY_MAX_MS,
    LATENCY_MIN_MS,
    LOAD_SATURATION,
    RACE_WIDTH,
    UNKNOWN_SUCCESS,
    DefaultRouter,
)


def descriptor(
    name: str,
    *,
    capabilities: frozenset[str] = frozenset(),
    cost: int = 3,
    latency_ms: int = 2000,
) -> AgentDescriptor:
    return AgentDescriptor(
        name=name,
        kind="cli",
        capabilities=capabilities,
        cost=cost,
        latency_ms=latency_ms,
    )


def task(
    text: str = "写个函数",
    *,
    capabilities: frozenset[str] = frozenset(),
    mode: str = "single",
) -> Task:
    return Task(id="t-1", text=text, capabilities=capabilities, mode=mode)


# -- capability gates, it does not weigh --------------------------------------


def test_an_incapable_agent_is_scored_but_never_planned():
    """The whole point of the gate.

    ``cheap`` is free, instant, and lacks the capability; ``able`` is the most
    expensive and slowest agent that has it. A weighted capability would let
    ``cheap`` win. It must not.
    """
    router = DefaultRouter(
        [
            descriptor("cheap", cost=COST_MIN, latency_ms=LATENCY_MIN_MS),
            descriptor(
                "able",
                capabilities=frozenset({"vision"}),
                cost=COST_MAX,
                latency_ms=LATENCY_MAX_MS,
            ),
        ]
    )
    scores = router.score(task(capabilities=frozenset({"vision"})))
    by_name = {entry.agent: entry for entry in scores}
    assert by_name["cheap"].capability == 0.0
    assert by_name["cheap"].total == 0.0
    assert "missing vision" in by_name["cheap"].reason
    assert by_name["able"].capability == 1.0
    plan = router.plan(task(capabilities=frozenset({"vision"})))
    assert plan.agents == ("able",)


def test_the_missing_capability_is_named_in_the_reason():
    router = DefaultRouter([descriptor("claude", capabilities=frozenset({"code"}))])
    (entry,) = router.score(task(capabilities=frozenset({"vision", "audio"})))
    assert entry.reason == "missing audio, vision"


def test_a_task_with_no_capabilities_is_satisfied_by_every_agent():
    router = DefaultRouter([descriptor("a"), descriptor("b")])
    scores = router.score(task())
    assert all(entry.capability == 1.0 for entry in scores)


# -- normalisation ------------------------------------------------------------


def test_cost_and_latency_normalise_to_the_declared_windows():
    router = DefaultRouter(
        [
            descriptor("best", cost=COST_MIN, latency_ms=LATENCY_MIN_MS),
            descriptor("worst", cost=COST_MAX, latency_ms=LATENCY_MAX_MS),
        ]
    )
    by_name = {entry.agent: entry for entry in router.score(task())}
    assert by_name["best"].cost == 1.0
    assert by_name["best"].latency == 1.0
    assert by_name["worst"].cost == 0.0
    assert by_name["worst"].latency == 0.0


def test_out_of_window_declarations_are_clamped_not_extrapolated():
    """A descriptor claiming cost 0 or 50 ms must not score above 1.0.

    Nothing validates these numbers against the schema at this layer, so a
    typo in ``config/agents.toml`` would otherwise let one agent outscore a
    perfect one and take every route.
    """
    router = DefaultRouter(
        [descriptor("liar", cost=-5, latency_ms=1), descriptor("honest", cost=COST_MIN)]
    )
    by_name = {entry.agent: entry for entry in router.score(task())}
    assert by_name["liar"].cost == 1.0
    assert by_name["liar"].latency == 1.0
    assert by_name["liar"].total <= 1.0


def test_a_free_instant_idle_never_failed_agent_scores_one():
    """Weights sum to 1.0, so ``total`` is readable as a fraction."""
    assert pytest.approx(sum(DEFAULT_WEIGHTS.values())) == 1.0
    recaller = Mock()
    recaller.success_rate.return_value = {"rate": 1.0}
    router = DefaultRouter(
        [descriptor("perfect", cost=COST_MIN, latency_ms=LATENCY_MIN_MS)],
        memory_recaller=recaller,
    )
    (entry,) = router.score(task())
    assert entry.total == 1.0


# -- success rate -------------------------------------------------------------


def test_no_memory_means_the_mid_range_unknown_score():
    router = DefaultRouter([descriptor("a")])
    (entry,) = router.score(task())
    assert entry.success == UNKNOWN_SUCCESS


def test_a_rate_of_None_is_unknown_not_total_failure():
    """``MemoryRecaller.success_rate()`` returns ``None`` for an unobserved agent.

    Reading that as 0.0 would make a never-tried agent lose to one that has
    failed every turn, which is how a fleet collapses onto whichever agent
    happened to run first.
    """
    recaller = Mock()
    recaller.success_rate.return_value = {"rate": None, "total": 0}
    router = DefaultRouter([descriptor("a")], memory_recaller=recaller)
    (entry,) = router.score(task())
    assert entry.success == UNKNOWN_SUCCESS


def test_a_recaller_that_raises_does_not_break_routing():
    recaller = Mock()
    recaller.success_rate.side_effect = RuntimeError("database is locked")
    router = DefaultRouter([descriptor("a")], memory_recaller=recaller)
    (entry,) = router.score(task())
    assert entry.success == UNKNOWN_SUCCESS


def test_an_observed_rate_is_used_and_clamped():
    recaller = Mock()
    recaller.success_rate.side_effect = lambda agent: {
        "good": {"rate": 0.9},
        "bad": {"rate": 0.1},
        "absurd": {"rate": 4.0},
    }[agent]
    router = DefaultRouter(
        [descriptor("good"), descriptor("bad"), descriptor("absurd")],
        memory_recaller=recaller,
    )
    by_name = {entry.agent: entry for entry in router.score(task())}
    assert by_name["good"].success == 0.9
    assert by_name["bad"].success == 0.1
    assert by_name["absurd"].success == 1.0


# -- load ---------------------------------------------------------------------


def test_load_saturates_rather_than_going_negative():
    router = DefaultRouter([descriptor("busy")])
    for _ in range(LOAD_SATURATION + 5):
        router._load["busy"] = router._load.get("busy", 0) + 1
    (entry,) = router.score(task())
    assert entry.load == 0.0


def test_plan_takes_a_load_slot_and_record_gives_it_back():
    router = DefaultRouter([descriptor("a")])
    router.plan(task())
    assert router.describe()["in_flight"] == {"a": 1}
    router.record("a", ok=True, elapsed_ms=100)
    assert router.describe()["in_flight"] == {}


def test_release_gives_a_slot_back_without_recording_an_outcome():
    """For a planned agent that had no adapter -- a configuration mismatch.

    Charging it to the agent would eventually route around a backend that never
    actually failed.
    """
    breaker = Mock()
    breaker.allows.return_value = True
    writer = Mock()
    router = DefaultRouter([descriptor("a")], breaker=breaker, memory_writer=writer)
    router.plan(task())
    router.release("a")
    assert router.describe()["in_flight"] == {}
    breaker.record.assert_not_called()
    writer.record_agent_outcome.assert_not_called()


def test_releasing_an_unknown_agent_is_a_no_op():
    router = DefaultRouter([descriptor("a")])
    router.release("never-planned")
    assert router.describe()["in_flight"] == {}


def test_two_slots_release_one_at_a_time():
    router = DefaultRouter([descriptor("a")])
    router.plan(task())
    router.plan(task())
    assert router.describe()["in_flight"] == {"a": 2}
    router.release("a")
    assert router.describe()["in_flight"] == {"a": 1}
    router.release("a")
    assert router.describe()["in_flight"] == {}


# -- modes --------------------------------------------------------------------


def test_single_picks_the_top_scorer_only():
    router = DefaultRouter(
        [descriptor("slow", latency_ms=9000), descriptor("fast", latency_ms=1200)]
    )
    plan = router.plan(task(mode="single"))
    assert plan.mode == "single"
    assert plan.agents == ("fast",)
    assert "fast scored" in plan.reason


def test_race_picks_the_top_two():
    router = DefaultRouter(
        [
            descriptor("a", latency_ms=1000),
            descriptor("b", latency_ms=2000),
            descriptor("c", latency_ms=3000),
        ]
    )
    plan = router.plan(task(mode="race"))
    assert len(plan.agents) == RACE_WIDTH
    assert plan.agents == ("a", "b")
    assert plan.reason == "racing a, b"


def test_fanout_takes_every_eligible_agent():
    router = DefaultRouter([descriptor("a"), descriptor("b"), descriptor("c")])
    plan = router.plan(task(mode="fanout"))
    assert set(plan.agents) == {"a", "b", "c"}
    assert plan.reason == "fanning out to 3 agents"


def test_an_unknown_mode_falls_back_to_single():
    router = DefaultRouter([descriptor("a"), descriptor("b")])
    plan = router.plan(Task(id="t", text="x", mode="turbo"))
    assert plan.mode == "single"
    assert len(plan.agents) == 1


# -- determinism --------------------------------------------------------------


def test_ties_break_by_name_so_planning_is_reproducible():
    """An order that depends on dict insertion makes routing tests flaky."""
    forward = DefaultRouter([descriptor("zeta"), descriptor("alpha")])
    backward = DefaultRouter([descriptor("alpha"), descriptor("zeta")])
    assert [entry.agent for entry in forward.score(task())] == ["alpha", "zeta"]
    assert forward.plan(task()).agents == backward.plan(task()).agents == ("alpha",)


# -- breaker ------------------------------------------------------------------


def test_a_tripped_agent_is_removed_from_scoring_entirely():
    """Not ranked last -- removed. An open breaker is a removal, not a ranking."""
    breaker = Mock()
    breaker.allows.side_effect = lambda name: name != "broken"
    router = DefaultRouter(
        [descriptor("broken"), descriptor("healthy")], breaker=breaker
    )
    assert [entry.agent for entry in router.score(task())] == ["healthy"]
    assert router.plan(task()).agents == ("healthy",)


def test_every_agent_tripped_says_so():
    breaker = Mock()
    breaker.allows.return_value = False
    router = DefaultRouter([descriptor("a"), descriptor("b")], breaker=breaker)
    plan = router.plan(task())
    assert plan.agents == ()
    assert plan.reason == "all agents tripped"


def test_record_feeds_the_breaker_and_the_memory_writer():
    breaker = Mock()
    breaker.allows.return_value = True
    writer = Mock()
    router = DefaultRouter([descriptor("a")], breaker=breaker, memory_writer=writer)
    router.record("a", ok=False, elapsed_ms=4200)
    breaker.record.assert_called_once_with("a", ok=False)
    writer.record_agent_outcome.assert_called_once_with(
        "a", ok=False, latency_ms=4200
    )


def test_a_writer_that_raises_does_not_fail_the_turn():
    """Statistics are an enhancement to a turn, never a precondition for one."""
    writer = Mock()
    writer.record_agent_outcome.side_effect = RuntimeError("database is locked")
    router = DefaultRouter([descriptor("a")], memory_writer=writer)
    router.record("a", ok=True, elapsed_ms=10)  # does not raise


# -- empty fleets -------------------------------------------------------------


def test_no_agents_configured_says_so():
    plan = DefaultRouter([]).plan(task())
    assert plan.agents == ()
    assert plan.reason == "no agents configured"


def test_no_agent_with_the_capability_names_the_capability():
    router = DefaultRouter([descriptor("a")])
    plan = router.plan(task(capabilities=frozenset({"vision"})))
    assert plan.agents == ()
    assert plan.reason == "no agent has vision"


def test_register_replaces_a_descriptor_without_clearing_its_load():
    router = DefaultRouter([descriptor("a", cost=COST_MAX)])
    router.plan(task())
    router.register(descriptor("a", cost=COST_MIN))
    assert router.describe()["in_flight"] == {"a": 1}
    (entry,) = router.score(task())
    assert entry.cost == 1.0


# -- diagnostics --------------------------------------------------------------


def test_describe_reports_wiring_without_task_text():
    router = DefaultRouter([descriptor("a")], breaker=Mock(), memory_recaller=Mock())
    report = router.describe()
    assert report["agents"] == ["a"]
    assert report["breaker"] is True
    assert report["memory"] is True
    assert set(report) == {"agents", "weights", "in_flight", "breaker", "memory"}


def test_score_is_pure_and_plan_is_not():
    """``score()`` must not take a load slot -- a caller may want the ranking."""
    router = DefaultRouter([descriptor("a")])
    router.score(task())
    router.score(task())
    assert router.describe()["in_flight"] == {}
    router.plan(task())
    assert router.describe()["in_flight"] == {"a": 1}


def test_custom_weights_override_the_defaults():
    router = DefaultRouter(
        [descriptor("expensive", cost=COST_MAX, latency_ms=LATENCY_MIN_MS)],
        weights={"latency": 1.0},
    )
    (entry,) = router.score(task())
    assert entry.total == 1.0
