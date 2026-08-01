"""``fs.read`` -- read one text file from inside the sandbox.

The policy has already resolved and admitted the path, but this tool resolves and
checks it again. Defence in depth is not redundancy here: the tool is reachable
from the dispatcher, from an agent's ``tool_call``, and from tests, and only one
of those three is guaranteed to have gone through the gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .contract import ToolRequest, ToolResult
from .policy import load_tools_config, refuse, resolve_in_sandbox, sandbox_roots, sensitive_name


class FsReadTool:
    """Read a UTF-8 text file, capped at ``fs.max_bytes``."""

    name = "fs.read"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config) if config is not None else load_tools_config()
        self.settings = dict(self.config.get("fs", {}))
        self.roots = sandbox_roots(self.config)

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {"path": "str, relative to a sandbox root"},
            "max_bytes": int(self.settings.get("max_bytes", 262144)),
            "roots": [str(root) for root in self.roots],
        }

    def run(self, request: ToolRequest) -> ToolResult:
        resolved, problem = resolve_in_sandbox(
            request.arguments.get("path", ""), self.roots
        )
        if problem is not None:
            return refuse(self.name, problem)
        assert resolved is not None
        matched = sensitive_name(resolved.name, self.settings.get("denied_names", ()))
        if matched is not None:
            return refuse(self.name, f"filename matches a denied pattern: {matched}")
        if not resolved.is_file():
            return refuse(self.name, "no such file")
        cap = int(self.settings.get("max_bytes", 262144))
        try:
            raw = resolved.read_bytes()[: cap + 1]
        except OSError as exc:
            return refuse(self.name, f"unreadable: {type(exc).__name__}")
        if b"\x00" in raw:
            # A .wav or an .onnx would otherwise arrive as mojibake in the reply
            # and, via the turn writer, in the memory store.
            return refuse(self.name, "not a text file")
        truncated = len(raw) > cap
        text = raw[:cap].decode("utf-8", errors="replace")
        return ToolResult(
            tool=self.name,
            ok=True,
            output=text,
            audit={
                "decision": "executed",
                "path": self._relative(resolved),
                "bytes": min(len(raw), cap),
                "truncated": truncated,
            },
        )

    def _relative(self, path: Path) -> str:
        """Audit paths are relative to their root: absolute paths leak the layout."""
        for root in self.roots:
            if path == root or root in path.parents:
                return path.relative_to(root).as_posix()
        return path.name
