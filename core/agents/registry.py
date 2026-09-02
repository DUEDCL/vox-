"""``config/agents.toml`` -> adapters (ADR 003).

Loading is separate from the adapters so that a mistake in the file fails at
startup with the offending key named, rather than turning into an agent that
silently never runs. The shape check is the one written in P2 --
``contracts/agents.schema.json`` through ``validate_agents_config`` -- and this
module adds only what a JSON Schema cannot say:

- which keys belong to which ``kind`` (``command`` means nothing to ``evox``, and
  a config that appears to set something it cannot is worse than one that omits
  it),
- that two agents do not share a name, since the router addresses them by name,
- that a ``kind`` the build does not know is an error rather than a no-op.

A disabled entry is dropped before any of those checks, so an entry can sit in
the file switched off without being inspected.

An entry whose command is missing from PATH is **kept**. Availability is host
state that changes between runs, and dropping it here would make a real
misconfiguration indistinguishable from an empty registry; ``check()`` on the
adapter is where availability is reported.

No credential is ever read from this file. The EvoX bridge token comes from the
environment through ``LocalEvoXTransport.from_env``, and the schema has no key to
put one in -- writing ``token = "..."`` in the config fails validation.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.session_bridge import LocalEvoXTransport
from core.tools.policy import workspace_root

from .acp import AcpAgentAdapter
from .cli import CliAgentAdapter
from .contract import AgentAdapter
from .evox import EvoXAgentAdapter
from .http import HttpAgentAdapter
from .schema import ConfigContractError, validate_agents_config

DEFAULT_CONFIG_NAME = "agents.toml"

#: Kinds with an adapter in this build. Every kind ``AGENT_KINDS`` declares is
#: here. ``PENDING_KINDS`` stays empty until a future kind is declared in the
#: contract but not yet implemented; naming its phase in the error is then what
#: keeps "not yet" from reading as "not supported".
ADAPTER_KINDS = frozenset({"cli", "evox", "acp", "http"})
PENDING_KINDS: Mapping[str, str] = {}

#: Keys every kind accepts, and the extras each one accepts on top. The union of
#: the two must equal the schema's property list -- a test asserts it, because a
#: key added to the schema and forgotten here would be rejected as unused.
COMMON_KEYS = frozenset(
    {"name", "kind", "enabled", "capabilities", "cost", "latency_ms", "timeout_s"}
)
KIND_KEYS: Mapping[str, frozenset[str]] = {
    "cli": frozenset({"command", "args", "output", "cwd", "env_passthrough", "prompt_stdin"}),
    "evox": frozenset({"url"}),
    "acp": frozenset({"command", "args", "cwd", "env_passthrough"}),
    "http": frozenset({"url", "model", "key_env"}),
}
REQUIRED_KEYS: Mapping[str, tuple[str, ...]] = {
    "cli": ("command",),
    "acp": ("command",),
    "http": ("url",),
    "evox": (),
}

#: Descriptor fields, carried through from config to adapter unchanged.
_ROUTING_KEYS = ("cost", "latency_ms", "timeout_s")


class AgentsConfigError(ConfigContractError):
    """The agent registry is unreadable, or says something it cannot mean.

    A subclass of ``ConfigContractError`` so one ``except`` covers both the
    schema's rejections and the cross-field rules added here.
    """


def config_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.getenv(
            "VOX_AGENTS_CONFIG", workspace_root() / "config" / DEFAULT_CONFIG_NAME
        )
    )


def load_agents_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read and fully check the registry. A missing file is an empty registry.

    Empty is a usable state: the platform's own tools still run, so a machine
    with no agent installed must start rather than refuse to.
    """
    resolved = config_path(path)
    if not resolved.is_file():
        return {"agents": []}
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentsConfigError(f"agent config is unreadable: {exc}") from exc
    validate_agents_config(raw)
    _check_entries(raw.get("agents", []))
    return raw


def enabled_entries(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The entries that are switched on, in file order."""
    return tuple(
        entry
        for entry in config.get("agents", ())
        if isinstance(entry, Mapping) and entry.get("enabled", True)
    )


def open_agents(
    config: Mapping[str, Any] | None = None,
    *,
    transport: Any = None,
) -> tuple[AgentAdapter, ...]:
    """Adapters for every enabled entry. Nothing is spawned or contacted here.

    ``transport`` overrides what an ``evox`` entry would build for itself, which
    is how a test -- or a host that already holds a session -- injects one.
    """
    resolved = dict(config) if config is not None else load_agents_config()
    if config is not None:
        validate_agents_config(resolved)
        _check_entries(resolved.get("agents", []))
    return tuple(
        build_adapter(entry, transport=transport)
        for entry in enabled_entries(resolved)
    )


def build_adapter(
    entry: Mapping[str, Any], *, transport: Any = None
) -> AgentAdapter:
    """One entry -> one adapter. The entry is assumed already checked."""
    kind = entry.get("kind")
    common: dict[str, Any] = {"name": entry["name"]}
    if "capabilities" in entry:
        common["capabilities"] = frozenset(entry["capabilities"])
    for key in _ROUTING_KEYS:
        if key in entry:
            common[key] = entry[key]
    if kind == "cli":
        extra = {
            name: entry[name]
            for name in ("output", "prompt_stdin")
            if name in entry
        }
        if "cwd" in entry:
            extra["cwd"] = _resolve_cwd(entry["cwd"])
        return CliAgentAdapter(
            command=entry["command"],
            args=tuple(entry.get("args", ())),
            env_passthrough=tuple(entry.get("env_passthrough", ())),
            **common,
            **extra,
        )
    if kind == "evox":
        return EvoXAgentAdapter(
            transport if transport is not None else _bridge(entry), **common
        )
    if kind == "acp":
        extra = {name: entry[name] for name in ("cwd",) if name in entry}
        return AcpAgentAdapter(
            command=entry["command"],
            args=tuple(entry.get("args", ())),
            env_passthrough=tuple(entry.get("env_passthrough", ())),
            **common,
            **extra,
        )
    if kind == "http":
        # ``key_env`` 只是**变量名**，值永远从环境读。让配置指名变量，是因为一台机器上
        # 会有好几个中转站/服务商各自的 key —— 2026-08-31 实机就是这个：relay 的有效凭据
        # 在 `ANTHROPIC_AUTH_TOKEN` 里，而适配器只读 `VOX_AGENT_HTTP_TOKEN`，于是每一轮都
        # 401（流式路径上服务端直接断连，报出来是 `SSL: UNEXPECTED_EOF_WHILE_READING`）。
        extra = {key: entry[key] for key in ("model", "key_env") if key in entry}
        return HttpAgentAdapter(url=entry["url"], **common, **extra)
    raise AgentsConfigError(f"kind {kind!r} has no adapter")


def _bridge(entry: Mapping[str, Any]) -> LocalEvoXTransport:
    """URL may come from config; the token only ever from the environment."""
    bridge = LocalEvoXTransport.from_env()
    url = entry.get("url")
    if url:
        bridge.base_url = url
    return bridge


def _resolve_cwd(raw: Any) -> str:
    """``cwd`` 相对仓库根解析，不相对进程 cwd，并且**保证目录存在**。

    一个 agent 的工作目录决定它**能看见什么** —— 裸 CLI 会去读那个目录里的
    ``CLAUDE.md``、``git status``、随手 glob 到的文件。按进程 cwd 解析会让「从哪里启动
    Vox」改变 agent 的视野，而那不该是启动方式的函数。

    ``~`` 与 ``%VAR%`` 会被展开。**这不是便利功能，是隔离的前提**：2026-09-01 实测，
    工作目录设在仓库**内部**（`.agent-workspace`）时 `claude` 的回答里出现了本仓库的
    `git status` —— 它点名了当时正在改的两个文件。git 会从 cwd 往上找仓库根，所以
    「放在仓库里的子目录」根本不是一道边界。要真隔离，路径必须落在仓库之外，而仓库之外
    的路径不能写死在一个进版本控制的配置文件里 —— 它得是 `~/.vox/...` 这种形状。

    目录不存在就建出来。不建的后果不是「少一层隔离」：``Popen`` 会直接失败，而那一轮
    到达调用方的形状是「cannot start 'claude'」—— 读起来像命令没装。
    """
    text = os.path.expandvars(str(raw))
    path = Path(text).expanduser()
    resolved = path if path.is_absolute() else workspace_root() / path
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来（权限、路径非法）就照原样交给 Popen：它的报错比这里编一个更准确。
        pass
    return str(resolved)


def _check_entries(entries: Any) -> None:
    if not isinstance(entries, Sequence):  # pragma: no cover - schema rejects first
        raise AgentsConfigError("config.agents: expected array")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        at = f"agents[{index}]"
        name = entry.get("name")
        if name in seen:
            raise AgentsConfigError(f"{at}.name: duplicate agent name {name!r}")
        seen.add(name)
        if not entry.get("enabled", True):
            continue
        kind = entry.get("kind")
        if kind in PENDING_KINDS:
            raise AgentsConfigError(
                f"{at}.kind: {kind!r} has no adapter yet (lands in "
                f"{PENDING_KINDS[kind]}); set enabled = false to keep the entry"
            )
        if kind not in ADAPTER_KINDS:  # pragma: no cover - schema enum rejects first
            raise AgentsConfigError(f"{at}.kind: {kind!r} is not a known kind")
        for required in REQUIRED_KEYS[kind]:
            if not entry.get(required):
                raise AgentsConfigError(
                    f"{at}.{required}: required for kind {kind!r}"
                )
        unused = sorted(set(entry) - COMMON_KEYS - KIND_KEYS[kind])
        if unused:
            raise AgentsConfigError(
                f"{at}: {unused} {'is' if len(unused) == 1 else 'are'} not used "
                f"by kind {kind!r}"
            )


__all__ = [
    "ADAPTER_KINDS",
    "COMMON_KEYS",
    "DEFAULT_CONFIG_NAME",
    "KIND_KEYS",
    "PENDING_KINDS",
    "REQUIRED_KEYS",
    "AgentsConfigError",
    "build_adapter",
    "config_path",
    "enabled_entries",
    "load_agents_config",
    "open_agents",
]
