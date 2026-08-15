"""Task dispatch and routing layer (ADR 005).

Intent classification → agent selection → parallel execution → result aggregation.
Three modes: ``single`` (default), ``race``, ``fanout`` (explicit only).
"""

from __future__ import annotations

from .aggregator import DefaultAggregator
from .breaker import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_THRESHOLD,
    STATES,
    BreakerState,
    CircuitBreaker,
)
from .contract import (
    DISPATCH_MODES,
    INTENT_KINDS,
    Aggregator,
    DispatchPlan,
    Intent,
    IntentResolver,
    Router,
    RouteScore,
)
from .dispatcher import DispatchResult, Dispatcher
from .intent import RuleBasedIntentResolver
from .router import (
    COST_MAX,
    COST_MIN,
    DEFAULT_WEIGHTS,
    LATENCY_MAX_MS,
    LATENCY_MIN_MS,
    DefaultRouter,
)

__all__ = [
    # Contract
    "DISPATCH_MODES",
    "INTENT_KINDS",
    "Aggregator",
    "DispatchPlan",
    "Intent",
    "IntentResolver",
    "RouteScore",
    "Router",
    # Implementations
    "DefaultAggregator",
    "DefaultRouter",
    "Dispatcher",
    "DispatchResult",
    "RuleBasedIntentResolver",
    # Circuit breaker
    "CircuitBreaker",
    "BreakerState",
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_THRESHOLD",
    "STATES",
    # Router constants
    "COST_MAX",
    "COST_MIN",
    "DEFAULT_WEIGHTS",
    "LATENCY_MAX_MS",
    "LATENCY_MIN_MS",
]
