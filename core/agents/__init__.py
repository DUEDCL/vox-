"""Agent integration layer.

Contracts and their validation exist at this stage. Adapters land in phase order:
``cli.py`` and ``evox.py`` first (widest coverage, and no regression against
today's behaviour), then ``acp.py`` (JSON-RPC 2.0 over stdio, the emerging
standard) and ``http.py`` / ``openclaw.py`` (OpenAI-compatible endpoints and the
OpenClaw Gateway).

Importing this package must never spawn a subprocess or open a socket.
"""

from __future__ import annotations

from .contract import (
    AGENT_KINDS,
    CHUNK_KINDS,
    AgentAdapter,
    AgentChunk,
    AgentDescriptor,
    Task,
)
from .schema import (
    AGENTS_SCHEMA_PATH,
    ConfigContractError,
    validate_agents_config,
)

__all__ = [
    "AGENT_KINDS",
    "AGENTS_SCHEMA_PATH",
    "CHUNK_KINDS",
    "AgentAdapter",
    "AgentChunk",
    "AgentDescriptor",
    "ConfigContractError",
    "Task",
    "validate_agents_config",
]
