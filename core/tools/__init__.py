"""Platform-owned tools: read files, search the web, run commands, remember.

Only contracts exist at this stage. ``policy.py`` is written before any tool that
needs it, so no tool can ship ahead of its gate.
"""

from __future__ import annotations

from .contract import ORIGINS, TOOL_NAMES, Tool, ToolPolicy, ToolRequest, ToolResult

__all__ = [
    "ORIGINS",
    "TOOL_NAMES",
    "Tool",
    "ToolPolicy",
    "ToolRequest",
    "ToolResult",
]
