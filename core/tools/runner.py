"""One funnel: request -> gate -> tool -> event -> audit.

Both origins go through this object, which is what makes FR-9.8 true in code
rather than in a review comment: an agent cannot reach a capability by a path the
user's own voice could not take, because there is only one path.

Two rules about what leaves this module:

- **Events carry decisions, never content.** ``tool.executed`` reports the tool,
  the outcome and a duration. The file's text and the command's output stay in the
  ``ToolResult`` returned to the caller.
- **``shell.run``'s command *is* in ``tool.confirm_required``.** The orb has to
  show what it is about to run; a confirmation prompt that hides the command is
  worse than no prompt (FR-6.13).
"""

from __future__ import annotations

import time
import re
from typing import Any, Mapping

from core.events import AGENT_SCHEMA_PATH, build_event, validate_event

from .contract import Tool, ToolRequest, ToolResult
from .policy import DefaultToolPolicy


_SAFE_EVENT_REASONS = frozenset(
    {
        "path is outside the sandbox",
        "path is required",
        "query is required",
        "command is required",
        "command is not on the allow-list",
        "no verified speaker",
        "confirmation required",
        "tool is not registered",
        "unknown tool",
        "unknown origin",
        "no search backend is configured",
        "fs tools are disabled",
        "web tools are disabled",
        "shell tools are disabled",
        "mcp tools are disabled",
        "tool is not on the allow-list",
        "tool is not on the server's allow-list",
        "the remote tool reported an error",
        "tool failed",
    }
)
_SAFE_EVENT_PATTERNS = (
    re.compile(r"^exit code -?\d+$"),
    re.compile(r"^timed out$"),
    re.compile(r"^search backend failed: [A-Za-z_][A-Za-z0-9_]*$"),
    re.compile(r"^could not start: [A-Za-z_][A-Za-z0-9_]*$"),
    re.compile(r"^blocked shape: [a-z ]+$"),
)


def _event_reason(error: str | None) -> str:
    """Keep tool events diagnostic but never content-bearing.

    Tool implementations are replaceable and may be third-party code. Their
    ``ToolResult.error`` is useful to the local caller, but it is not safe to
    fan out to every event sink because it can contain paths, command output,
    search text, or exception messages.
    """
    if not isinstance(error, str):
        return "tool failed"
    candidate = error.strip()
    if candidate in _SAFE_EVENT_REASONS:
        return candidate
    if any(pattern.fullmatch(candidate) for pattern in _SAFE_EVENT_PATTERNS):
        return candidate
    return "tool failed"


class ToolRunner:
    """Hold the gate, the tools, and the two side channels (events, audit)."""

    def __init__(
        self,
        tools: Mapping[str, Tool] | None = None,
        *,
        policy: Any = None,
        on_event: Any = None,
        memory_writer: Any = None,
    ) -> None:
        self.policy = policy if policy is not None else DefaultToolPolicy()
        self.tools: dict[str, Tool] = dict(tools or {})
        self.on_event = on_event
        self.memory_writer = memory_writer
        #: MCP servers this runner owns, if any. Held so ``close()`` can reap the
        #: subprocesses: they are children of this process and nothing else is in a
        #: position to end them.
        self.mcp: Any = None
        self.executed = 0
        self.refused = 0
        self.confirmations = 0
        self.sink_failures = 0
        #: Audit rows the memory layer would not accept (its credential filter
        #: applies to tool rows too). Counted, not retried.
        self.audit_dropped = 0

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def close(self) -> None:
        """Release the MCP subprocesses. Idempotent; safe with none attached."""
        registry, self.mcp = self.mcp, None
        if registry is not None:
            try:
                registry.close()
            except Exception:
                # Teardown is best-effort. A server that will not die must not
                # prevent the rest of the runtime from shutting down.
                pass

    def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = validate_event(build_event(event_type, payload), AGENT_SCHEMA_PATH)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                # Event delivery is a side channel; it must not change the tool
                # decision or make a local tool failure look like a crash.
                self.sink_failures += 1
        return event

    def _audit(self, request: ToolRequest, result: ToolResult) -> None:
        """One long-layer row per decision. Failures here never fail the call."""
        if self.memory_writer is None:
            return
        decision = str(result.audit.get("decision") or ("executed" if result.ok else "refused"))
        detail = result.audit.get("command") or result.audit.get("path") or ""
        text = f"{request.tool} {decision}"
        if result.error and decision != "executed":
            text += f": {result.error}"
        if detail:
            text += f" [{detail}]"
        tags = [f"tool:{request.tool}", f"origin:{request.origin}", f"decision:{decision}"]
        try:
            if self.memory_writer.write_audit(text, tags=tags) is None:
                self.audit_dropped += 1
        except Exception:
            self.audit_dropped += 1

    def run(self, request: ToolRequest) -> ToolResult:
        """Gate, then execute. The return value is the only place content appears."""
        self._emit(
            "tool.requested",
            {
                "tool": request.tool,
                "origin": request.origin,
                "speaker_verified": bool(request.speaker),
            },
        )
        verdict = self.policy.check(request)
        if verdict is not None:
            if verdict.needs_confirmation:
                self.confirmations += 1
                self._emit(
                    "tool.confirm_required",
                    {
                        "tool": request.tool,
                        "origin": request.origin,
                        "command": str(request.arguments.get("command", "")),
                    },
                )
            else:
                self.refused += 1
                self._emit(
                    "tool.refused",
                    {
                        "tool": request.tool,
                        "origin": request.origin,
                        "reason": _event_reason(verdict.error or "refused"),
                    },
                )
            self._audit(request, verdict)
            return verdict
        tool = self.tools.get(request.tool)
        if tool is None:
            result = ToolResult(
                tool=request.tool,
                ok=False,
                error="tool is not registered",
                audit={"decision": "refused"},
            )
            self.refused += 1
            self._emit(
                "tool.refused",
                {
                    "tool": request.tool,
                    "origin": request.origin,
                    "reason": _event_reason(result.error),
                },
            )
            self._audit(request, result)
            return result
        started = time.perf_counter()
        try:
            result = tool.run(request)
        except Exception as exc:  # a tool is allowed to be buggy, not fatal
            result = ToolResult(
                tool=request.tool,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                audit={"decision": "refused"},
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if result.ok:
            self.executed += 1
        else:
            self.refused += 1
        self._emit(
            "tool.executed" if result.ok else "tool.refused",
            {
                "tool": request.tool,
                "origin": request.origin,
                "ok": result.ok,
                "duration_ms": elapsed_ms,
                **({} if result.ok else {"reason": _event_reason(result.error)}),
            },
        )
        self._audit(request, result)
        return result

    def describe(self) -> dict[str, Any]:
        """Counts and gate settings. No arguments, no output, no file contents."""
        report: dict[str, Any] = {
            "registered": sorted(self.tools),
            "executed": self.executed,
            "refused": self.refused,
            "confirmations": self.confirmations,
            "sink_failures": self.sink_failures,
            "audit_dropped": self.audit_dropped,
            "audit_attached": self.memory_writer is not None,
        }
        describe = getattr(self.policy, "describe", None)
        if callable(describe):
            report["policy"] = describe()
        if self.mcp is not None:
            report["mcp"] = self.mcp.describe()
        return report
