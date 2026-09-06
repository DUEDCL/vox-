"""Tool-facing contracts for the platform's own capabilities.

The platform answers「读一下 X」「搜一下 Y」「运行 Z」itself rather than paying a
round trip to an agent. That speed comes with an attack surface, so every tool
passes a ``ToolPolicy`` before it runs -- the policy is not optional and not
per-tool discretion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

#: Built-in tool names. ``shell.run`` is disabled by default in config/tools.toml.
#:
#: ``time.now`` / ``app.open`` / ``app.close`` / ``web.open`` / ``system.volume`` / ``weather.now``
#: 是「简单的事平台自己做」那一类：查时间、打开播放器、打开一个搜索页、调音量、查天气 ——
#: 派给 agent 要几秒和一次出网，而答案（或那个动作）就在本机或一个固定的免 key 端点上。
#:
#: **这个集合是政策门的入口白名单，注册工具时容易只加一半。** 2026-09-05 实测过：
#: `weather.now` 已经 `register()` 了、已经在 `REGISTERED` 里、已经在页面上列出来了，
#: 而 `policy.check()` 因为这里没有它一律回 `unknown tool` —— 于是「今天天气怎么样」
#: 变成一句 `ok=False`，而**单元测试全绿**（它们验的是「注册了吗」，不是「跑得动吗」）。
TOOL_NAMES = frozenset(
    {
        "fs.read",
        "web.search",
        "web.open",
        "shell.run",
        "memory.recall",
        "memory.write",
        "time.now",
        "app.open",
        "app.close",
        "system.volume",
        "timer.remind",
        "weather.now",
    }
)

#: Who asked. A voice-originated request went through the speaker gate; an
#: agent-originated one did not, and is therefore trusted less, not more.
ORIGINS = frozenset({"voice", "agent"})


@dataclass(frozen=True)
class ToolRequest:
    """One tool invocation, with the provenance the policy needs to judge it.

    ``speaker`` is the verified enrolled name, or ``None`` when the speaker gate
    was explicitly disabled. A policy may refuse on ``speaker is None`` alone.
    """

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    origin: str = "voice"
    speaker: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one invocation.

    ``needs_confirmation`` means the policy allows the request in principle but
    requires an explicit user action first; the caller must surface it in the
    wake orb and re-submit. It is never auto-confirmed.
    """

    tool: str
    ok: bool
    output: str = ""
    error: str | None = None
    needs_confirmation: bool = False
    audit: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    """One capability the platform executes locally."""

    name: str

    def describe(self) -> Mapping[str, Any]:
        """Argument shape and limits, for the intent resolver and for agents."""

    def run(self, request: ToolRequest) -> ToolResult:
        """Execute. Must assume the policy already ran, and must still validate
        its own arguments -- defence in depth, not trust in the caller."""


@runtime_checkable
class ToolPolicy(Protocol):
    """The gate every request crosses before a tool sees it."""

    def check(self, request: ToolRequest) -> ToolResult | None:
        """Return ``None`` to allow, or a refusing ``ToolResult`` to deny.

        Denial is the default for anything not explicitly permitted: an unknown
        tool, a disabled tool, a path outside the sandbox, a command off the
        allow-list. Asking the user instead of refusing would only train them to
        confirm reflexively.
        """
