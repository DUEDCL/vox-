"""``app.open`` —— 启动本机应用。白名单，不是「在 PATH 里找」。

## 为什么必须是白名单

能启动任意可执行文件等于代码执行。语音是这条路的入口，而语音会被转写错 —— 「打开记事本」
和「打开记账本」在 14M 的识别器上是同一个音。所以这个工具只认一张**显式的名字 → 可执行
文件**映射表，表里没有的名字一律拒绝，而不是去猜、去搜索、去 PATH 里找一个同名的东西。

同一条理由让它**不接受路径参数**：接受路径就等于放弃白名单，因为任何可执行文件都能被写成
一个路径。

## 为什么不用 `start` / ShellExecute

``cmd /c start`` 会让参数经过 cmd.exe 的解析（`&`、`%VAR%`、换行都在那里有意义），而这条
路的输入来自语音转写 —— 那是最不该交给一个二次解析器的输入源。``CreateProcess`` 直接拿
argv 列表，没有第二个解析器。

## 名字怎么匹配

去掉空白后大小写不敏感地精确匹配，再试一次「去掉常见后缀」（「网易云音乐」→「网易云」）。
不做模糊匹配：模糊匹配会让「打开音乐」在装了三个音乐播放器的机器上变成一次抽奖。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

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


class AppOpenTool:
    """按名字启动一个白名单里的应用。"""

    name = "app.open"

    def __init__(self, config: Mapping[str, Any] | None = None, *, spawn: Any = None) -> None:
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
        #: 注入的启动函数，测试用。生产上是 ``subprocess.Popen``。
        self.spawn = spawn or self._spawn

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {"name": "str, 白名单里的应用名"},
            "enabled": self.enabled,
            # 报名字和「在不在」，不报完整路径：路径会进事件流和日志，而它暴露磁盘布局。
            "apps": sorted(
                {key: path.is_file() for key, path in self.entries.items()}.items()
            ),
        }

    def available_names(self) -> list[str]:
        """白名单里**文件真的存在**的那些。装了才算可用。"""
        return sorted(key for key, path in self.entries.items() if path.is_file())

    def run(self, request: ToolRequest) -> ToolResult:
        if not self.enabled:
            return refuse(self.name, "app.open 在 config/tools.toml 里是关的")
        wanted = str(request.arguments.get("name", "")).strip()
        if not wanted:
            # 不给名字 = 「放点音乐」这类泛指。开 apps.default_music，它没配就报错而不是
            # 在白名单里挑一个 —— 装了三个播放器的机器上「挑一个」是抽奖。
            wanted = str(self.settings.get("default_music", "") or "").strip()
            if not wanted:
                return refuse(
                    self.name,
                    "没说开哪个，而 config/tools.toml 里也没配 apps.default_music",
                )
        target: Path | None = None
        matched = ""
        for key in _candidates(wanted):
            if key in self.entries:
                target, matched = self.entries[key], key
                break
        if target is None:
            known = "、".join(self.available_names()) or "（白名单是空的）"
            return refuse(
                self.name,
                f"「{wanted}」不在可启动的应用里。现在能开的是：{known}",
                requested=wanted,
            )
        if not target.is_file():
            return refuse(
                self.name,
                f"「{wanted}」配了但文件不在：{target.name} —— 装的位置可能变了",
                requested=wanted,
            )
        try:
            self.spawn(target)
        except OSError as exc:
            return refuse(self.name, f"启动失败：{type(exc).__name__}: {exc}", requested=wanted)
        return ToolResult(
            tool=self.name,
            ok=True,
            output=f"已经打开{wanted}",
            audit={"decision": "executed", "app": matched, "exe": target.name},
        )

    @staticmethod
    def _spawn(target: Path) -> None:
        """启动它，然后**不管它**。

        不等它退出：一个 GUI 应用会活到用户关它为止，等它等于挂住这一轮。``DETACHED_PROCESS``
        让它不随 Vox 一起死 —— 用户让它开的音乐不该在 Vox 重启时停掉。
        """
        creation = 0
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        no_window = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creation = detached | no_window
        subprocess.Popen(  # noqa: S603 - 白名单里的绝对路径，argv 列表，绝不 shell=True
            [str(target)],
            cwd=str(target.parent),
            env=scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
            shell=False,
        )


__all__ = ["AppOpenTool"]
