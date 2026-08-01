"""Dispatch, routing, and aggregation contracts.

The dispatcher is the platform's new core: it decides whether a request is
answered locally by a tool or sent to one or more agents, and it merges whatever
comes back into a single increment stream the voice layer can speak.

Deliberately absent: any new ``VoiceState``. Parallel dispatch happens inside
``thinking``, and progress is reported through separate ``task.*`` events, so the
six-state machine and its three contract tests stay untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from core.agents.contract import AgentChunk, Task

#: ``single`` routes to one agent, ``race`` takes the first usable increment and
#: cancels the rest, ``fanout`` collects all and aggregates. Voice defaults to
#: ``single``/``race``: fanout degrades first-token latency to the slowest agent.
DISPATCH_MODES = frozenset({"single", "race", "fanout"})

#: What the intent resolver decided to do with an utterance.
INTENT_KINDS = frozenset({"tool", "agent"})


@dataclass(frozen=True)
class RouteScore:
    """Per-agent scoring breakdown, kept separable so it stays explainable."""

    agent: str
    total: float
    capability: float = 0.0
    cost: float = 0.0
    latency: float = 0.0
    success: float = 0.0
    load: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class DispatchPlan:
    """The routing decision for one task."""

    mode: str
    agents: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class Intent:
    """Rule-based classification of an utterance.

    Rules, not a model: keyword and regex hits on the local tools cut latency
    from seconds to milliseconds, and the hit path is fully AUTO-testable.
    """

    kind: str
    tool: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.0


@runtime_checkable
class Router(Protocol):
    """Capability, cost, latency, success rate, and load -- plus a breaker."""

    def score(self, task: Task) -> tuple[RouteScore, ...]:
        """Rank every non-tripped agent. Explainability is the point."""

    def plan(self, task: Task) -> DispatchPlan:
        """Pick mode and agents. Must exclude agents whose breaker is open."""

    def record(self, agent: str, *, ok: bool, elapsed_ms: int) -> None:
        """Feed one outcome back into the success-rate and breaker state."""


@runtime_checkable
class Aggregator(Protocol):
    """Merges several agent streams into one."""

    def merge(
        self, mode: str, streams: Sequence[Iterator[AgentChunk]]
    ) -> Iterator[AgentChunk]:
        """Yield a single increment stream. For ``race``, the losers must be
        cancelled rather than drained."""


@runtime_checkable
class IntentResolver(Protocol):
    def resolve(self, text: str) -> Intent:
        """Classify without a model call; fall back to ``kind="agent"``."""
