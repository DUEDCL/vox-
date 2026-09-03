"""装了什么就能开什么 —— 不用先把每个应用抄进白名单。

使用者的要求：「我让他打开网易云就打开，让他打开我的 B 站就打开，而不是每次都需要添加名单
才能打开。」而在这个模块之前，`app.open` 只认 `config/tools.toml` 的 `apps.entries` ——
一张要手写绝对路径的表。装了一个新应用就得回来加一行，那不是助手，是一张需要维护的清单。

## 从哪里发现应用

**开始菜单的快捷方式**，加上注册表的 `App Paths`。这两个地方合起来就是「这台机器上装了
什么」的事实来源 —— 一个人自己找应用也是去开始菜单翻。

    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\**\\*.lnk    当前用户装的
    %ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs\\**\\*.lnk 所有用户装的
    HKLM/HKCU \\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths  注册过的可执行名

**只发现，不猜路径。** 候选集永远来自这两处枚举，`app.open` 的参数只用来**在候选里挑**。
这条是安全边界：一个能把话里的字符串当路径去执行的工具，等于把「说一句话」变成了任意代码
执行，而这个项目的 `shell.run` 为此默认关着。发现出来的东西是**使用者自己装的应用**，
和他在开始菜单里点一下是同一件事。

## 为什么不用 COM 解析 .lnk

`.lnk` 是二进制的 Shell Link 格式，正经解析要 `pywin32` 或 `comtypes`。这里**不解析**：
`os.startfile(lnk)` 让 Windows 自己去跟这个快捷方式（Explorer 双击就是这么做的），所以
目标路径、工作目录、参数、图标全都由系统处理。少一个依赖，也少一类「我解析错了但看起来
成功了」的故障。

代价是拿不到目标 exe 的路径，所以 `describe()` 报的是快捷方式名而不是可执行文件 ——
而那恰好是使用者说的那个词。

## 匹配：先精确，再前缀，再包含，都要过同一道分数

「网易云」要能匹配到「网易云音乐」，但「音乐」不该匹配到随便一个带「音乐」的东西。所以
三级打分而不是一个 `in`：精确 100、开头 80、包含 60，再按名字长度**反向**加权（同样含
「网易云」时短的那个更可能是本体）。低于 `MIN_SCORE` 一律不认 —— 宁可说「没找到」，
也不要开错一个应用。

歧义（两个同分）**不猜**：返回全部候选让上层报出来。开错一个应用比说「你是说这两个里的
哪一个」更糟，因为前者会让人以为它听错了。

证据等级：AUTO（枚举被注入，不碰真注册表）。真机开一次网易云是 REAL-WIN。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

#: 分数下限。低于它就当没找到 —— 开错一个应用比说「没找到」更糟。
MIN_SCORE = 55

#: 扫描的层数上限。开始菜单基本是两三层，再深的是安装器自己造的目录树。
MAX_DEPTH = 4

#: 一次扫描最多看多少个文件。一个装了几百个应用的机器上，无上限的递归会让第一次唤醒
#: 卡住几秒 —— 而那正是最不该等的时候。
MAX_ENTRIES = 4000

#: 这些后缀在比名字时被剥掉。「网易云音乐」和「网易云」要能互相认出来。
_NOISE = (
    "音乐",
    "浏览器",
    "客户端",
    "桌面版",
    "电脑版",
    "官方版",
    "app",
    "desktop",
    "for windows",
    "(x64)",
    "(x86)",
    "64 位",
    "32 位",
)

#: 开始菜单里这些一律跳过：它们是卸载器、帮助文件和文档，开起来只会让人困惑。
_SKIP_WORDS = (
    "卸载",
    "uninstall",
    "帮助",
    "help",
    "readme",
    "说明",
    "文档",
    "documentation",
    "官网",
    "website",
    "反馈",
    "feedback",
    "修复",
    "repair",
    "命令提示符",
    "powershell",
)


def _fold(text: str) -> str:
    """比名字用的形式：去空格、去大小写、去噪声词。"""
    value = str(text or "").strip().casefold()
    for token in ("  ", " ", "-", "_", ".", "·"):
        value = value.replace(token, "")
    return value


def _strip_noise(folded: str) -> str:
    value = folded
    for token in _NOISE:
        value = value.replace(_fold(token), "")
    return value or folded


def _score(wanted: str, candidate: str) -> int:
    """一个候选和使用者说的那个词有多像。0 = 不像。

    三级而不是一个 ``in``：「网易云」要匹配到「网易云音乐」，但「音乐」不该匹配到随便一个
    带「音乐」的东西。名字长度**反向**加权 —— 同样含「网易云」时短的那个更可能是本体
    （「网易云音乐」vs「网易云音乐音效插件」）。

    **噪声词只从候选那一侧剥，不从使用者说的那一侧剥。** 这条是实测改的：两侧都剥的话
    「QQ音乐」会剥成「QQ」，然后精确匹配到本机的「QQ」—— 开出来是聊天软件而不是播放器。
    单侧剥仍然让短查询匹配长名字（「网易云」→「网易云音乐」），但不会把查询里的信息丢掉。
    """
    want = _fold(wanted)
    have = _fold(candidate)
    if not want or not have:
        return 0
    bare_have = _strip_noise(have)
    if have == want or bare_have == want:
        base = 100
    elif have.startswith(want) or bare_have.startswith(want):
        base = 80
    elif want in have:
        base = 60
    else:
        return 0
    # 长度惩罚最多扣 15：够拉开「网易云音乐」和「网易云音乐音效插件」，
    # 不至于让一个名字稍长的正确应用掉到下限以下。
    penalty = min(15, max(0, len(have) - len(want)) // 2)
    return base - penalty


@dataclass(frozen=True)
class Discovered:
    """一个发现出来的可启动项。"""

    label: str
    #: 交给 ``os.startfile`` / opener 的东西。快捷方式就是 ``.lnk`` 本身。
    target: str
    source: str

    @property
    def exists(self) -> bool:
        try:
            return Path(self.target).exists()
        except OSError:
            return False


def _start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    for variable, tail in (
        ("APPDATA", "Microsoft/Windows/Start Menu/Programs"),
        ("ProgramData", "Microsoft/Windows/Start Menu/Programs"),
    ):
        base = os.environ.get(variable, "").strip()
        if base:
            roots.append(Path(base) / tail)
    extra = os.environ.get("VOX_APP_SCAN_DIRS", "").strip()
    for piece in extra.split(os.pathsep):
        if piece.strip():
            roots.append(Path(piece.strip()))
    return roots


def scan_start_menu(roots: Iterable[Path] | None = None) -> list[Discovered]:
    """开始菜单里的快捷方式。

    ``.lnk`` **不解析**：``os.startfile`` 让 Windows 自己去跟它，和 Explorer 双击一样。
    少一个 `pywin32` 依赖，也少一类「解析错了但看起来成功」的故障。
    """
    found: list[Discovered] = []
    seen: set[str] = set()
    budget = MAX_ENTRIES
    for root in roots if roots is not None else _start_menu_roots():
        if not root.is_dir():
            continue
        for path in _walk(root, MAX_DEPTH):
            budget -= 1
            if budget <= 0:
                return found
            if path.suffix.casefold() not in (".lnk", ".url", ".appref-ms"):
                continue
            label = path.stem
            folded = _fold(label)
            if not folded or folded in seen:
                continue
            if any(_fold(word) in folded for word in _SKIP_WORDS):
                continue
            seen.add(folded)
            found.append(Discovered(label=label, target=str(path), source="start-menu"))
    return found


def _walk(root: Path, depth: int) -> Iterable[Path]:
    """有深度上限的遍历。``os.walk`` 不带上限，而安装器造的目录树可以很深。"""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, level = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if level < depth:
                        stack.append((entry, level + 1))
                    continue
            except OSError:
                continue
            yield entry


def scan_app_paths() -> list[Discovered]:
    """注册表 ``App Paths`` 里注册过的可执行名（`chrome.exe`、`msedge.exe` 这类）。

    ``winreg`` 只在 Windows 上有，别的平台直接返回空 —— 这个模块不该因为在 Linux 上
    import 就炸。
    """
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except Exception:  # noqa: BLE001
        return []
    found: list[Discovered] = []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (getattr(winreg, "HKEY_LOCAL_MACHINE"), getattr(winreg, "HKEY_CURRENT_USER")):
        try:
            with winreg.OpenKey(hive, key_path) as handle:
                count = winreg.QueryInfoKey(handle)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(handle, index)
                        with winreg.OpenKey(handle, name) as entry:
                            target = str(winreg.QueryValue(entry, "") or "").strip('"')
                    except OSError:
                        continue
                    if not target:
                        continue
                    label = Path(name).stem
                    if any(_fold(word) in _fold(label) for word in _SKIP_WORDS):
                        continue
                    found.append(
                        Discovered(label=label, target=target, source="app-paths")
                    )
        except OSError:
            continue
    return found


@dataclass
class AppIndex:
    """这台机器上能开的东西，扫一次缓存住。

    缓存是因为扫描要几十到几百毫秒，而它发生在唤醒之后 —— 那是最不该等的时候。
    ``ttl_s`` 之后过期：装了新应用不用重启 Vox 才能开它。
    """

    scanners: tuple[Callable[[], list[Discovered]], ...] = ()
    ttl_s: float = 300.0
    _items: tuple[Discovered, ...] = field(default=(), repr=False)
    _stamped: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        if not self.scanners:
            self.scanners = (scan_start_menu, scan_app_paths)

    def items(self, *, refresh: bool = False) -> tuple[Discovered, ...]:
        import time

        if not refresh and self._items and time.monotonic() - self._stamped < self.ttl_s:
            return self._items
        found: list[Discovered] = []
        seen: set[str] = set()
        for scanner in self.scanners:
            try:
                batch = scanner()
            except Exception:  # noqa: BLE001 - 一个扫描器坏了不该让整条路不可用
                continue
            for item in batch:
                folded = _fold(item.label)
                if folded and folded not in seen:
                    seen.add(folded)
                    found.append(item)
        self._items = tuple(found)
        self._stamped = time.monotonic()
        return self._items

    def find(self, wanted: str) -> tuple[list[Discovered], int]:
        """按名字找。返回 (并列第一的候选, 分数)。

        **歧义不猜**：两个同分就把两个都返回，让上层去报「你是说哪一个」。开错一个应用
        比问一句更糟 —— 前者会让人以为它听错了。
        """
        scored: list[tuple[int, Discovered]] = []
        for item in self.items():
            points = _score(wanted, item.label)
            if points >= MIN_SCORE:
                scored.append((points, item))
        if not scored:
            return [], 0
        best = max(points for points, _ in scored)
        return [item for points, item in scored if points == best], best

    def labels(self) -> list[str]:
        return sorted(item.label for item in self.items())


__all__ = [
    "MAX_DEPTH",
    "MAX_ENTRIES",
    "MIN_SCORE",
    "AppIndex",
    "Discovered",
    "scan_app_paths",
    "scan_start_menu",
]
