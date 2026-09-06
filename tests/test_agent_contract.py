"""The agent contract's type surface -- design red line 2, asserted.

``core/agents/contract.py`` says in prose that every field is built from ``str``,
``int``, ``float``, ``frozenset``, ``tuple``, and ``Mapping``. Prose does not
fail a build. This file resolves the annotations for real and walks them, so an
agent SDK type, a subprocess handle, or a transport object appearing in a field
turns red here rather than leaking out through an event payload later.

The walk is structural rather than a name whitelist: ``frozenset[SomeSdkType]``
must fail, and it only does if the arguments are inspected too.

Evidence level: AUTO.
"""

from __future__ import annotations

import ast
import collections.abc
import types
import typing
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import pytest

from core.agents import contract
from core.agents.contract import (
    AGENT_KINDS,
    CHUNK_KINDS,
    AgentAdapter,
    AgentChunk,
    AgentDescriptor,
    Task,
    render_prompt,
)

#: The whole permitted vocabulary. ``NoneType`` rides along because an optional
#: field is ``str | None``, and ``Any`` is allowed only where noted below.
ALLOWED = (str, int, float, bool, frozenset, tuple, dict, type(None))
#: ``get_origin`` normalises differently than the annotation reads: ``str | None``
#: yields ``types.UnionType`` (not ``typing.Union``) and ``Mapping[str, Any]``
#: yields ``collections.abc.Mapping`` (not ``typing.Mapping``). Both spellings are
#: listed so the test does not depend on which one the contract happens to use.
ALLOWED_ORIGINS = (
    frozenset,
    tuple,
    dict,
    collections.abc.Mapping,
    typing.Union,
    types.UnionType,
)

CONTRACT_TYPES = (AgentDescriptor, Task, AgentChunk)


def _walk(annotation: Any, path: str) -> list[str]:
    """Every leaf type in an annotation, as complaints about the illegal ones."""
    origin = get_origin(annotation)
    if origin is None:
        if annotation is Any:
            return []
        if isinstance(annotation, type) and issubclass(annotation, ALLOWED):
            return []
        return [f"{path}: {annotation!r} is not a permitted contract type"]
    bad: list[str] = []
    if origin not in ALLOWED_ORIGINS and not (
        isinstance(origin, type) and issubclass(origin, ALLOWED)
    ):
        bad.append(f"{path}: container {origin!r} is not permitted")
    for index, argument in enumerate(get_args(annotation)):
        if argument is Ellipsis:
            continue
        bad.extend(_walk(argument, f"{path}[{index}]"))
    return bad


@pytest.mark.parametrize("dataclass_type", CONTRACT_TYPES, ids=lambda t: t.__name__)
def test_every_field_is_a_primitive_or_immutable_container(dataclass_type):
    hints = get_type_hints(dataclass_type)
    complaints: list[str] = []
    for name in (f.name for f in fields(dataclass_type)):
        complaints.extend(_walk(hints[name], f"{dataclass_type.__name__}.{name}"))
    assert not complaints, "\n".join(complaints)


@pytest.mark.parametrize("dataclass_type", CONTRACT_TYPES, ids=lambda t: t.__name__)
def test_the_contract_types_are_frozen_dataclasses(dataclass_type):
    # A mutable descriptor would let a router edit what an agent claimed, and a
    # mutable chunk would let one consumer rewrite what another already read.
    assert is_dataclass(dataclass_type)
    assert dataclass_type.__dataclass_params__.frozen


def test_the_walk_would_actually_catch_an_sdk_type():
    # The assertion above is only worth having if it can fail. A name whitelist
    # would pass `frozenset[FakeSdkClient]`; the structural walk must not.
    class FakeSdkClient:
        pass

    assert _walk(FakeSdkClient, "x")
    assert _walk(frozenset[FakeSdkClient], "x")
    assert _walk(tuple[str, FakeSdkClient], "x")
    assert not _walk(frozenset[str], "x")
    assert not _walk(tuple[str, ...], "x")
    assert not _walk(str | None, "x")


def test_no_field_annotation_mentions_a_third_party_module():
    # Belt to the walk's braces: catches a string annotation that never resolved
    # to a class, which `_walk` would see only as whatever it resolved to.
    banned = ("sherpa", "sounddevice", "voxcord", "subprocess", "httpx", "requests")
    for dataclass_type in CONTRACT_TYPES:
        for annotation in dataclass_type.__annotations__.values():
            rendered = str(annotation).lower()
            for name in banned:
                assert name not in rendered, f"{dataclass_type.__name__}: {annotation}"


def test_arguments_is_the_only_place_any_is_allowed():
    # `AgentChunk.arguments` is a tool call's keyword arguments, whose shape the
    # tool defines -- the gate in `core/tools/policy.py` validates it, not this
    # contract. Anywhere else, `Any` would be an escape hatch around red line 2.
    hints = {
        f"{t.__name__}.{name}": hint
        for t in CONTRACT_TYPES
        for name, hint in get_type_hints(t).items()
    }
    with_any = sorted(name for name, hint in hints.items() if Any in get_args(hint))
    assert with_any == ["AgentChunk.arguments"]


def test_the_protocol_declares_exactly_three_methods():
    # `stream` / `describe` / `cancel`. A fourth would be a capability every
    # future adapter silently owes, so growing it is a decision, not a detail.
    declared = sorted(
        name
        for name in vars(AgentAdapter)
        if not name.startswith("_") and callable(vars(AgentAdapter)[name])
    )
    assert declared == ["cancel", "describe", "stream"]


def test_kind_vocabularies_are_closed_frozensets():
    assert isinstance(AGENT_KINDS, frozenset)
    assert isinstance(CHUNK_KINDS, frozenset)
    assert AGENT_KINDS == {"cli", "acp", "http", "evox"}
    assert CHUNK_KINDS == {"text", "tool_call", "done"}


def test_the_contract_imports_nothing_that_can_spawn_or_connect():
    # Checked against the parsed imports, not the source text: the module's own
    # docstring names `subprocess` while explaining the red line, so a substring
    # search would fail on the sentence that states the rule.
    tree = ast.parse(Path(contract.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "typing"}, sorted(imported)


def test_render_prompt_puts_context_before_the_question():
    plain = Task(id="t1", text="whats the time")
    assert render_prompt(plain) == "whats the time"

    with_context = Task(id="t2", text="and now", context=("user likes tea",))
    rendered = render_prompt(with_context)
    assert rendered.index("user likes tea") < rendered.index("and now")
    assert rendered.startswith("Context:")


def test_render_prompt_keeps_context_lines_distinguishable():
    # Two context items must not merge into one line: an agent reading them back
    # as a single fact is a quieter failure than seeing none at all.
    rendered = render_prompt(
        Task(id="t3", text="q", context=("fact one", "fact two"))
    )
    assert "- fact one\n- fact two" in rendered


def test_the_shipped_agents_config_passes_the_real_checker():
    """The repo's own ``config/agents.toml``, through ``load_agents_config``.

    Every other agent test builds its entry inline, so a key the JSON Schema allows
    but ``KIND_KEYS`` does not would leave the whole suite green while the shipped
    file refuses to load -- and the first place that shows up is startup. This was
    not hypothetical: ``prompt_stdin`` passed the schema and failed the per-kind
    check, with 77 tests still passing.
    """
    from core.agents.registry import load_agents_config

    config = load_agents_config()

    assert config["agents"], "the shipped config declares no agents"
    names = [entry["name"] for entry in config["agents"]]
    assert "claude" in names
