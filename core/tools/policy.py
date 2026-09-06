"""The one gate every tool request crosses (ADR 005).

Two properties are worth stating before the code, because both are easy to
erode later:

**Refusal is the default.** An unknown tool, a disabled tool, a path outside the
sandbox, a command off the allow-list -- each returns a refusing ``ToolResult``.
Nothing reaches a tool by being merely un-forbidden.

**Refused, not queried.** Prompting the user for anything off the allow-list
would train reflexive confirmation, which is worse than a flat no. Confirmation
exists for one case only: an allow-listed ``shell.run`` command, shown on the orb
before it runs.

The dangerous-command patterns in ``DANGEROUS_PATTERNS`` are **not
configurable**. A hard block that a config file can switch off is not a hard
block; the config's allow-list can only narrow what runs, never widen it.
"""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import ORIGINS, TOOL_NAMES, ToolRequest, ToolResult

DEFAULT_CONFIG_NAME = "tools.toml"


class ToolsConfigError(RuntimeError):
    """The tool config is unreadable, malformed, or has an unknown key."""


#: Shipped defaults. ``shell.enabled`` is False here as well as in the file, so
#: a deleted config file cannot turn the shell on.
DEFAULTS: dict[str, dict[str, Any]] = {
    "fs": {
        "enabled": True,
        "roots": ["."],
        "max_bytes": 262144,
        "denied_names": [
            ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
            "credentials.json", "*secret*", "*.voiceprint", "voiceprints.json",
            "*.pfx", "*.p12", ".netrc", ".npmrc", ".git-credentials",
        ],
        "denied_dirs": [
            "enrollment", "memory", ".git", ".venv", "node_modules", "__pycache__",
        ],
    },
    "web": {
        "enabled": True,
        "blocked_domains": [],
        "max_results": 5,
        "snippet_chars": 280,
        # Backend selection (see core/tools/search_backends.py). Both off by
        # default, which is what keeps "the default install talks to nobody" true.
        # A self-hosted SearxNG on loopback wins whenever one is configured.
        "searx_url": "",
        "allow_internet": False,
        "timeout_s": 8,
        # ``web.open``：把地址交给默认浏览器，不抓结果回来。默认开着，因为它不出网、
        # 不下载、不回传 —— 动作就只是「让浏览器打开一个页面」。
        "open_enabled": True,
        # ``{q}`` 是编码后的查询词。必应不需要 JS 就能出结果页。
        "open_search_url": "https://www.bing.com/search?q={q}",
    },
    # ``app.open``：语音能启动哪些本机应用。
    #
    # **白名单，不是搜索**。能启动任意可执行文件等于代码执行，而这条路的输入是语音转写 ——
    # 「打开记事本」和「打开记账本」在一个 14M 的识别器上是同一个音。所以这里是显式的
    # 「说出来的名字 → 可执行文件绝对路径」映射，表里没有的一律拒绝。
    #
    # entries 默认是空的：路径是每台机器自己的事，写死在代码里的路径在别人机器上是错的。
    # 控制台的「技能」那一栏会列出配了但文件不在的条目。
    "apps": {
        "enabled": True,
        "entries": {},
        # **装了什么就能开什么**（2026-09-03 加，默认开）。
        #
        # 使用者点名的要求：「我让他打开网易云就打开，而不是每次都需要添加名单才能打开。」
        # 开着的时候 `app.open` 在 entries / sites 都没命中时去**枚举**开始菜单的快捷方式与
        # 注册表的 App Paths（见 core/tools/app_index.py），按名字打分挑一个。
        #
        # 为什么这不等于「把话当命令执行」：候选集永远来自那两处枚举，参数只用来**在候选里
        # 挑**。发现出来的是使用者自己装的应用，开它和他在开始菜单里点一下是同一件事。
        # 歧义（两个同分）不猜，报候选让他说清楚 —— 开错一个应用会让人以为它听错了。
        #
        # 关掉它就回到「只认白名单」，和这个功能不存在时一模一样。
        "discover": True,
        # 「放点音乐」这类泛指开哪个。空 = 报错而不是在白名单里挑一个：装了三个播放器的
        # 机器上「挑一个」是抽奖，而抽错的那次用户还得自己去关。
        "default_music": "",
        # 说出来的名字 → 一个**网页**。「给我打开抖音」在这台机器上要的是网页版
        # （使用者原话：「我习惯使用网页版刷视频」），而抖音根本没装客户端。
        #
        # 和 entries 同一条规矩：白名单，不是搜索。放在 [apps] 而不是 [web] 下面，是因为
        # 「打开 X」是**一句话一个意图** —— 让意图层去分「X 是应用还是网站」等于让它知道
        # 这台机器装了什么，那正是它不该知道的。先查 entries 再查 sites，两张表都是显式的。
        "sites": {},
        # 应用名 → 「带着一个搜索词打开」的模板，``{q}`` 是那个词。
        #
        # 「我想听薛之谦的歌」要的不只是打开播放器。模板有两种形状：
        #   * `http(s)://…`  → 交给默认浏览器（网页播放器，**这条已验证可用**）
        #   * 其他（如 `orpheus://search/{q}`）→ 作为**一个 argv** 传给那个 exe
        # 没配模板时带词的请求照旧打开应用，但会明说「没法直接搜」—— 一个假装搜了的
        # 回答比一句「打开了，搜不了」糟得多。
        "play": {},
    },
    "system": {
        # `system.volume` —— 读或改**默认播放设备**的音量（「声音大一点」）。
        #
        # 默认开。它是朗读期间最自然的一句话，而那正是使用者手不在键盘上的时刻 —— 一个
        # 语音助手回答「请在任务栏上点音量图标」是这个产品最没有说服力的一种回答。
        #
        # 不需要确认卡：可逆、无数据损失、后果当场可听见。和 `app.open` 同一档。
        # 非 Windows 上这个工具**不注册**（`core/audio/winlevel.py` 是 Core Audio 的
        # ctypes 绑定），所以关掉它只在 Windows 上有意义。
        "enabled": True,
    },
    "timer": {
        # `timer.remind` —— 「二十分钟后提醒我关火」。**这是唯一让 Vox 主动开口的工具**，
        # 所以它也是唯一会往盘上写使用者说的话的工具（`.vox/reminders.json`，gitignored，
        # 播报完就删）。记忆库已经是同一个立场，而这里的量小得多、也更短命。
        #
        # 关掉它就退回「只会应答」。到期播报在 `pump()` 那一侧走和普通回答同一条路径 ——
        # 一个能自己开口的工具会绕过状态机（球不亮、静音窗不挂、打断不生效）。
        "enabled": True,
    },
    "memory": {
        # `memory.recall` —— 让 agent **主动**翻记忆（`⟦vox:tool memory.recall {"query": …}⟧`）。
        #
        # 默认开，而这不是一次新的出网授权：**记忆文本在这个工具之前就已经在出网了** ——
        # `Dispatcher._recall_context()` 每一轮都把 `facts()` + `recent_turns()` 拼进发给
        # 云端 LLM 的请求。所以这个开关控制的不是「记忆会不会出网」，是「谁决定查哪一条」。
        #
        # 关掉它就退回纯被动召回：按当前这句话去查，查到什么给什么。代价是「我上次说想买的
        # 那个东西叫什么」这类问题必然答不了 —— 使用者这句话里没有那个东西的名字。
        #
        # 只读。**没有 `memory.write`**：给模型一支能往长期记忆里写字的笔，等于让一次转写
        # 错误变成一条永久的「事实」。写入仍然只走 `write_turn` 与隐式提炼。
        "enabled": True,
    },
    "weather": {
        # `weather.now` —— 「今天天气怎么样」。**在它之前那句话的回答必然是编的**：LLM 手上
        # 没有今天的数据，而一个把气温说错五度的助手比一个说「我查不到」的助手糟得多。
        #
        # 默认开，但它**是一次对外请求** —— 所以这个开关和 `web.allow_internet`、
        # `web.open_enabled` 同一个形状：一个会出网的能力必须能被单独关掉。去的是
        # Open-Meteo 的两个固定主机（`geocoding-api.` 与 `api.open-meteo.com`），
        # 主机写死在代码里，没有任何输入能改变请求发到哪台机器。
        #
        # **不需要 key**，所以也没有「配了 key 但放错变量」这一整类失败（那一类在这个仓库里
        # 出现过三次）。
        "enabled": True,
        # 「今天天气怎么样」（没说城市）用哪个城市。**出厂是空的，这是刻意的**：填一个
        # 「北京」会让在成都的人听到北京的天气，而那句话听起来完全正常 —— 空的时候工具报
        # 一句可执行的话（「说『上海天气』就行，或者在控制台把默认城市填上」）。
        "default_city": "",
        "timeout_s": 8,
    },
    "shell": {
        "enabled": False,
        "allow": [],
        "require_confirmation": True,
        "require_verified_speaker": True,
        "timeout_s": 20,
        "max_output_bytes": 20000,
    },
}

#: Hard blocks, each with the name that goes into the refusal reason. Checked on
#: the raw command string *and* on the parsed tokens, because a pattern that
#: only looks at one of the two is trivially bypassed by quoting.
DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive delete", re.compile(r"(?i)\brm\s+(-\w+\s+)*-[a-z]*[rf][a-z]*\b")),
    ("windows recursive delete", re.compile(r"(?i)\b(?:del|erase|rd|rmdir)\b.*\s/s\b")),
    ("force push", re.compile(r"(?i)\bgit\b.*\bpush\b.*(?:--force|--force-with-lease|\s-f\b)")),
    ("hard reset", re.compile(r"(?i)\bgit\b.*\breset\b.*--hard")),
    ("force clean", re.compile(r"(?i)\bgit\b.*\bclean\b.*-[a-z]*f")),
    ("branch delete", re.compile(r"(?i)\bgit\b.*\bbranch\b.*\s-D\b")),
    ("disk format", re.compile(r"(?i)\b(?:format|mkfs(?:\.\w+)?|diskpart)\b")),
    ("raw disk write", re.compile(r"(?i)\bdd\b[^\n]*\bof=")),
    ("privilege escalation", re.compile(r"(?i)\b(?:sudo|runas|doas)\b")),
    ("power state change", re.compile(r"(?i)\b(?:shutdown|reboot|halt)\b")),
    ("pipe to interpreter", re.compile(r"(?i)\|\s*(?:ba|z|d|)sh\b|\biex\b|\bInvoke-Expression\b")),
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{")),
    ("shell metacharacter", re.compile(r"[;&|`\n\r]|\$\(|>>|(?<![0-9a-zA-Z])>")),
)

#: Environment variable names a subprocess must never inherit. Substring match,
#: case-insensitive: one list covers ANTHROPIC_AUTH_TOKEN, AWS_SECRET_ACCESS_KEY
#: and whatever the next provider calls its credential.
SENSITIVE_ENV_MARKERS = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "access_key", "credential", "auth", "session", "cookie", "private",
)


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_tools_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``config/tools.toml``; a missing file yields the shipped defaults.

    An **unknown key raises** rather than being ignored. A misspelt
    ``denied_names`` would silently widen the sandbox, and a config that looks
    like it constrains something but does not is the worst of the three states.
    """
    root = workspace_root()
    config_path = Path(
        path or os.getenv("VOX_TOOLS_CONFIG", root / "config" / DEFAULT_CONFIG_NAME)
    )
    config = {section: dict(values) for section, values in DEFAULTS.items()}
    if not config_path.is_file():
        return config
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolsConfigError(f"tool config is unreadable: {exc}") from exc
    for section, values in raw.items():
        if section not in config:
            raise ToolsConfigError(f"unknown config section: [{section}]")
        if not isinstance(values, Mapping):
            raise ToolsConfigError(f"[{section}] must be a table")
        for key, value in values.items():
            if key not in config[section]:
                raise ToolsConfigError(f"unknown config key: {section}.{key}")
            expected = type(config[section][key])
            if isinstance(config[section][key], bool) and not isinstance(value, bool):
                raise ToolsConfigError(f"{section}.{key} must be a boolean")
            if not isinstance(value, expected):
                raise ToolsConfigError(
                    f"{section}.{key} must be {expected.__name__}, got {type(value).__name__}"
                )
            config[section][key] = value
    return config


def sandbox_roots(config: Mapping[str, Any]) -> tuple[Path, ...]:
    """Absolute sandbox roots. Relative entries resolve against the workspace."""
    root = workspace_root()
    out: list[Path] = []
    for entry in config.get("fs", {}).get("roots", ["."]):
        candidate = Path(entry)
        out.append((candidate if candidate.is_absolute() else root / candidate).resolve())
    return tuple(out)


def sensitive_name(name: str, patterns: Sequence[str]) -> str | None:
    """The pattern a filename matches, or ``None``. Case-insensitive."""
    lowered = name.casefold()
    for pattern in patterns:
        if fnmatch(lowered, pattern.casefold()):
            return pattern
    return None


def resolve_in_sandbox(
    raw: str, roots: Sequence[Path]
) -> tuple[Path | None, str | None]:
    """Resolve a requested path and confirm it lands inside a sandbox root.

    ``resolve()`` runs first and symlinks are followed, so a link pointing out of
    the workspace is refused by the same check that refuses ``../../``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "path is required"
    candidate = Path(raw.strip())
    root = roots[0] if roots else workspace_root()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    for allowed in roots:
        if resolved == allowed or allowed in resolved.parents:
            return resolved, None
    return None, "path is outside the sandbox"


def dangerous_reason(command: str) -> str | None:
    """The name of the first hard-blocked shape found in a command, or ``None``."""
    text = command if isinstance(command, str) else ""
    try:
        joined = " ".join(shlex.split(text, posix=False)) if text.strip() else ""
    except ValueError:
        # An unbalanced quote is itself suspicious; check the raw string only.
        joined = ""
    for name, pattern in DANGEROUS_PATTERNS:
        if pattern.search(text) or (joined and pattern.search(joined)):
            return name
    return None


def command_is_allowed(command: str, allow: Sequence[str]) -> bool:
    """True when the command's leading tokens match an allow-list entry exactly.

    Token comparison rather than string prefix: ``git status`` must not admit
    ``git statuses`` or ``git status; rm -rf .``.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    for entry in allow:
        wanted = shlex.split(entry)
        if wanted and tokens[: len(wanted)] == wanted:
            return True
    return False


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A subprocess environment with every credential-shaped variable removed.

    Allow-listing every needed variable was the alternative; it breaks tools that
    read ``LOCALAPPDATA``, ``PROGRAMFILES`` and friends on Windows. Dropping by
    marker keeps the shell usable while guaranteeing that the token sitting in
    this process's environment is not handed to a command the user dictated.
    """
    source = os.environ if base is None else base
    return {
        key: value
        for key, value in source.items()
        if not any(marker in key.casefold() for marker in SENSITIVE_ENV_MARKERS)
    }


def refuse(tool: str, reason: str, **audit: Any) -> ToolResult:
    return ToolResult(
        tool=tool, ok=False, error=reason, audit={"decision": "refused", **audit}
    )


class DefaultToolPolicy:
    """The shipped ``ToolPolicy``. Decides only -- it never runs anything."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        mcp_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = dict(config) if config is not None else load_tools_config()
        self.roots = sandbox_roots(self.config)
        #: MCP servers, injected rather than read here. It lives in its own file
        #: (``config/mcp.toml``) because its shape is a list of subprocesses rather
        #: than a set of switches, and passing it in keeps this class testable
        #: without a second config file on disk. ``None`` means "no MCP", which is
        #: the closed answer.
        self.mcp_config = dict(mcp_config) if mcp_config is not None else None
        #: Refusal counters by reason, for ``describe()``. No arguments are kept.
        self.refusals: dict[str, int] = {}

    def _refuse(self, tool: str, reason: str, **audit: Any) -> ToolResult:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        return refuse(tool, reason, **audit)

    def check(self, request: ToolRequest) -> ToolResult | None:
        """``None`` to allow; a refusing ``ToolResult`` to deny."""
        tool = request.tool
        # MCP tools are named ``mcp.<server>.<tool>`` and are checked here, by the
        # same gate, for the reason red line 2 gives: a remote tool must not reach
        # a capability the user's own voice could not. They are not in
        # ``TOOL_NAMES`` because the set is discovered at runtime from whichever
        # servers are configured -- the switch that governs them is in the config,
        # not in a frozenset.
        if tool.startswith("mcp."):
            if request.origin not in ORIGINS:
                return self._refuse(tool, "unknown origin")
            return self._check_mcp(request)
        if tool not in TOOL_NAMES:
            return self._refuse(tool, "unknown tool")
        if request.origin not in ORIGINS:
            return self._refuse(tool, "unknown origin")
        section = tool.split(".", 1)[0]
        settings = self.config.get(section, {})
        if section in self.config and not settings.get("enabled", False):
            return self._refuse(tool, f"{section} tools are disabled")
        if tool == "fs.read":
            return self._check_fs_read(request, settings)
        if tool == "web.search":
            return self._check_web_search(request, settings)
        if tool == "shell.run":
            return self._check_shell_run(request, settings)
        return None

    def _check_mcp(self, request: ToolRequest) -> ToolResult | None:
        """Decide one ``mcp.<server>.<tool>`` request.

        Confirmation is the default rather than the exception, which is the
        opposite of every built-in tool but the same as ``shell.run``. The reason is
        the same too: an MCP tool's blast radius is whatever its author gave it, so
        the starting assumption cannot be "read-only".

        Refusals deliberately say ``unknown tool`` for a server that is absent,
        disabled, or misspelled. Distinguishing them would let a caller enumerate
        which servers this machine has configured.
        """
        tool = request.tool
        config = self.mcp_config
        if not config or not config.get("enabled", False):
            return self._refuse(tool, "mcp tools are disabled")
        parts = tool.split(".")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return self._refuse(tool, "unknown tool")
        _, server_name, remote = parts
        server = next(
            (
                entry
                for entry in config.get("servers", ())
                if getattr(entry, "name", None) == server_name and getattr(entry, "enabled", False)
            ),
            None,
        )
        if server is None:
            return self._refuse(tool, "unknown tool")
        allow = tuple(getattr(server, "allow", ()) or ())
        if allow and remote not in allow:
            return self._refuse(tool, "tool is not on the allow-list")
        auto = tuple(getattr(server, "auto_allow", ()) or ())
        if config.get("require_confirmation", True) and remote not in auto:
            if request.arguments.get("confirmed") is not True:
                # ``is True`` and not truthiness: ``"confirmed": "no"`` is a truthy
                # string, and that exact bug was caught once already in shell.run.
                return ToolResult(
                    tool=tool,
                    ok=False,
                    error="confirmation required",
                    needs_confirmation=True,
                    audit={"decision": "confirm", "server": server_name, "remote": remote},
                )
        return None

    def _check_fs_read(
        self, request: ToolRequest, settings: Mapping[str, Any]
    ) -> ToolResult | None:
        resolved, problem = resolve_in_sandbox(
            request.arguments.get("path", ""), self.roots
        )
        if problem is not None:
            return self._refuse(request.tool, problem)
        assert resolved is not None
        denied_dirs = {name.casefold() for name in settings.get("denied_dirs", ())}
        for part in resolved.parts:
            if part.casefold() in denied_dirs:
                return self._refuse(request.tool, f"path crosses a denied directory: {part}")
        matched = sensitive_name(resolved.name, settings.get("denied_names", ()))
        if matched is not None:
            return self._refuse(request.tool, f"filename matches a denied pattern: {matched}")
        return None

    def _check_web_search(
        self, request: ToolRequest, settings: Mapping[str, Any]
    ) -> ToolResult | None:
        query = request.arguments.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return self._refuse(request.tool, "query is required")
        return None

    def _check_shell_run(
        self, request: ToolRequest, settings: Mapping[str, Any]
    ) -> ToolResult | None:
        """Four layers, in the order that leaks the least information.

        Disabled is checked before anything else by ``check``; here the order is
        dangerous shape, then allow-list, then speaker, then confirmation -- so a
        blocked command is never echoed back on the orb as a pending action.
        """
        command = request.arguments.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return self._refuse(request.tool, "command is required")
        dangerous = dangerous_reason(command)
        if dangerous is not None:
            return self._refuse(request.tool, f"blocked shape: {dangerous}")
        if not command_is_allowed(command, settings.get("allow", ())):
            return self._refuse(request.tool, "command is not on the allow-list")
        if settings.get("require_verified_speaker", True) and not request.speaker:
            return self._refuse(request.tool, "no verified speaker")
        # ``is not True`` rather than falsiness: a JSON ``"confirmed": "no"`` is
        # truthy, and a confirmation flag that a stray string can set is not one.
        if settings.get("require_confirmation", True) and (
            request.arguments.get("confirmed") is not True
        ):
            return ToolResult(
                tool=request.tool,
                ok=False,
                error="confirmation required",
                needs_confirmation=True,
                audit={"decision": "confirm_required", "command": command},
            )
        return None

    def describe(self) -> dict[str, Any]:
        """What the gate is currently enforcing. Counts and flags, no arguments."""
        shell = self.config.get("shell", {})
        report: dict[str, Any] = {
            "roots": [str(root) for root in self.roots],
            "fs_enabled": bool(self.config.get("fs", {}).get("enabled", False)),
            "web_enabled": bool(self.config.get("web", {}).get("enabled", False)),
            "shell_enabled": bool(shell.get("enabled", False)),
            "shell_allow_count": len(shell.get("allow", ())),
            "dangerous_patterns": len(DANGEROUS_PATTERNS),
            "refusals": dict(self.refusals),
            "warnings": [],
        }
        if shell.get("enabled", False):
            report["warnings"].append(
                "shell.run is enabled: a misrecognised utterance can reach a command"
            )
            if not shell.get("require_confirmation", True):
                report["warnings"].append("shell.run confirmation is switched off")
            if not shell.get("require_verified_speaker", True):
                report["warnings"].append("shell.run does not require a verified speaker")
        return report
