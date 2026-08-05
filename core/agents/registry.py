"""``config/agents.toml`` -> adapters (ADR 003).

Loading is separate from the adapters so that a mistake in the file fails at
startup with the offending key named, rather than turning into an agent that
silently never runs. The shape check is the one written in P2 --
``contracts/agents.schema.json`` through ``validate_agents_config`` -- and this
module adds only what a JSON Schema cannot say:

- which keys belong to which ``kind`` (``command`` means nothing to ``evox``, and
  a config that appears to set something it cannot is worse than one that omits
  it),
- that two agents do not share a name, since the router addresses them by name,
- that a ``kind`` with no adapter yet is an error rather than a no-op.

A disabled entry is dropped before any of those checks, so an ``acp`` block can
sit in the file waiting for P7 as long as it is switched off.

An entry whose command is missing from PATH is **kept**. Availability is host
state that changes between runs, and dropping it here would make a real
misconfiguration indistinguishable from an empty registry; ``check()`` on the
adapter is where availability is reported.

No credential is ever read from this file. The EvoX bridge token comes from the
environment through ``LocalEvoXTransport.from_env``, and the schema has no key to
put one in -- writing ``token = "..."`` in the config fails validation.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.session_bridge import LocalEvoXTransport
from core.tools.policy import workspace_root

from .cli import CliAgentAdapter
from .contract import AgentAdapter
from .evox import EvoXAgentAdapter
from .schema import ConfigContractError, validate_agents_config

DEFAULT_CONFIG_NAME = "agents.toml"

#: Kinds with an adapter in this build. The rest of ``AGENT_KINDS`` is declared by
#: the contract and lands later; naming the phase in the error is what keeps
#: "not yet" from reading as "not supported".
ADAPTER_KINDS = frozenset({"cli", "evox"})
PENDING_KINDS: Mapping[str, str] = {"acp": "P7", "http": "P7"}

#: Keys every kind accepts, and the extras each one accepts on top. The union of
#: the two must equal the schema's property list -- a test asserts it, because a
#: key added to the schema and forgotten here would be rejected as unused.
COMMON_KEYS = frozenset(
    {"name", "kind", "enabled", "capabilities", "cost", "latency_ms", "timeout_s"}
)
KIND_KEYS: Mapping[str, frozenset[str]] = {
    "cli": frozenset({"command", "args", "output", "cwd", "env_passthrough"}),
    "evox": frozenset({"url"}),
    "acp": frozenset({"command", "args", "cwd", "env_passthrough"}),
    "http": frozenset({"url"}),
}
REQUIRED_KEYS: Mapping[str, tuple[str, ...]] = {
    "cli": ("command",),
    "acp": ("command",),
    "http": ("url",),
    "evox": (),
}

#: Descriptor fields, carried through from config to adapter unchanged.
_ROUTING_KEYS = ("cost", "latency_ms", "timeout_s")


class AgentsConfigError(ConfigContractError):
    """The agent registry is unreadable, or says something it cannot mean.

    A subclass of ``ConfigContractError`` so one ``except`` covers both the
    schema's rejections and the cross-field rules added here.
    """


def config_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.getenv(
            "EVOX_AGENTS_CONFIG", workspace_root() / "config" / DEFAULT_CONFIG_NAME
        )
    )


def load_agents_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read and fully check the registry. A missing file is an empty registry.

    Empty is a usable state: the platform's own tools still run, so a machine
    with no agent installed must start rather than refuse to.
    """
    resolved = config_path(path)
    if not resolved.is_file():
        return {"agents": []}
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentsConfigError(f"agent config is unreadable: {exc}") from exc
    validate_agents_config(raw)
    _check_entries(raw.get("agents", []))
    return raw


def enabled_entries(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The entries that are switched on, in file order."""
    return tuple(
        entry
        for entry in config.get("agents", ())
        if isinstance(entry, Mapping) and entry.get("enabled", True)
    )


def open_agents(
    config: Mapping[str, Any] | None = None,
    *,
    transport: Any = None,
) -> tuple[AgentAdapter, ...]:
    """Adapters for every enabled entry. Nothing is spawned or contacted here.

    ``transport`` overrides what an ``evox`` entry would build for itself, which
    is how a test -- or a host that already holds a session -- injects one.
    """
    resolved = dict(config) if config is not None else load_agents_config()
    if config is not None:
        validate_agents_config(resolved)
        _check_entries(resolved.get("agents", []))
    return tuple(
        build_adapter(entry, transport=transport)
        for entry in enabled_entries(resolved)
    )


def build_adapter(
    entry: Mapping[str, Any], *, transport: Any = None
) -> AgentAdapter:
    """One entry -> one adapter. The entry is assumed already checked."""
    kind = entry.get("kind")
    common: dict[str, Any] = {"name": entry["name"]}
    if "capabilities" in entry:
        common["capabilities"] = frozenset(entry["capabilities"])
    for key in _ROUTING_KEYS:
        if key in entry:
            common[key] = entry[key]
    if kind == "cli":
        extra = {
            name: entry[name]
            for name in ("output", "cwd")
            if name in entry
        }
        return CliAgentAdapter(
            command=entry["command"],
            args=tuple(entry.get("args", ())),
            env_passthrough=tuple(entry.get("env_passthrough", ())),
            **common,
            **extra,
        )
    if kind == "evox":
        return EvoXAgentAdapter(
            transport if transport is not None else _bridge(entry), **common
        )
    raise AgentsConfigError(f"kind {kind!r} has no adapter")


def _bridge(entry: Mapping[str, Any]) -> LocalEvoXTransport:
    """URL may come from config; the token only ever from the environment."""
    bridge = LocalEvoXTransport.from_env()
    url = entry.get("url")
    if url:
        bridge.base_url = url
    return bridge


def _check_entries(entries: Any) -> None:
    if not isinstance(entries, Sequence):  # pragma: no cover - schema rejects first
        raise AgentsConfigError("config.agents: expected array")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        at = f"agents[{index}]"
        name = entry.get("name")
        if name in seen:
            raise AgentsConfigError(f"{at}.name: duplicate agent name {name!r}")
        seen.add(name)
        if not entry.get("enabled", True):
            continue
        kind = entry.get("kind")
        if kind in PENDING_KINDS:
            raise AgentsConfigError(
                f"{at}.kind: {kind!r} has no adapter yet (lands in "
                f"{PENDING_KINDS[kind]}); set enabled = false to keep the entry"
            )
        if kind not in ADAPTER_KINDS:  # pragma: no cover - schema enum rejects first
            raise AgentsConfigError(f"{at}.kind: {kind!r} is not a known kind")
        for required in REQUIRED_KEYS[kind]:
            if not entry.get(required):
                raise AgentsConfigError(
                    f"{at}.{required}: required for kind {kind!r}"
                )
        unused = sorted(set(entry) - COMMON_KEYS - KIND_KEYS[kind])
        if unused:
            raise AgentsConfigError(
                f"{at}: {unused} {'is' if len(unused) == 1 else 'are'} not used "
                f"by kind {kind!r}"
            )


__all__ = [
    "ADAPTER_KINDS",
    "COMMON_KEYS",
    "DEFAULT_CONFIG_NAME",
    "KIND_KEYS",
    "PENDING_KINDS",
    "REQUIRED_KEYS",
    "AgentsConfigError",
    "build_adapter",
    "config_path",
    "enabled_entries",
    "load_agents_config",
    "open_agents",
]
