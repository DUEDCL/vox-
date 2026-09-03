"""``app.open`` —— 打开使用者说的那个东西：先本机白名单应用，再配好的网站。

## 为什么必须是白名单

能启动任意可执行文件等于代码执行。语音是这条路的入口，而语音会被转写错 —— 「打开记事本」
和「打开记账本」在 14M 的识别器上是同一个音。所以这个工具只认一张**显式的名字 → 可执行
文件**映射表，表里没有的名字一律拒绝，而不是去猜、去搜索、去 PATH 里找一个同名的东西。

同一条理由让它**不接受路径参数**：接受路径就等于放弃白名单，因为任何可执行文件都能被写成
一个路径。

## 为什么网站也在这里

「给我打开抖音」在这台机器上要的是网页版（使用者原话：「我习惯使用网页版刷视频」），而抖音
根本没装客户端。**「打开 X」是一句话一个意图** —— 让意图层去分「X 是应用还是网站」等于让它
知道这台机器装了什么，那正是它不该知道的（见 `core/dispatch/intent.py` 里
`_looks_like_app_name` 的注释）。所以这里先查 `apps.entries`，再查 `apps.sites`，两张表
都是显式白名单，安全姿态一模一样。

## 带着一个搜索词打开

「我想听薛之谦的歌」要的不只是打开播放器。`apps.play` 给每个应用配一个模板（`{q}` 是那个
词）：`http(s)://` 的模板交给浏览器，其他形状（如 `orpheus://search/{q}`）作为**一个
argv** 传给那个 exe。没配模板时照旧打开应用，并且**明说搜不了** —— 一个假装搜了的回答比
一句「打开了，搜不了」糟得多。

## 为什么不用 `start` / ShellExecute

``cmd /c start`` 会让参数经过 cmd.exe 的解析（`&`、`%VAR%`、换行都在那里有意义），而这条
路的输入来自语音转写 —— 那是最不该交给一个二次解析器的输入源。``CreateProcess`` 直接拿
argv 列表，没有第二个解析器。

## 名字怎么匹配

去掉空白后大小写不敏感地精确匹配，再试一次「去掉常见后缀」（「网易云音乐」→「网易云」）。
不做模糊匹配：模糊匹配会让「打开音乐」在装了三个音乐播放器的机器上变成一次抽奖。
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Mapping

from core.tools.app_index import AppIndex


def _on_windows() -> bool:
    return sys.platform.startswith("win")

from .browser import url_problem
from .contract import ToolRequest, ToolResult
from .policy import load_tools_config, refuse, scrubbed_env

#: 匹配时会被剥掉的尾巴。「网易云音乐」和「网易云」应该指同一个东西。
_TAILS = ("音乐", "播放器", "客户端", "桌面版", "app", "App")


def _normalise(name: str) -> str:
    return "".join(str(name).split()).casefold()


def _candidates(name: str) -> list[str]:
    """一个说出来的名字 -> 几种可能的键。顺序即优先级。"""
    base = _normalise(name)
    out = [base]
    for tail in _TAILS:
        folded = tail.casefold()
        if base.endswith(folded) and len(base) > len(folded):
            out.append(base[: -len(folded)])
    return out


def _expand(raw: Any) -> dict[str, str]:
    """名字表 -> 归一化后的键，带后缀变体。精确匹配永远赢（``setdefault``）。"""
    table = dict(raw or {})
    out: dict[str, str] = {}
    for key, value in table.items():
        out[_normalise(key)] = str(value)
    for key, value in table.items():
        for variant in _candidates(key)[1:]:
            out.setdefault(variant, str(value))
    return out


class AppOpenTool:
    """按名字启动一个白名单里的应用。"""

    name = "app.open"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        spawn: Any = None,
        opener: Any = None,
        index: Any = None,
        launch: Any = None,
    ) -> None:
        self.config = dict(config) if config is not None else load_tools_config()
        self.settings = dict(self.config.get("apps", {}))
        #: 名字 -> 可执行文件。键在读进来的时候就展开成变体：配了「网易云音乐」的人说
        #: 「打开网易云」也该开得起来。完整名字先进表，变体不覆盖已有的 —— 精确匹配永远赢，
        #: 所以「酷狗音乐」和「酷狗」都指对，而两个应用剥完后缀撞名时先配的那个不会被顶掉。
        raw = dict(self.settings.get("entries", {}) or {})
        self.entries: dict[str, Path] = {}
        for key, value in raw.items():
            self.entries[_normalise(key)] = Path(str(value))
        for key, value in raw.items():
            for variant in _candidates(key)[1:]:
                self.entries.setdefault(variant, Path(str(value)))
        #: 名字 -> 网页。同款变体展开，同一条白名单立场。
        self.sites: dict[str, str] = _expand(self.settings.get("sites", {}))
        #: 应用名 -> 「带着一个搜索词打开」的模板。
        self.play: dict[str, str] = _expand(self.settings.get("play", {}))
        #: 注入的启动函数，测试用。生产上是 ``subprocess.Popen``。
        self.spawn = spawn or self._spawn
        #: 注入的开网页函数，测试用。生产上是 ``webbrowser.open``。
        self.opener = opener or webbrowser.open
        #: 已装应用的索引（开始菜单 + 注册表 App Paths）。``None`` = 不发现，只认白名单。
        #:
        #: 这一层让「装了什么就能开什么」成立 —— 使用者点名不想每次先往白名单里加一行。
        #: 候选集永远来自**枚举**，参数只用来在候选里挑，所以它不是「把话当路径执行」。
        self.index = index if index is not None else (AppIndex() if _on_windows() else None)
        #: 启动一个发现出来的项。快捷方式要交给系统去跟（``os.startfile``），
        #: 而不是当成可执行文件去 spawn —— `.lnk` 不是程序。
        self.launch = launch or self._launch

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {
                "name": "str, 白名单里的应用名或网站名",
                "query": "str, 要搜/要放的东西（需要 apps.play 配模板）",
            },
            "enabled": self.enabled,
            # 报名字和「在不在」，不报完整路径：路径会进事件流和日志，而它暴露磁盘布局。
            "apps": sorted(
                {key: path.is_file() for key, path in self.entries.items()}.items()
            ),
            "sites": sorted(self.sites),
            "play": sorted(self.play),
        }

    def available_names(self) -> list[str]:
        """白名单里**文件真的存在**的那些。装了才算可用。"""
        return sorted(key for key, path in self.entries.items() if path.is_file())

    def run(self, request: ToolRequest) -> ToolResult:
        if not self.enabled:
            return refuse(self.name, "app.open 在 config/tools.toml 里是关的")
        wanted = str(request.arguments.get("name", "")).strip()
        query = str(request.arguments.get("query", "")).strip()
        if not wanted:
            # 不给名字 = 「放点音乐」「我想听薛之谦的歌」这类：开哪个由 apps.default_music
            # 定，它没配就报错而不是在白名单里挑一个 —— 装了三个播放器的机器上「挑一个」
            # 是抽奖。
            wanted = str(self.settings.get("default_music", "") or "").strip()
            if not wanted:
                return refuse(
                    self.name,
                    "没说开哪个，而 config/tools.toml 里也没配 apps.default_music",
                )
        keys = _candidates(wanted)
        target: Path | None = None
        matched = ""
        for key in keys:
            if key in self.entries:
                target, matched = self.entries[key], key
                break
        if target is None:
            # 本机没有这个应用，但可能配了它的网页版。「打开抖音」走的正是这一条。
            for key in keys:
                if key in self.sites:
                    return self._open_site(wanted, key, self.sites[key], query)
            # 白名单和网站表都没有 —— **去问这台机器装了什么**。
            #
            # 使用者的要求：「我让他打开网易云就打开，而不是每次都需要添加名单才能打开。」
            # 候选集来自开始菜单与注册表 App Paths 的**枚举**，不是拿他说的字符串当路径 ——
            # 后者等于把「说一句话」变成任意代码执行。发现出来的是他自己装的应用，
            # 开它和他在开始菜单里点一下是同一件事。
            discovered = self._discover(wanted)
            if discovered is not None:
                return discovered
            known = "、".join(self.available_names()) or "（白名单是空的）"
            sites = "、".join(sorted(self.sites)) or "（没有配网页）"
            return refuse(
                self.name,
                f"「{wanted}」不在可启动的应用里，这台机器的开始菜单里也没找到。"
                f"现在能开的应用是：{known}；能开的网页是：{sites}",
                requested=wanted,
            )
        if not target.is_file():
            return refuse(
                self.name,
                f"「{wanted}」配了但文件不在：{target.name} —— 装的位置可能变了",
                requested=wanted,
            )
        template = next((self.play[key] for key in keys if key in self.play), "")
        if query and template:
            return self._open_with_query(wanted, matched, target, template, query)
        try:
            self.spawn(target)
        except OSError as exc:
            return refuse(self.name, f"启动失败：{type(exc).__name__}: {exc}", requested=wanted)
        if query:
            # **不假装搜过了。** 一个说「已经放上薛之谦的歌」而其实只打开了播放器的回答，
            # 会让人以为是播放器的问题；说清楚缺什么，那句话才可行动。
            return ToolResult(
                tool=self.name,
                ok=True,
                output=f"已经打开{wanted}，但没配搜索模板，所以没能直接放「{query}」",
                audit={"decision": "executed", "app": matched, "exe": target.name, "query": True},
            )
        return ToolResult(
            tool=self.name,
            ok=True,
            output=f"已经打开{wanted}",
            audit={"decision": "executed", "app": matched, "exe": target.name},
        )

    def _discover(self, wanted: str) -> ToolResult | None:
        """去开始菜单和注册表里找这个应用。找不到返回 ``None``（让调用方去报那句长错误）。

        `apps.discover` 关掉时直接返回 ``None`` —— 那时行为回到「只认白名单」，
        和这个功能不存在时一模一样。
        """
        if not bool(self.settings.get("discover", True)):
            return None
        if self.index is None:
            return None
        try:
            matches, score = self.index.find(wanted)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不该让这条路变成一个崩溃
            return refuse(self.name, f"扫描已装应用时出错：{type(exc).__name__}: {exc}", requested=wanted)
        if not matches:
            return None
        if len(matches) > 1:
            # **歧义不猜。** 开错一个应用比问一句更糟：前者会让人以为它听错了。
            names = "、".join(item.label for item in matches[:4])
            return refuse(
                self.name,
                f"「{wanted}」对上了好几个，说得再具体一点：{names}",
                requested=wanted,
            )
        item = matches[0]
        if not item.exists:
            return refuse(
                self.name,
                f"开始菜单里有「{item.label}」但它指向的东西不在了 —— 大概是卸载后残留的快捷方式",
                requested=wanted,
            )
        try:
            self.launch(item.target)
        except OSError as exc:
            return refuse(self.name, f"启动失败：{type(exc).__name__}: {exc}", requested=wanted)
        return ToolResult(
            tool=self.name,
            ok=True,
            output=f"已经打开{item.label}",
            audit={
                "decision": "executed",
                "app": item.label,
                "via": item.source,
                "score": score,
            },
        )

    def _open_site(self, wanted: str, matched: str, url: str, query: str) -> ToolResult:
        """本机没装，但配了网页版。"""
        target = url.replace("{q}", urllib.parse.quote_plus(query)) if query else url
        if "{q}" in url and not query:
            return refuse(
                self.name,
                f"「{wanted}」的网页配的是一个搜索地址，但这句话里没有要搜的词",
                requested=wanted,
            )
        problem = url_problem(target)
        if problem is not None:
            return refuse(self.name, f"「{wanted}」配的网页不能打开：{problem}", requested=wanted)
        try:
            opened = self.opener(target)
        except Exception as exc:  # noqa: BLE001 - webbrowser 的异常形状不固定
            return refuse(self.name, f"打不开：{type(exc).__name__}: {exc}", requested=wanted)
        if opened is False:
            return refuse(self.name, "系统没有可用的默认浏览器", requested=wanted)
        spoken = f"已经打开{wanted}网页版" + (f"，搜的是「{query}」" if query else "")
        return ToolResult(
            tool=self.name,
            ok=True,
            output=spoken,
            # 记主机名不记完整地址：搜索词可能是私事，和 web.open 同一条规矩。
            audit={
                "decision": "executed",
                "site": matched,
                "host": urllib.parse.urlsplit(target).netloc,
            },
        )

    def _open_with_query(
        self, wanted: str, matched: str, target: Path, template: str, query: str
    ) -> ToolResult:
        """带着一个搜索词打开。模板是网址就交给浏览器，否则作为一个 argv 交给那个 exe。"""
        filled = template.replace("{q}", urllib.parse.quote_plus(query))
        if filled.casefold().startswith(("http://", "https://")):
            problem = url_problem(filled)
            if problem is not None:
                return refuse(
                    self.name, f"「{wanted}」的播放模板不能打开：{problem}", requested=wanted
                )
            try:
                opened = self.opener(filled)
            except Exception as exc:  # noqa: BLE001
                return refuse(self.name, f"打不开：{type(exc).__name__}: {exc}", requested=wanted)
            if opened is False:
                return refuse(self.name, "系统没有可用的默认浏览器", requested=wanted)
            return ToolResult(
                tool=self.name,
                ok=True,
                output=f"已经放上「{query}」（{wanted}网页版）",
                audit={
                    "decision": "executed",
                    "app": matched,
                    "host": urllib.parse.urlsplit(filled).netloc,
                },
            )
        # 非网址模板：作为**一个** argv 传给 exe。一个 —— 不是拼成一行再切开，那等于
        # 把语音转写交给第二个解析器（见模块头的 `start` 那一节）。
        try:
            self.spawn(target, filled)
        except OSError as exc:
            return refuse(self.name, f"启动失败：{type(exc).__name__}: {exc}", requested=wanted)
        return ToolResult(
            tool=self.name,
            ok=True,
            output=f"已经打开{wanted}并放上「{query}」",
            audit={"decision": "executed", "app": matched, "exe": target.name, "query": True},
        )

    @staticmethod
    def _launch(target: str) -> None:
        """启动一个**发现出来**的项。

        和 ``_spawn`` 分开是因为发现出来的多半是 ``.lnk``，而 `.lnk` 不是程序 ——
        `Popen(["x.lnk"])` 会报 `%1 is not a valid Win32 application`。``os.startfile``
        让 Windows 自己去跟这个快捷方式，目标路径、工作目录、参数、图标全由系统处理，
        和 Explorer 双击一样。

        非 Windows 上退回 ``Popen`` —— 那里没有 `.lnk`。
        """
        starter = getattr(os, "startfile", None)
        if callable(starter):
            starter(target)  # noqa: S606 - 来自开始菜单枚举的路径，不是话里的字符串
            return
        subprocess.Popen(  # noqa: S603 - 同上
            [target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    @staticmethod
    def _spawn(target: Path, *args: str) -> None:
        """启动它，然后**不管它**。

        不等它退出：一个 GUI 应用会活到用户关它为止，等它等于挂住这一轮。``DETACHED_PROCESS``
        让它不随 Vox 一起死 —— 用户让它开的音乐不该在 Vox 重启时停掉。
        """
        creation = 0
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        no_window = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creation = detached | no_window
        subprocess.Popen(  # noqa: S603 - 白名单里的绝对路径，argv 列表，绝不 shell=True
            [str(target), *args],
            cwd=str(target.parent),
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
            shell=False,
        )


__all__ = ["AppOpenTool"]
