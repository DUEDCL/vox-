"""Agent integration layer.

Two adapters exist: ``cli.py`` spawns a headless CLI, and ``evox.py`` wraps the
existing local session bridge. ``acp.py`` (JSON-RPC 2.0 over stdio) and
``http.py`` / ``openclaw.py`` (OpenAI-compatible endpoints, the OpenClaw Gateway)
land in P7 -- until then a config entry naming those kinds must be disabled, and
says so when it is not.

``open_agents()`` is the wiring: it reads ``config/agents.toml``, checks it
against ``contracts/agents.schema.json`` plus the cross-field rules a schema
cannot express, and returns one adapter per enabled entry.

Importing this package must never spawn a subprocess or open a socket, and
neither must building an adapter -- both are tested.
"""

from __future__ import annotations

from .cli import (
    OUTPUT_MODES,
    PROMPT_PLACEHOLDER,
    CliAgentAdapter,
    CliAgentError,
    spawn_target,
    which,
)
from .contract import (
    AGENT_KINDS,
    CHUNK_KINDS,
    AgentAdapter,
    AgentChunk,
    AgentDescriptor,
    Task,
    render_prompt,
)
from .evox import EvoXAgentAdapter
from .registry import (
    ADAPTER_KINDS,
    PENDING_KINDS,
    AgentsConfigError,
    build_adapter,
    enabled_entries,
    load_agents_config,
    open_agents,
)
from .schema import (
    AGENTS_SCHEMA_PATH,
    ConfigContractError,
    validate_agents_config,
)

__all__ = [
    "ADAPTER_KINDS",
    "AGENTS_SCHEMA_PATH",
    "AGENT_KINDS",
    "CHUNK_KINDS",
    "OUTPUT_MODES",
    "PENDING_KINDS",
    "PROMPT_PLACEHOLDER",
    "AgentAdapter",
    "AgentChunk",
    "AgentDescriptor",
    "AgentsConfigError",
    "CliAgentAdapter",
    "CliAgentError",
    "ConfigContractError",
    "EvoXAgentAdapter",
    "Task",
    "build_adapter",
    "enabled_entries",
    "load_agents_config",
    "open_agents",
    "render_prompt",
    "spawn_target",
    "validate_agents_config",
    "which",
]
