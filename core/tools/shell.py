"""``shell.run`` -- the largest attack surface in the platform, kept shut.

"Say a sentence, execute a command on this machine" is the whole risk. The
speaker gate (ADR 002) removes other people's voices from that surface;
misrecognition and recorded replay remain, so four layers stack here and none is
optional (ADR 005):

1. ``enabled = false`` by default, in the file *and* in the shipped defaults.
2. Non-allow-listed commands are refused, never queried.
3. Allow-listed commands still need an explicit confirmation.
4. Dangerous shapes are blocked in code, where no config can reach them.

Layers 2–4 live in ``policy.py`` because both origins must cross the same gate.
This module re-checks all of them anyway, then executes with ``shell=False``, a
scrubbed environment, a timeout, and an output cap.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import Any, Mapping

from .contract import ToolRequest, ToolResult
from .policy import (
    command_is_allowed,
    dangerous_reason,
    load_tools_config,
    refuse,
    sandbox_roots,
    scrubbed_env,
)


class ShellRunTool:
    """Run one allow-listed command, without a shell interpreter."""

    name = "shell.run"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config) if config is not None else load_tools_config()
        self.settings = dict(self.config.get("shell", {}))
        self.roots = sandbox_roots(self.config)

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {"command": "str", "confirmed": "bool"},
            "enabled": bool(self.settings.get("enabled", False)),
            "allow": list(self.settings.get("allow", ())),
            "timeout_s": int(self.settings.get("timeout_s", 20)),
            "shell_interpreter": False,
        }

    def run(self, request: ToolRequest) -> ToolResult:
        command = request.arguments.get("command", "")
        if not self.settings.get("enabled", False):
            return refuse(self.name, "shell tools are disabled")
        if not isinstance(command, str) or not command.strip():
            return refuse(self.name, "command is required")
        dangerous = dangerous_reason(command)
        if dangerous is not None:
            return refuse(self.name, f"blocked shape: {dangerous}")
        if not command_is_allowed(command, self.settings.get("allow", ())):
            return refuse(self.name, "command is not on the allow-list")
        if self.settings.get("require_verified_speaker", True) and not request.speaker:
            return refuse(self.name, "no verified speaker")
        if self.settings.get("require_confirmation", True) and (
            request.arguments.get("confirmed") is not True
        ):
            return ToolResult(
                tool=self.name,
                ok=False,
                error="confirmation required",
                needs_confirmation=True,
                audit={"decision": "confirm_required", "command": command},
            )
        return self._execute(command)

    def _execute(self, command: str) -> ToolResult:
        cap = int(self.settings.get("max_output_bytes", 20000))
        cwd = self.roots[0] if self.roots else None
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # noqa: S603 -- shell=False, allow-listed
                shlex.split(command),
                cwd=str(cwd) if cwd is not None else None,
                env=scrubbed_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(self.settings.get("timeout_s", 20)),
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return refuse(self.name, "timed out", command=command)
        except (OSError, ValueError) as exc:
            return refuse(self.name, f"could not start: {type(exc).__name__}", command=command)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        output = ((completed.stdout or "") + (completed.stderr or ""))[:cap]
        return ToolResult(
            tool=self.name,
            ok=completed.returncode == 0,
            output=output,
            error=None if completed.returncode == 0 else f"exit code {completed.returncode}",
            audit={
                "decision": "executed",
                "command": command,
                "exit_code": completed.returncode,
                "duration_ms": elapsed_ms,
                "output_bytes": len(output),
            },
        )
