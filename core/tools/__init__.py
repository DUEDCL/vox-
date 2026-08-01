"""Platform-owned tools: read files, search the web, run commands, remember.

``policy.py`` was written before any tool that needs it, so no capability ships
ahead of its gate. ``open_tools()`` is the wiring: it builds the gate from
``config/tools.toml``, registers the tools that config permits, and returns the
runner every caller -- voice or agent -- goes through.

Importing this package must not start a subprocess or open a socket. The web tool
has no backend until one is injected, and the shell tool is off by default.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contract import ORIGINS, TOOL_NAMES, Tool, ToolPolicy, ToolRequest, ToolResult
from .fs import FsReadTool
from .policy import (
    DANGEROUS_PATTERNS,
    SENSITIVE_ENV_MARKERS,
    DefaultToolPolicy,
    ToolsConfigError,
    command_is_allowed,
    dangerous_reason,
    load_tools_config,
    resolve_in_sandbox,
    sandbox_roots,
    scrubbed_env,
    sensitive_name,
)
from .runner import ToolRunner
from .shell import ShellRunTool
from .web import WebSearchTool


def open_tools(
    config: Mapping[str, Any] | None = None,
    *,
    on_event: Any = None,
    memory_writer: Any = None,
    search_backend: Any = None,
) -> ToolRunner:
    """Gate plus tools, wired. ``shell.run`` is registered only when enabled.

    Registering it while disabled would work -- the gate refuses it either way --
    but leaving it out means ``describe()["registered"]`` answers "can this
    machine run commands at all" without reading the config a second time.
    """
    resolved = dict(config) if config is not None else load_tools_config()
    runner = ToolRunner(
        policy=DefaultToolPolicy(resolved),
        on_event=on_event,
        memory_writer=memory_writer,
    )
    runner.register(FsReadTool(resolved))
    runner.register(WebSearchTool(resolved, backend=search_backend))
    if resolved.get("shell", {}).get("enabled", False):
        runner.register(ShellRunTool(resolved))
    return runner


__all__ = [
    "DANGEROUS_PATTERNS",
    "ORIGINS",
    "SENSITIVE_ENV_MARKERS",
    "TOOL_NAMES",
    "DefaultToolPolicy",
    "FsReadTool",
    "ShellRunTool",
    "Tool",
    "ToolPolicy",
    "ToolRequest",
    "ToolResult",
    "ToolRunner",
    "ToolsConfigError",
    "WebSearchTool",
    "command_is_allowed",
    "dangerous_reason",
    "load_tools_config",
    "open_tools",
    "resolve_in_sandbox",
    "sandbox_roots",
    "scrubbed_env",
    "sensitive_name",
]
