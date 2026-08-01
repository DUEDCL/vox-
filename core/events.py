"""Single construction and validation point for event envelopes.

Every event the platform emits -- the nine voice events and the twelve platform
events -- shares one envelope::

    {"version": "1", "type": ..., "id": ..., "timestamp": ..., "payload": {...}}

Centralising it here buys two things. The envelope can gain a field in exactly
one place, and the contract's ``type`` enum is read from the schema file instead
of being duplicated in Python -- so the contract stays the single gate that
design red line 2 assumes it is.

Two contract files, not one. ``voice-events.schema.json`` is frozen byte-for-byte
at version ``"1"``; platform events therefore live in
``agent-events.schema.json`` with an identical envelope. Because the envelopes
match and the two ``type`` enums are disjoint, ``validate_any_event`` can accept
either, which is what lets the streams merge at the transport boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

ENVELOPE_VERSION = "1"

_ROOT = Path(__file__).resolve().parents[1]
VOICE_SCHEMA_PATH = _ROOT / "contracts" / "voice-events.schema.json"
AGENT_SCHEMA_PATH = _ROOT / "contracts" / "agent-events.schema.json"

#: Every contract that describes an event envelope, in resolution order.
CONTRACT_PATHS: tuple[Path, ...] = (VOICE_SCHEMA_PATH, AGENT_SCHEMA_PATH)


class EventContractError(ValueError):
    """An event envelope does not satisfy its declared contract."""


@lru_cache(maxsize=8)
def load_schema(path: Path = VOICE_SCHEMA_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def allowed_types(schema_path: Path = VOICE_SCHEMA_PATH) -> frozenset[str]:
    """The ``type`` values a contract permits, read from the contract itself."""
    return frozenset(load_schema(schema_path)["properties"]["type"]["enum"])


def build_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    version: str = ENVELOPE_VERSION,
) -> dict[str, Any]:
    """Construct one envelope. Validation is a separate, explicit step."""
    return {
        "version": version,
        "type": event_type,
        "id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }


def validate_event(
    event: dict[str, Any], schema_path: Path = VOICE_SCHEMA_PATH
) -> dict[str, Any]:
    """Check an envelope against a contract file and return it unchanged.

    Hand-rolled rather than pulling in ``jsonschema``: the contract is 14 lines,
    and a new runtime dependency to read it would be a poor trade. This covers
    everything the contract actually asserts -- required keys, the ``version``
    const, the ``type`` enum, and ``additionalProperties: false``.
    """
    schema = load_schema(schema_path)
    props: dict[str, Any] = schema.get("properties", {})

    missing = [key for key in schema.get("required", []) if key not in event]
    if missing:
        raise EventContractError(f"event is missing required keys: {missing}")

    if schema.get("additionalProperties") is False:
        extra = sorted(set(event) - set(props))
        if extra:
            raise EventContractError(f"event has keys outside the contract: {extra}")

    expected_version = props.get("version", {}).get("const")
    if expected_version is not None and event["version"] != expected_version:
        raise EventContractError(
            f"event version {event['version']!r} does not match "
            f"contract {expected_version!r}"
        )

    enum = props.get("type", {}).get("enum")
    if enum is not None and event["type"] not in enum:
        raise EventContractError(
            f"event type {event['type']!r} is not in the contract enum"
        )
    return event


def contract_for(event_type: str) -> Path:
    """The contract file that declares ``event_type``.

    Resolution is by lookup, not by convention on the name's prefix: a contract
    gains a type by being edited, and this function keeps working without a
    matching edit here. An event type declared by two contracts is an error
    rather than a silent first-match, because the envelopes are interchangeable
    and the ambiguity would never surface on its own.
    """
    owners = [path for path in CONTRACT_PATHS if event_type in allowed_types(path)]
    if not owners:
        raise EventContractError(
            f"event type {event_type!r} is not declared by any contract"
        )
    if len(owners) > 1:
        raise EventContractError(
            f"event type {event_type!r} is declared by more than one contract: "
            f"{[p.name for p in owners]}"
        )
    return owners[0]


def validate_any_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate against whichever contract declares the event's ``type``.

    This is the confluence point for the two streams. Callers that handle a
    mixed stream -- the transport boundary, the desktop bridge -- use this;
    callers that own exactly one contract keep passing its path to
    ``validate_event`` so a misrouted event still fails loudly.
    """
    if "type" not in event:
        raise EventContractError("event is missing required keys: ['type']")
    return validate_event(event, contract_for(event["type"]))
