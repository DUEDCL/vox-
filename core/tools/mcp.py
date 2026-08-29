"""MCP (Model Context Protocol) servers as local tools, behind the same gate.

An MCP server is an external subprocess that exposes tools over JSON-RPC 2.0 on
stdio -- the same transport shape ``core/agents/acp.py`` already speaks, which is
why this is an addition rather than a new subsystem. What arrives is a list of
tools with JSON Schemas, and each one is wrapped as a local ``Tool`` so it reaches
callers through ``ToolRunner`` and ``ToolPolicy``.

That last point is the design, not an implementation detail. Red line 2 says an
agent must not reach a capability the user's own voice could not; the same
sentence read backwards says a *remote* tool must not reach one either. So there
is no second path: ``mcp.<server>.<tool>`` is checked by the same policy that
checks ``fs.read``, and the console cannot confirm it any more than it can confirm
``shell.run``.

Three defaults, all closed:

- ``[mcp] enabled`` ships ``false``, and each server has its own ``enabled``.
- ``require_confirmation`` ships ``true``. An MCP tool's blast radius is whatever
  its author gave it -- writing files, calling APIs, mutating a database -- so the
  starting point is the same as ``shell.run``'s, not the same as ``fs.read``'s.
- The child gets ``scrubbed_env()``. A credential reaches a server only by naming
  the variable in ``env_passthrough``.

Remote output is untrusted input. It is truncated, it never enters an event
payload, and only the truncated text is handed back -- the same posture
``web.search`` takes toward page text.

**Evidence level: SIM.** The tests drive a Python snippet that speaks these
shapes. No third-party MCP server has completed a call through this client yet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.agents.cli import spawn_target
from core.tools.contract import ToolRequest, ToolResult
from core.tools.policy import scrubbed_env

DEFAULT_CONFIG_NAME = "mcp.toml"

#: The protocol revision we announce. A server that requires a newer one says so
#: in its ``initialize`` result and the handshake is reported as failed rather
#: than guessed at.
PROTOCOL_VERSION = "2024-11-05"

#: Same forcing as ``acp.py``, same reason: MCP frames are UTF-8 by protocol,
#: while a Python child on Windows picks its stdio codec from the ANSI code page.
_UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: How long a terminated child gets before it is killed.
_GRACE_S = 2.0

#: A remote tool name we are willing to put in our own namespace. Restricted
#: rather than free text: the name becomes a tool id that a policy matches on, and
#: a name containing a dot would forge a different section.
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "timeout_s": 30.0,
    "max_output_bytes": 20000,
    "require_confirmation": True,
}


class McpError(RuntimeError):
    """An MCP server that cannot be reached, or a config that cannot be trusted."""


class McpConfigError(McpError):
    """The MCP config is unreadable, malformed, or has an unknown key."""


@dataclass(frozen=True)
class McpServerConfig:
    """One server entry. ``allow`` empty means every tool it offers."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    enabled: bool = False
    env_passthrough: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    auto_allow: tuple[str, ...] = ()
    timeout_s: float = 30.0


def config_path(path: str | Path | None = None) -> Path:
    root = Path(__file__).resolve().parents[2]
    return Path(path or os.getenv("VOX_MCP_CONFIG", root / "config" / DEFAULT_CONFIG_NAME))


def load_mcp_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``config/mcp.toml``. A missing file means "no MCP", not an error.

    Unknown keys raise, like every other config in this project: a misspelled
    ``auto_allow`` would silently mean "confirm everything" (harmless) or a
    misspelled ``allow`` would silently mean "expose everything" (not harmless),
    and the two cannot be told apart from the outside.
    """
    resolved = config_path(path)
    config: dict[str, Any] = {**_DEFAULTS, "servers": ()}
    if not resolved.is_file():
        return config
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise McpConfigError(f"mcp config is unreadable: {exc}") from exc
    for section in raw:
        if section not in {"mcp", "servers"}:
            raise McpConfigError(f"unknown config section: [{section}]")
    for key, value in (raw.get("mcp") or {}).items():
        if key not in _DEFAULTS:
            raise McpConfigError(f"unknown config key: mcp.{key}")
        expected = type(_DEFAULTS[key])
        if isinstance(_DEFAULTS[key], bool) and not isinstance(value, bool):
            raise McpConfigError(f"mcp.{key} must be a boolean")
        if not isinstance(_DEFAULTS[key], bool) and isinstance(value, bool):
            raise McpConfigError(f"mcp.{key} must be {expected.__name__}")
        if isinstance(_DEFAULTS[key], float) and isinstance(value, int):
            value = float(value)
        if not isinstance(value, expected):
            raise McpConfigError(f"mcp.{key} must be {expected.__name__}")
        config[key] = value
    config["servers"] = tuple(
        _server_entry(entry, index, float(config["timeout_s"]))
        for index, entry in enumerate(raw.get("servers") or ())
    )
    names = [server.name for server in config["servers"]]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate is not None:
        raise McpConfigError(f"duplicate server name: {duplicate!r}")
    return config


_SERVER_KEYS = {
    "name", "command", "args", "cwd", "enabled",
    "env_passthrough", "allow", "auto_allow", "timeout_s",
}


def _server_entry(entry: Any, index: int, default_timeout: float) -> McpServerConfig:
    where = f"servers[{index}]"
    if not isinstance(entry, Mapping):
        raise McpConfigError(f"{where} must be a table")
    for key in entry:
        if key not in _SERVER_KEYS:
            raise McpConfigError(f"unknown config key: {where}.{key}")
    name = str(entry.get("name", ""))
    if not _SERVER_NAME.match(name):
        raise McpConfigError(
            f"{where}.name must be a lowercase slug (it becomes part of the tool name), got {name!r}"
        )
    command = str(entry.get("command", ""))
    if not command:
        raise McpConfigError(f"{where}.command is required")

    def strings(key: str) -> tuple[str, ...]:
        values = entry.get(key, ())
        if isinstance(values, str) or not isinstance(values, Sequence):
            raise McpConfigError(f"{where}.{key} must be an array of strings")
        return tuple(str(item) for item in values)

    timeout = entry.get("timeout_s", default_timeout)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise McpConfigError(f"{where}.timeout_s must be a number")
    return McpServerConfig(
        name=name,
        command=command,
        args=strings("args"),
        cwd=str(entry["cwd"]) if entry.get("cwd") else None,
        enabled=bool(entry.get("enabled", False)),
        env_passthrough=strings("env_passthrough"),
        allow=strings("allow"),
        auto_allow=strings("auto_allow"),
        timeout_s=float(timeout),
    )


class McpClient:
    """One stdio JSON-RPC connection to one MCP server. Lazily started.

    Not a general JSON-RPC client: it speaks exactly ``initialize``,
    ``notifications/initialized``, ``tools/list`` and ``tools/call``, and it reads
    responses by id. Server-initiated requests are answered with a "not supported"
    error rather than ignored -- a server left waiting on a request it believes we
    received would hang the next call instead of failing it.
    """

    def __init__(self, config: McpServerConfig, *, spawn: Any = None) -> None:
        self.config = config
        self.spawn = spawn or subprocess.Popen
        self._process: Any = None
        self._next_id = 0
        self._lock = threading.Lock()
        self.tools: tuple[dict[str, Any], ...] = ()
        self.server_info: dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _child_env(self) -> dict[str, str]:
        env = scrubbed_env()
        env.update(_UTF8_ENV)
        for name in self.config.env_passthrough:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    def start(self) -> tuple[dict[str, Any], ...]:
        """Spawn, handshake, and return the tools the server offers.

        Idempotent: a second call on a live client re-lists rather than spawning a
        second process.
        """
        if self.alive:
            return self.tools
        argv = [self.config.command, *self.config.args]
        target, refusal = spawn_target(argv)
        if refusal is not None:
            raise McpError(f"{self.config.name}: {refusal}")
        try:
            self._process = self.spawn(
                target,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._child_env(),
                cwd=self.config.cwd,
                shell=isinstance(target, str),
            )
        except OSError as exc:
            raise McpError(f"{self.config.name}: could not start: {type(exc).__name__}") from exc

        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "vox", "version": "1"},
                },
            )
            self.server_info = dict(result.get("serverInfo") or {})
            self._notify("notifications/initialized", {})
            listed = self._request("tools/list", {})
        except McpError:
            self.close()
            raise
        offered = listed.get("tools")
        if not isinstance(offered, list):
            self.close()
            raise McpError(f"{self.config.name}: tools/list returned no tools array")
        self.tools = tuple(
            entry for entry in offered if isinstance(entry, Mapping) and _usable(entry)
        )
        return self.tools

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=_GRACE_S)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    # -- calls ----------------------------------------------------------------

    def call(self, tool: str, arguments: Mapping[str, Any]) -> tuple[str, bool]:
        """``tools/call`` -> (text, is_error). Starts the server if needed."""
        if not self.alive:
            self.start()
        result = self._request("tools/call", {"name": tool, "arguments": dict(arguments)})
        return _result_text(result), bool(result.get("isError"))

    def _write(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpError(f"{self.config.name}: server is not running")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"{self.config.name}: write failed: {type(exc).__name__}") from exc

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """One request, one response, matched by id.

        The lock serialises calls rather than multiplexing them. An MCP server is a
        tool provider reached one call at a time from a voice turn; a full async
        client would add a reader thread and a pending-request table to solve a
        problem this caller does not have.
        """
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params)})
            return self._read_response(rid)

    def _read_response(self, rid: int) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise McpError(f"{self.config.name}: server is not running")
        while True:
            line = process.stdout.readline()
            if not line:
                raise McpError(f"{self.config.name}: server closed the connection")
            try:
                message = json.loads(line.strip() or "{}")
            except json.JSONDecodeError:
                # A server that writes noise to stdout is out of spec; skip the
                # line rather than aborting a turn over one bad frame.
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == rid:
                error = message.get("error")
                if isinstance(error, Mapping):
                    raise McpError(
                        f"{self.config.name}: {error.get('message') or 'JSON-RPC error'}"
                    )
                result = message.get("result")
                return dict(result) if isinstance(result, Mapping) else {}
            if "method" in message and "id" in message:
                # A server-initiated request (sampling, roots). We advertise no
                # capabilities, so refuse it explicitly instead of leaving it open.
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not supported by this client"},
                    }
                )


def _usable(entry: Mapping[str, Any]) -> bool:
    """Whether a listed tool has a name we are willing to expose."""
    return bool(_REMOTE_NAME.match(str(entry.get("name", ""))))


def _result_text(result: Mapping[str, Any]) -> str:
    """The ``content`` array flattened to text. Non-text parts are named, not dropped.

    An image or a resource link is a real result and saying "[image]" is more
    honest than returning an empty string that reads like "the tool did nothing".
    """
    content = result.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type", ""))
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind:
            parts.append(f"[{kind}]")
    return "\n".join(part for part in parts if part)


class McpTool:
    """One remote tool wearing the local ``Tool`` protocol.

    ``name`` is ``mcp.<server>.<tool>``. The three-part shape matters: the policy
    splits on the first dot to find its config section, so every MCP tool lands in
    the ``mcp`` section no matter which server it came from, and one switch governs
    all of them.
    """

    def __init__(
        self,
        server: McpServerConfig,
        remote: Mapping[str, Any],
        client: McpClient,
        *,
        max_output_bytes: int = 20000,
    ) -> None:
        self.server = server
        self.remote_name = str(remote.get("name", ""))
        self.name = f"mcp.{server.name}.{self.remote_name}"
        self.description = str(remote.get("description", ""))[:400]
        self.input_schema = dict(remote.get("inputSchema") or {})
        self.client = client
        self.max_output_bytes = max_output_bytes

    def describe(self) -> Mapping[str, Any]:
        """What the intent resolver and an agent see. The remote schema is passed
        through as-is: it is the server's own description of its arguments, and
        rewriting it would be inventing a contract."""
        return {
            "name": self.name,
            "server": self.server.name,
            "remote": self.remote_name,
            "description": self.description,
            "arguments": self.input_schema.get("properties", {}),
            "required": self.input_schema.get("required", []),
            "auto_allowed": self.remote_name in self.server.auto_allow,
            "returns": "text, truncated -- never the raw transport frame",
        }

    def run(self, request: ToolRequest) -> ToolResult:
        """Call the remote tool. Assumes the policy already ran, and still checks.

        The ``allow`` list is re-checked here rather than trusted from
        registration: defence in depth is the rule the built-in tools follow
        (``fs.read`` re-resolves its path even though the policy did), and it is
        what makes a tool safe to hand to a caller that built its own runner.
        """
        if self.server.allow and self.remote_name not in self.server.allow:
            return ToolResult(
                tool=self.name,
                ok=False,
                error="tool is not on the server's allow-list",
                audit={"decision": "refused"},
            )
        try:
            text, is_error = self.client.call(self.remote_name, request.arguments)
        except McpError as exc:
            # The message can name a command or a path, so the caller gets it and
            # the event stream does not (``runner._event_reason`` filters).
            return ToolResult(
                tool=self.name, ok=False, error=f"mcp failed: {exc}", audit={"decision": "refused"}
            )
        encoded = text.encode("utf-8")
        truncated = len(encoded) > self.max_output_bytes
        if truncated:
            text = encoded[: self.max_output_bytes].decode("utf-8", errors="ignore")
        return ToolResult(
            tool=self.name,
            ok=not is_error,
            output=text,
            error="the remote tool reported an error" if is_error else None,
            audit={
                "decision": "executed" if not is_error else "refused",
                "server": self.server.name,
                "remote": self.remote_name,
                "bytes": len(encoded),
                "truncated": truncated,
            },
        )


@dataclass
class McpRegistry:
    """The clients this process owns, so they can all be closed together."""

    clients: dict[str, McpClient] = field(default_factory=dict)
    tools: tuple[McpTool, ...] = ()
    warnings: tuple[str, ...] = ()

    def close(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except Exception:
                pass
        self.clients.clear()
        self.tools = ()

    def describe(self) -> dict[str, Any]:
        return {
            "servers": [
                {
                    "name": name,
                    "alive": client.alive,
                    "tools": [str(t.get("name", "")) for t in client.tools],
                    "info": client.server_info,
                }
                for name, client in sorted(self.clients.items())
            ],
            "tools": [tool.name for tool in self.tools],
            "warnings": list(self.warnings),
        }


def open_mcp_tools(
    config: Mapping[str, Any] | None = None, *, spawn: Any = None
) -> McpRegistry:
    """Start every enabled server and wrap what they offer. Failures are warnings.

    One unreachable server must not stop the others or the platform: MCP is an
    addition to the local tools, not a prerequisite for them. Every skipped server
    is reported by name so "I enabled it and nothing happened" has an answer.
    """
    resolved = dict(config) if config is not None else load_mcp_config()
    registry = McpRegistry()
    if not resolved.get("enabled", False):
        return registry
    warnings: list[str] = []
    tools: list[McpTool] = []
    cap = int(resolved.get("max_output_bytes", 20000))
    for server in resolved.get("servers", ()):
        if not server.enabled:
            continue
        client = McpClient(server, spawn=spawn)
        try:
            offered = client.start()
        except McpError as exc:
            warnings.append(str(exc))
            client.close()
            continue
        registry.clients[server.name] = client
        for remote in offered:
            name = str(remote.get("name", ""))
            if server.allow and name not in server.allow:
                continue
            tools.append(McpTool(server, remote, client, max_output_bytes=cap))
    registry.tools = tuple(tools)
    registry.warnings = tuple(warnings)
    return registry


__all__ = [
    "PROTOCOL_VERSION",
    "McpClient",
    "McpConfigError",
    "McpError",
    "McpRegistry",
    "McpServerConfig",
    "McpTool",
    "load_mcp_config",
    "open_mcp_tools",
]
