"""The circuit breaker (ADR 005, routing dimension five's companion).

An agent that has started failing keeps winning routes on its stale success rate,
and every turn it wins pays its full timeout before failing again. The breaker is
what stops that: N consecutive failures take an agent out of rotation for a
cooldown, and one probe decides whether it comes back.

Three states, and the middle one is the point:

- ``closed`` -- normal. Failures accumulate; a success resets the count.
- ``open`` -- skipped by the router entirely, until the cooldown elapses.
- ``half_open`` -- the cooldown has elapsed and exactly **one** probe is allowed
  through. It succeeds and the breaker closes; it fails and the cooldown starts
  again. Without this state a recovering agent would get the full traffic of a
  healthy one on no evidence at all, and a still-broken one would re-fail every
  concurrent turn admitted in the same instant.

Time is injected rather than read from the clock, because a cooldown test that
sleeps is a test that is slow and flaky at once.

Nothing here reaches the network, spawns anything, or persists: the breaker is
in-memory state about the current process. Success *rates* live in the memory
layer (ADR 004) and survive restarts; a tripped breaker deliberately does not.
An agent broken at shutdown deserves one attempt at the next start.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core.events import AGENT_SCHEMA_PATH, build_event, validate_event

#: Consecutive failures that trip the breaker. Three rather than one: a single
#: timeout is often the host, not the agent, and tripping on it would take a
#: working agent out of rotation for the whole cooldown.
DEFAULT_THRESHOLD = 3

#: Seconds an agent stays out. Long enough that a crash-looping backend is not
#: retried every turn, short enough that a restarted one is not exiled.
DEFAULT_COOLDOWN_S = 60.0

STATES = frozenset({"closed", "open", "half_open"})


@dataclass
class BreakerState:
    """One agent's health. Mutable by design -- it is a running tally."""

    agent: str
    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float | None = None
    trips: int = 0
    #: Set while a half-open probe is in flight, so a second concurrent turn is
    #: not also admitted as "the one probe".
    probe_in_flight: bool = False
    #: When the breaker entered ``half_open``. An admitted probe that is never
    #: reported expires one cooldown after this, so an abandoned probe cannot
    #: exile the agent permanently.
    half_open_at: float | None = None


class CircuitBreaker:
    """Per-agent breakers, keyed by name.

    ``allows()`` is a *query* except in one place: admitting a half-open probe
    marks it in flight, because two callers asking simultaneously must not both
    be told yes. That is the only mutation on the read path, and it is why this
    class is not a pure function of its state.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError("breaker threshold must be at least 1")
        if cooldown_s <= 0:
            raise ValueError("breaker cooldown must be positive")
        self.threshold = int(threshold)
        self.cooldown_s = float(cooldown_s)
        self._clock = clock if clock is not None else time.monotonic
        self._on_event = on_event
        self._agents: dict[str, BreakerState] = {}

    # -- state ---------------------------------------------------------------

    def state_of(self, agent: str) -> BreakerState:
        """The agent's breaker, created closed on first sight.

        An unknown agent is healthy: a name the breaker has never heard of has
        no failures against it, and defaulting to open would make a newly
        configured agent unreachable.
        """
        existing = self._agents.get(agent)
        if existing is None:
            existing = BreakerState(agent=agent)
            self._agents[agent] = existing
        return existing

    def _refresh(self, entry: BreakerState) -> BreakerState:
        """Promote ``open`` to ``half_open`` once the cooldown has elapsed.

        Also reclaims a probe that was admitted but never reported. ``allows()``
        marks the probe in flight, and only ``record()`` clears it -- so a caller
        that asks and then does not dispatch (the router scores an agent, then
        plans nothing because the task needs a capability it lacks) would leave
        the flag set forever, exiling a recoverable agent for the life of the
        process. Reclaiming after another full cooldown makes the probe a
        *reservation with an expiry* rather than a one-way door.
        """
        if entry.state == "open" and entry.opened_at is not None:
            if self._clock() - entry.opened_at >= self.cooldown_s:
                entry.state = "half_open"
                entry.probe_in_flight = False
                entry.half_open_at = self._clock()
            return entry
        if (
            entry.state == "half_open"
            and entry.probe_in_flight
            and entry.half_open_at is not None
            and self._clock() - entry.half_open_at >= self.cooldown_s
        ):
            # The probe was admitted a full cooldown ago and never reported.
            # Treat it as abandoned rather than as still running.
            entry.probe_in_flight = False
            entry.half_open_at = self._clock()
        return entry

    def allows(self, agent: str) -> bool:
        """May this agent be routed to right now?

        Admitting a half-open probe consumes it -- see the class docstring.
        """
        entry = self._refresh(self.state_of(agent))
        if entry.state == "closed":
            return True
        if entry.state == "open":
            return False
        if entry.probe_in_flight:
            return False
        entry.probe_in_flight = True
        return True

    def is_open(self, agent: str) -> bool:
        """Tripped and still cooling down. ``half_open`` reports ``False``."""
        return self._refresh(self.state_of(agent)).state == "open"

    # -- outcomes ------------------------------------------------------------

    def record(self, agent: str, *, ok: bool) -> BreakerState:
        """Feed one turn's outcome in. This is the only way state changes."""
        entry = self._refresh(self.state_of(agent))
        entry.probe_in_flight = False
        if ok:
            was = entry.state
            entry.consecutive_failures = 0
            entry.state = "closed"
            entry.opened_at = None
            if was != "closed":
                self._emit("agent.recovered", agent, {"trips": entry.trips})
            return entry
        entry.consecutive_failures += 1
        # A failed probe re-opens immediately: it already had its one chance,
        # and counting up to the threshold again would give a dead agent three
        # more full-timeout turns per cooldown.
        if entry.state == "half_open" or entry.consecutive_failures >= self.threshold:
            self._trip(entry)
        return entry

    def _trip(self, entry: BreakerState) -> None:
        entry.state = "open"
        entry.opened_at = self._clock()
        entry.trips += 1
        self._emit(
            "agent.tripped",
            entry.agent,
            {
                "consecutive_failures": entry.consecutive_failures,
                "cooldown_s": self.cooldown_s,
                "trips": entry.trips,
            },
        )

    def reset(self, agent: str | None = None) -> None:
        """Clear one agent's breaker, or all of them. For operators and tests."""
        if agent is None:
            self._agents.clear()
            return
        self._agents.pop(agent, None)

    # -- reporting -----------------------------------------------------------

    def _emit(self, event_type: str, agent: str, detail: Mapping[str, Any]) -> None:
        """Build, validate, then hand the sink a full envelope.

        Same shape as the tool runner, the memory layer and the dispatcher:
        one argument, already validated against
        ``contracts/agent-events.schema.json``. The agent name moves into the
        payload rather than riding as a second positional -- three sinks with
        three different signatures would mean the transport that consumes them
        needs three branches, and the odd one out is always the one that gets
        wired wrong.

        The breaker names an agent and a count -- never an utterance, a prompt,
        or an error body. ``agent.tripped`` fans out to every log and transport.
        """
        payload = {"agent": agent, **dict(detail)}
        event = validate_event(build_event(event_type, payload), AGENT_SCHEMA_PATH)
        if self._on_event is None:
            return
        self._on_event(event)

    def describe(self) -> dict[str, Any]:
        """Health of every agent the breaker has seen. No task text, ever."""
        return {
            "threshold": self.threshold,
            "cooldown_s": self.cooldown_s,
            "agents": {
                name: {
                    "state": self._refresh(entry).state,
                    "consecutive_failures": entry.consecutive_failures,
                    "trips": entry.trips,
                }
                for name, entry in sorted(self._agents.items())
            },
        }


__all__ = [
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_THRESHOLD",
    "STATES",
    "BreakerState",
    "CircuitBreaker",
]
