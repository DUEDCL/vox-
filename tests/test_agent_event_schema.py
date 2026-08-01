"""The platform event contract and the agent-registry contract.

Two properties matter more than the individual field checks. First,
``contracts/voice-events.schema.json`` must stay byte-identical at version
``"1"`` -- pinned here by digest, because "we didn't mean to change it" is not a
guarantee. Second, the two event contracts must stay *interchangeable at the
envelope*: same required keys, same version, disjoint type enums. That is what
lets one stream carry both.

Evidence level: AUTO.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from core.agents.schema import (
    AGENTS_SCHEMA_PATH,
    SCHEMA_ANNOTATIONS,
    SUPPORTED_KEYWORDS,
    ConfigContractError,
    validate_agents_config,
)
from core.events import (
    AGENT_SCHEMA_PATH,
    CONTRACT_PATHS,
    VOICE_SCHEMA_PATH,
    EventContractError,
    allowed_types,
    build_event,
    contract_for,
    load_schema,
    validate_any_event,
    validate_event,
)

#: Measured 2026-08-02 on the committed file: 575 bytes. NFR-5.8 says this file
#: does not change; the digest is how that stops being an intention.
VOICE_SCHEMA_SHA256 = (
    "4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5"
)

ENVELOPE_KEYS = {"version", "type", "id", "timestamp", "payload"}


def test_voice_contract_is_byte_identical():
    raw = VOICE_SCHEMA_PATH.read_bytes()

    assert hashlib.sha256(raw).hexdigest() == VOICE_SCHEMA_SHA256, (
        "voice-events.schema.json changed. Platform events belong in "
        "agent-events.schema.json; if a voice event genuinely has to change, "
        "that is an envelope version bump, not an edit in place."
    )


def test_platform_contract_declares_the_expected_surface():
    schema = load_schema(AGENT_SCHEMA_PATH)
    types = allowed_types(AGENT_SCHEMA_PATH)

    assert schema["properties"]["version"]["const"] == "1"
    assert schema["additionalProperties"] is False
    # Four producers, each landing in a named phase: dispatch (P6), the breaker
    # (P6), the tool gate (P4), and memory (P3).
    assert {t for t in types if t.startswith("task.")} == {
        "task.dispatched",
        "task.progress",
        "task.done",
        "task.failed",
    }
    assert {t for t in types if t.startswith("agent.")} == {
        "agent.tripped",
        "agent.recovered",
    }
    assert {t for t in types if t.startswith("tool.")} == {
        "tool.requested",
        "tool.confirm_required",
        "tool.executed",
        "tool.refused",
    }
    assert {t for t in types if t.startswith("memory.")} == {
        "memory.written",
        "memory.recalled",
    }
    assert len(types) == 12


def test_the_two_envelopes_can_merge():
    voice = load_schema(VOICE_SCHEMA_PATH)
    platform = load_schema(AGENT_SCHEMA_PATH)

    assert set(voice["required"]) == set(platform["required"])
    assert set(voice["properties"]) == set(platform["properties"]) == ENVELOPE_KEYS
    assert (
        voice["properties"]["version"]["const"]
        == platform["properties"]["version"]["const"]
    )
    assert voice["additionalProperties"] == platform["additionalProperties"] is False


def test_type_enums_are_disjoint():
    overlap = allowed_types(VOICE_SCHEMA_PATH) & allowed_types(AGENT_SCHEMA_PATH)

    assert not overlap, f"a type declared twice makes contract_for ambiguous: {overlap}"


@pytest.mark.parametrize(
    "event_type, expected",
    [
        ("wake.rejected", VOICE_SCHEMA_PATH),
        ("state.changed", VOICE_SCHEMA_PATH),
        ("task.dispatched", AGENT_SCHEMA_PATH),
        ("memory.written", AGENT_SCHEMA_PATH),
    ],
)
def test_contract_for_resolves_to_the_declaring_file(event_type, expected):
    assert contract_for(event_type) == expected


def test_contract_for_rejects_an_undeclared_type():
    with pytest.raises(EventContractError, match="not declared by any contract"):
        contract_for("task.invented")


def test_every_declared_type_resolves():
    for path in CONTRACT_PATHS:
        for event_type in allowed_types(path):
            assert contract_for(event_type) == path


def test_validate_any_event_accepts_both_streams():
    voice = build_event("wake.detected", {"keyword": "你好问问", "score": 0.81})
    platform = build_event("task.dispatched", {"mode": "single", "agents": ["claude"]})

    assert validate_any_event(voice) is voice
    assert validate_any_event(platform) is platform


def test_a_platform_event_still_fails_against_the_voice_contract():
    """The lenient path is opt-in: a caller that names a contract keeps its gate."""
    event = build_event("tool.refused", {"tool": "shell.run"})

    with pytest.raises(EventContractError, match="not in the contract enum"):
        validate_event(event, VOICE_SCHEMA_PATH)


def test_validate_any_event_reports_a_missing_type():
    event = build_event("task.done")
    del event["type"]

    with pytest.raises(EventContractError, match="missing required keys"):
        validate_any_event(event)


# --- agent registry contract -------------------------------------------------


def valid_config():
    return {
        "agents": [
            {
                "name": "claude",
                "kind": "cli",
                "command": "claude",
                "args": ["-p"],
                "capabilities": ["code", "reason"],
                "cost": 4,
                "latency_ms": 2500,
                "timeout_s": 120.0,
            },
            {"name": "evox", "kind": "evox", "enabled": True},
        ]
    }


def test_a_representative_registry_validates():
    data = valid_config()

    assert validate_agents_config(data) is data


def test_an_empty_registry_is_allowed():
    """Nothing registered is a usable state -- the platform's own tools still run."""
    validate_agents_config({"agents": []})


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"name": None}, r"agents\[0\].name: expected string"),
        ({"name": ""}, r"agents\[0\].name: must not be shorter"),
        ({"kind": "grpc"}, r"agents\[0\].kind: 'grpc' is not one of"),
        ({"cost": 9}, r"agents\[0\].cost: must be at most 5"),
        ({"cost": 0}, r"agents\[0\].cost: must be at least 1"),
        ({"cost": True}, r"agents\[0\].cost: expected integer, got bool"),
        ({"latency_ms": -1}, r"agents\[0\].latency_ms: must be at least 0"),
        ({"timeout_s": 0}, r"agents\[0\].timeout_s: must be greater than 0"),
        ({"args": "-p"}, r"agents\[0\].args: expected array"),
        ({"args": ["-p", 2]}, r"agents\[0\].args\[1\]: expected string"),
        ({"retries": 3}, r"agents\[0\]: unknown keys \['retries'\]"),
    ],
)
def test_registry_rejects_bad_entries(mutation, message):
    data = valid_config()
    data["agents"][0].update(mutation)

    with pytest.raises(ConfigContractError, match=message):
        validate_agents_config(data)


@pytest.mark.parametrize("missing", ["name", "kind"])
def test_registry_requires_name_and_kind(missing):
    data = valid_config()
    del data["agents"][0][missing]

    with pytest.raises(ConfigContractError, match=f"missing required key '{missing}'"):
        validate_agents_config(data)


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, r"config: missing required key 'agents'"),
        ({"agents": {}}, r"config.agents: expected array"),
        ({"agents": [], "default": "claude"}, r"config: unknown keys \['default'\]"),
        ({"agents": ["claude"]}, r"config.agents\[0\]: expected object"),
    ],
)
def test_registry_rejects_bad_top_level(data, message):
    with pytest.raises(ConfigContractError, match=message):
        validate_agents_config(data)


def test_registry_kinds_match_the_python_contract():
    """One list of kinds. A schema enum drifting from AGENT_KINDS would let a
    config validate and then fail at adapter construction."""
    from core.agents.contract import AGENT_KINDS

    schema = json.loads(AGENTS_SCHEMA_PATH.read_text(encoding="utf-8"))
    enum = schema["$defs"]["agent"]["properties"]["kind"]["enum"]

    assert frozenset(enum) == AGENT_KINDS


def test_the_schema_stays_inside_the_validator_subset():
    """An unimplemented keyword would read as a constraint while enforcing
    nothing -- worse than not declaring it."""
    schema = json.loads(AGENTS_SCHEMA_PATH.read_text(encoding="utf-8"))
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            seen.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    unsupported = seen - SUPPORTED_KEYWORDS - _schema_field_names(schema)

    assert not unsupported, f"validator does not implement: {sorted(unsupported)}"


def _schema_field_names(schema):
    """Property names and the ``agent`` definition key: dict keys that are data,
    not keywords."""
    names = set(SCHEMA_ANNOTATIONS) | {"agent"}
    names.update(schema["properties"])
    names.update(schema["$defs"]["agent"]["properties"])
    return names
