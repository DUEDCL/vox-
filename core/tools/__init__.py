"""Platform-owned tools: read files, search the web, run commands, remember.

``policy.py`` was written before any tool that needs it, so no capability ships
ahead of its gate. ``open_tools()`` is the wiring: it builds the gate from
``config/tools.toml``, registers the tools that config permits, and returns the
runner every caller -- voice or agent -- goes through.

Importing this package must not start a subprocess or open a socket. The web tool
has no backend until one is injected, and the shell tool is off by default.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from .contract import ORIGINS, TOOL_NAMES, Tool, ToolPolicy, ToolRequest, ToolResult
from .app_close import AppCloseTool
from .apps import AppOpenTool
from .browser import WebOpenTool
from .clock import TimeNowTool
from .fs import FsReadTool
from .memory_recall import MemoryRecallTool
from .mcp import (
    McpClient,
    McpConfigError,
    McpError,
    McpRegistry,
    McpServerConfig,
    McpTool,
    load_mcp_config,
    open_mcp_tools,
)
from .reminders import TimerRemindTool
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
from .search_backends import (
    DuckDuckGoBackend,
    SearchBackendError,
    SearxBackend,
    endpoint_problem,
    open_search_backend,
)
from .shell import ShellRunTool
from .volume import SystemVolumeTool
from .web import WebSearchTool


def open_tools(
    config: Mapping[str, Any] | None = None,
    *,
    on_event: Any = None,
    memory_writer: Any = None,
    memory_recaller: Any = None,
    reminders: Any = None,
    search_backend: Any = None,
    mcp: Any = None,
) -> ToolRunner:
    """Gate plus tools, wired. ``shell.run`` is registered only when enabled.

    Registering it while disabled would work -- the gate refuses it either way --
    but leaving it out means ``describe()["registered"]`` answers "can this
    machine run commands at all" without reading the config a second time.

    ``search_backend=None`` means "decide from the config": that is what makes a
    configured SearxNG instance actually reach the tool. Passing one explicitly
    (including a fake, which is how this is tested) overrides the config.

    ``memory_recaller`` 决定 ``memory.recall`` 在不在清单里。**注入而不是自己开**：这个
    工具开一个自己的 SQLite 连接就等于绕过记忆层那把 `RLock`，而控制台是多线程的
    （HTTP 工作线程 + pump + 音频回调）。不给就没有这个工具 —— 那和「记忆没接上」是
    同一件事，不该由工具层假装它接上了。

    ``mcp`` is the same idea one step further: ``None`` reads ``config/mcp.toml``
    and starts whatever it enables (nothing, out of the box), ``False`` skips MCP
    entirely, and an ``McpRegistry`` is used as given. Starting servers here rather
    than lazily is deliberate -- a tool list that grows after the first call would
    make "what can this platform do" a question with a time-dependent answer.
    """
    resolved = dict(config) if config is not None else load_tools_config()
    if search_backend is None:
        search_backend = open_search_backend(resolved)

    mcp_config = None
    registry = None
    if mcp is not False:
        if mcp is None:
            try:
                mcp_config = load_mcp_config()
                registry = open_mcp_tools(mcp_config)
            except McpConfigError:
                # A broken MCP config must not stop the platform's own tools. It is
                # surfaced through describe() rather than by refusing to start.
                mcp_config = None
                registry = None
        else:
            registry = mcp
            mcp_config = getattr(mcp, "config_snapshot", None)

    runner = ToolRunner(
        policy=DefaultToolPolicy(resolved, mcp_config=mcp_config),
        on_event=on_event,
        memory_writer=memory_writer,
    )
    runner.register(FsReadTool(resolved))
    runner.register(WebSearchTool(resolved, backend=search_backend))
    runner.register(TimeNowTool(resolved))
    # 两个「打开点什么」的工具。各自的开关在 config/tools.toml：apps.enabled 与
    # web.open_enabled，默认都开 —— 它们不出网、不读文件、不回传。
    if resolved.get("apps", {}).get("enabled", True):
        runner.register(AppOpenTool(resolved))
        # 关应用和开应用同一个开关：一个只会开不会关的助手在使用路径上是半个。
        # 非 Windows 上不注册 —— 窗口枚举是 user32 的 ctypes 绑定。
        if sys.platform == "win32":
            runner.register(AppCloseTool(resolved))
    if resolved.get("web", {}).get("open_enabled", True):
        runner.register(WebOpenTool(resolved))
    # 记忆只在接上了的时候才成为一个工具。给一个「查不到任何东西」的 memory.recall 比
    # 没有它更糟：模型会用它，然后据「记忆里没有」下结论。
    if memory_recaller is not None and resolved.get("memory", {}).get("enabled", True):
        runner.register(MemoryRecallTool(memory_recaller, resolved))
    # 音量只在 Windows 上有实现（winlevel 是 Core Audio 的 ctypes 绑定）。别的平台上
    # **不注册**而不是注册一个恒失败的名字：`describe()["registered"]` 是「这台机器能做
    # 什么」的答案，它不该列出做不到的事。
    if sys.platform == "win32" and resolved.get("system", {}).get("enabled", True):
        runner.register(SystemVolumeTool(resolved))
    # 提醒只在**存储接上了**的时候才成为一个工具。给一个存不下东西的 `timer.remind` 是最坏
    # 的失败形状：它会答「好，二十分钟后提醒你」，然后什么都不会发生。
    if reminders is not None and resolved.get("timer", {}).get("enabled", True):
        runner.register(TimerRemindTool(resolved, store=reminders))
    if resolved.get("shell", {}).get("enabled", False):
        runner.register(ShellRunTool(resolved))
    if registry is not None:
        runner.mcp = registry
        for tool in registry.tools:
            runner.register(tool)
    return runner


__all__ = [
    "DANGEROUS_PATTERNS",
    "ORIGINS",
    "SENSITIVE_ENV_MARKERS",
    "TOOL_NAMES",
    "AppCloseTool",
    "DefaultToolPolicy",
    "DuckDuckGoBackend",
    "FsReadTool",
    "McpClient",
    "McpConfigError",
    "McpError",
    "McpRegistry",
    "McpServerConfig",
    "McpTool",
    "MemoryRecallTool",
    "SearchBackendError",
    "SearxBackend",
    "ShellRunTool",
    "SystemVolumeTool",
    "TimerRemindTool",
    "Tool",
    "ToolPolicy",
    "ToolRequest",
    "ToolResult",
    "ToolRunner",
    "ToolsConfigError",
    "WebSearchTool",
    "command_is_allowed",
    "dangerous_reason",
    "endpoint_problem",
    "load_mcp_config",
    "load_tools_config",
    "open_mcp_tools",
    "open_search_backend",
    "open_tools",
    "resolve_in_sandbox",
    "sandbox_roots",
    "scrubbed_env",
    "sensitive_name",
]
