"""Task dispatch, routing, and aggregation.

Only contracts exist at this stage. ``intent.py``, ``router.py``, ``breaker.py``,
``aggregator.py``, and ``dispatcher.py`` follow once the tool and agent layers
are in place, because the dispatcher is the one component that needs both.
"""

from __future__ import annotations

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

__all__ = [
    "DISPATCH_MODES",
    "INTENT_KINDS",
    "Aggregator",
    "DispatchPlan",
    "Intent",
    "IntentResolver",
    "RouteScore",
    "Router",
]
