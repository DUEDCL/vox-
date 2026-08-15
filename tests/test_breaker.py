"""Circuit breaker tests (ADR 005).

Three-state machine: closed (healthy) → open (tripped, cooling down) → half_open
(probe pending) → closed again or back to open. The point is the half-open
state: exactly one probe is admitted, and it decides whether the agent recovers
or needs another cooldown.

Coverage: threshold/cooldown validation, consecutive-failure counting, trip/open
transitions, half-open admit-once, clock injection, event emission, and reset.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from core.dispatch.breaker import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_THRESHOLD,
    CircuitBreaker,
)


# -- validation ---------------------------------------------------------------


def test_threshold_must_be_at_least_one():
    with pytest.raises(ValueError, match="threshold must be at least 1"):
        CircuitBreaker(threshold=0)


def test_cooldown_must_be_positive():
    with pytest.raises(ValueError, match="cooldown must be positive"):
        CircuitBreaker(cooldown_s=0.0)
    with pytest.raises(ValueError, match="cooldown must be positive"):
        CircuitBreaker(cooldown_s=-10.0)


# -- defaults -----------------------------------------------------------------


def test_defaults_are_sensible():
    breaker = CircuitBreaker()
    assert breaker.threshold == DEFAULT_THRESHOLD
    assert breaker.cooldown_s == DEFAULT_COOLDOWN_S


# -- closed (healthy) ---------------------------------------------------------


def test_a_new_agent_is_closed_and_allowed():
    breaker = CircuitBreaker()
    assert breaker.allows("agent-1")
    assert breaker.state_of("agent-1").state == "closed"


def test_a_success_resets_consecutive_failures():
    breaker = CircuitBreaker(threshold=3)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    assert breaker.state_of("agent-1").consecutive_failures == 2
    breaker.record("agent-1", ok=True)
    assert breaker.state_of("agent-1").consecutive_failures == 0
    assert breaker.state_of("agent-1").state == "closed"


# -- open (tripped) -----------------------------------------------------------


def test_N_consecutive_failures_trip_the_breaker():
    breaker = CircuitBreaker(threshold=3, clock=lambda: 100.0)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    assert breaker.state_of("agent-1").state == "closed"
    breaker.record("agent-1", ok=False)
    entry = breaker.state_of("agent-1")
    assert entry.state == "open"
    assert entry.consecutive_failures == 3
    assert entry.trips == 1
    assert entry.opened_at == 100.0


def test_an_open_breaker_denies_every_request():
    breaker = CircuitBreaker(threshold=2, clock=lambda: 100.0)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    assert breaker.allows("agent-1") is False
    assert breaker.allows("agent-1") is False


def test_is_open_reports_true_while_cooling_down():
    breaker = CircuitBreaker(threshold=2, cooldown_s=60.0)
    clock_value = [100.0]
    breaker._clock = lambda: clock_value[0]
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    assert breaker.is_open("agent-1") is True
    clock_value[0] = 150.0  # 50 s later, still cooling
    assert breaker.is_open("agent-1") is True


# -- half-open (probe) --------------------------------------------------------


def test_the_breaker_promotes_to_half_open_after_cooldown():
    breaker = CircuitBreaker(threshold=2, cooldown_s=60.0)
    clock_value = [100.0]
    breaker._clock = lambda: clock_value[0]
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    clock_value[0] = 160.0  # past the cooldown
    # is_open() triggers the refresh and reports False once half-open.
    assert breaker.is_open("agent-1") is False
    entry = breaker.state_of("agent-1")
    assert entry.state == "half_open"


def test_half_open_admits_exactly_one_probe():
    """The first allows() call returns True and marks the probe in flight."""
    breaker = CircuitBreaker(threshold=2, cooldown_s=60.0)
    clock_value = [100.0]
    breaker._clock = lambda: clock_value[0]
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    clock_value[0] = 160.0
    assert breaker.allows("agent-1") is True
    entry = breaker.state_of("agent-1")
    assert entry.state == "half_open"
    assert entry.probe_in_flight is True
    # Second call while the probe is in flight is refused.
    assert breaker.allows("agent-1") is False


def test_a_successful_probe_closes_the_breaker():
    """And emits agent.recovered."""
    on_event = Mock()
    breaker = CircuitBreaker(threshold=2, cooldown_s=60.0, on_event=on_event)
    clock_value = [100.0]
    breaker._clock = lambda: clock_value[0]
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    assert breaker.state_of("agent-1").trips == 1
    clock_value[0] = 160.0
    breaker.allows("agent-1")  # admit the probe
    breaker.record("agent-1", ok=True)  # probe succeeds
    entry = breaker.state_of("agent-1")
    assert entry.state == "closed"
    assert entry.consecutive_failures == 0
    assert entry.probe_in_flight is False
    assert on_event.call_count == 2  # agent.tripped + agent.recovered
    event = on_event.call_args_list[-1][0][0]
    assert event["type"] == "agent.recovered"
    assert event["payload"] == {"agent": "agent-1", "trips": 1}


def test_a_failed_probe_re_opens_the_breaker_immediately():
    """And emits a second agent.tripped."""
    on_event = Mock()
    breaker = CircuitBreaker(threshold=3, cooldown_s=60.0, on_event=on_event)
    clock_value = [100.0]
    breaker._clock = lambda: clock_value[0]
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    assert breaker.state_of("agent-1").trips == 1
    clock_value[0] = 160.0
    breaker.allows("agent-1")
    breaker.record("agent-1", ok=False)  # probe fails
    entry = breaker.state_of("agent-1")
    assert entry.state == "open"
    assert entry.opened_at == 160.0  # new cooldown starts now
    assert entry.trips == 2
    assert on_event.call_count == 2  # both trips
    event = on_event.call_args_list[-1][0][0]
    assert event["type"] == "agent.tripped"
    assert event["payload"]["trips"] == 2


# -- event emission -----------------------------------------------------------


def test_agent_tripped_is_emitted_with_counts():
    on_event = Mock()
    breaker = CircuitBreaker(threshold=3, cooldown_s=60.0, on_event=on_event)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    on_event.assert_called_once()
    (event,) = on_event.call_args[0]
    assert event["type"] == "agent.tripped"
    payload = event["payload"]
    assert payload["agent"] == "agent-1"
    assert payload["consecutive_failures"] == 3
    assert payload["cooldown_s"] == 60.0
    assert payload["trips"] == 1


def test_the_sink_receives_one_validated_envelope():
    """Not a ``(type, agent, detail)`` triple.

    The breaker used to call the sink with three positionals, which meant its
    event type was never checked against ``contracts/agent-events.schema.json``.
    That is exactly how the dispatcher shipped ``task.completed`` -- a type that
    is not in the enum -- through the whole of P6 without a red test. This
    asserts the envelope is built in ``core.events`` and validated on the way
    out, so an invented type raises here rather than reaching a transport.
    """
    on_event = Mock()
    breaker = CircuitBreaker(threshold=1, on_event=on_event)
    breaker.record("agent-1", ok=False)
    (event,) = on_event.call_args[0]
    assert set(event) == {"version", "type", "id", "timestamp", "payload"}
    assert event["version"] == "1"
    assert isinstance(event["payload"], dict)


def test_no_event_is_emitted_when_on_event_is_None():
    """Does not raise."""
    breaker = CircuitBreaker(threshold=2)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)


# -- reset --------------------------------------------------------------------


def test_reset_with_a_name_clears_one_agent():
    breaker = CircuitBreaker(threshold=2)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-2", ok=False)
    breaker.reset("agent-1")
    # agent-1 is gone; agent-2 is still there.
    assert breaker.state_of("agent-1").state == "closed"
    assert breaker.state_of("agent-2").consecutive_failures == 1


def test_reset_with_no_name_clears_all():
    breaker = CircuitBreaker(threshold=2)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-2", ok=False)
    breaker.reset()
    assert breaker.state_of("agent-1").consecutive_failures == 0
    assert breaker.state_of("agent-2").consecutive_failures == 0


# -- describe -----------------------------------------------------------------


def test_describe_reports_health_with_no_task_text():
    breaker = CircuitBreaker(threshold=3, cooldown_s=120.0)
    clock_value = [100.0]
    breaker._clock = lambda: clock_value[0]
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-1", ok=False)
    breaker.record("agent-2", ok=True)
    report = breaker.describe()
    assert report["threshold"] == 3
    assert report["cooldown_s"] == 120.0
    assert report["agents"]["agent-1"]["state"] == "open"
    assert report["agents"]["agent-1"]["consecutive_failures"] == 3
    assert report["agents"]["agent-1"]["trips"] == 1
    assert report["agents"]["agent-2"]["state"] == "closed"
    # No utterance, no prompt, no error body.
