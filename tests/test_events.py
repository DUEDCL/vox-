"""Envelope construction and contract validation.

``core.events`` is the one place an event envelope is built, so these checks are
what stops the envelope and ``contracts/voice-events.schema.json`` from drifting
apart -- the schema is read at runtime rather than mirrored in Python.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.events import (
    ENVELOPE_VERSION,
    EventContractError,
    allowed_types,
    build_event,
    validate_event,
)


def test_allowed_types_come_from_the_contract_file():
    types = allowed_types()

    assert "wake.detected" in types
    # Defined in the contract but historically without a producer; the speaker
    # gate is what finally emits it.
    assert "wake.rejected" in types
    assert len(types) == 9


def test_build_event_produces_a_valid_envelope():
    event = build_event("wake.detected", {"keyword": "你好问问", "score": 0.87})

    assert event["version"] == ENVELOPE_VERSION
    assert event["payload"] == {"keyword": "你好问问", "score": 0.87}
    datetime.fromisoformat(event["timestamp"])
    assert validate_event(event) is event


def test_build_event_defaults_payload_to_an_empty_dict():
    assert build_event("turn.done")["payload"] == {}


def test_ids_are_unique_per_event():
    ids = {build_event("turn.done")["id"] for _ in range(50)}

    assert len(ids) == 50


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"type": "not.a.real.type"}, "not in the contract enum"),
        ({"version": "2"}, "does not match contract"),
        ({"unexpected": 1}, "outside the contract"),
    ],
)
def test_validation_rejects_contract_violations(mutation, message):
    event = build_event("turn.done") | mutation

    with pytest.raises(EventContractError, match=message):
        validate_event(event)


def test_validation_rejects_a_missing_required_key():
    event = build_event("turn.done")
    del event["timestamp"]

    with pytest.raises(EventContractError, match="missing required keys"):
        validate_event(event)
