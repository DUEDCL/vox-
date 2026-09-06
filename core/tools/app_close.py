"""``app.close`` —— 「把网易云关掉」。

## 为什么它和 `app.open` 是一对

使用者说得出「打开网易云」就说得出「关掉网易云」，而在这个工具之前后半句的回答是一句
「这个我做不到」。一个只会开不会关的助手在使用路径上是半个。

## 用 WM_CLOSE，不用 TerminateProcess

`WM_CLOSE` 等于点窗口右上角那个 X：应用自己决定要不要提示保存，未存的东西不会静默消失。
`TerminateProcess` 会 —— 而「关掉」这句话里没有「丢掉我没保存的东西」这层意思。代价说清楚：
一个挂着「要保存吗」对话框的应用**不会真的关掉**，而这个工具会如实报告「已经请它关闭」
而不是「关掉了」（那两句话在语音里差别很大）。

所以它不需要确认卡：动作可被应用自己拦下，后果当场可见，和 `app.open` 同一档。

## 只碰可见的顶层窗口

后台服务、托盘常驻、隐藏窗口一律不在候选里。理由和 `app.open` 的白名单同源：**候选集必须
是使用者自己看得见的东西**。一个能关掉不可见进程的工具在语音误识别下的后果不可预期。

## 歧义不猜

两个窗口同分时报候选让他说清楚 —— 和 `app.open` 同一条立场（开错一个应用会让人以为它听错
了，而关错一个的代价更高）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Mapping

from .app_index import MIN_SCORE, _score
from .contract import ToolRequest, ToolResult

#: `WM_CLOSE`。窗口收到它等于使用者点了那个 X。
WM_CLOSE = 0x0010

#: 自己的窗口一律不碰。Vox 的控制台、球、以及跑它的那个终端 —— 一个能把自己关掉的工具
#: 会让「关掉那个东西」变成一次自杀，而使用者说的是别的应用。
SELF_MARKERS = ("vox", "python", "claude", "cmd.exe", "powershell", "windowsterminal")


@dataclass(frozen=True)
class Window:
    """一个可见的顶层窗口。``exe`` 是进程可执行文件名（不含目录）。"""

    handle: int
    title: str
    exe: str

    def label(self) -> str:
        return f"{self.title or self.exe}"


def visible_windows() -> list[Window]:
    """全部**可见的顶层窗口**。非 Windows 上返回空表（这个工具在那里不注册）。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    found: list[Window] = []

    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(handle, _param):  # noqa: ANN001 - ctypes 回调
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        title = ""
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)
            title = buffer.value
        # 没有标题的可见顶层窗口多是壳（任务栏、输入法、隐藏的宿主），不进候选。
        if not title.strip():
            return True
        found.append(Window(int(handle), title, _exe_of(kernel32, user32, handle)))
        return True

    user32.EnumWindows(proto(visit), 0)
    return found


def _exe_of(kernel32: Any, user32: Any, handle: Any) -> str:
    """窗口所属进程的可执行文件名。拿不到就返回空串 —— 那时只按标题匹配。"""
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if not pid.value:
        return ""
    #: PROCESS_QUERY_LIMITED_INFORMATION —— 够拿路径，且对多数进程不需要提权。
    process = kernel32.OpenProcess(0x1000, False, pid.value)
    if not process:
        return ""
    try:
        size = wintypes.DWORD(512)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(process)


def ask_to_close(handle: int) -> bool:
    """给一个窗口发 `WM_CLOSE`。返回消息有没有投递成功（**不是**「关掉了」）。"""
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    return bool(user32.PostMessageW(handle, WM_CLOSE, 0, 0))


class AppCloseTool:
    """按名字关一个应用。**发 WM_CLOSE，不杀进程。**"""

    name = "app.close"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        lister: Any = None,
        closer: Any = None,
    ) -> None:
        self.config = dict(config) if config is not None else {}
        #: 注入点，给测试用。默认是真的窗口枚举。
        self.lister = lister or visible_windows
        self.closer = closer or ask_to_close

    def describe(self) -> Mapping[str, Any]:
        return {"name": self.name, "arguments": {"name": "要关哪个应用"}}

    def run(self, request: ToolRequest) -> ToolResult:
        wanted = str((request.arguments or {}).get("name", "") or "").strip()
        if not wanted:
            return ToolResult(
                tool=self.name,
                ok=False,
                error="要关哪个没有说",
                audit={"decision": "refused", "reason": "empty name"},
            )
        try:
            windows = list(self.lister() or ())
        except Exception as exc:  # noqa: BLE001 - 枚举失败要说得清
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"列不出窗口：{type(exc).__name__}: {exc}",
                audit={"decision": "failed"},
            )
        best, ties = self._match(wanted, windows)
        if best is None:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"没有开着的「{wanted}」。现在开着的：{self._names(windows) or '（没有）'}",
                audit={"decision": "refused", "reason": "not running", "windows": len(windows)},
            )
        if ties:
            # **歧义不猜。** 关错一个应用的代价比开错一个高，而使用者说清楚只要一句话。
            names = "、".join(sorted({window.label()[:24] for window in (best, *ties)}))
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"有几个都像「{wanted}」：{names}。说清楚是哪个",
                audit={"decision": "refused", "reason": "ambiguous", "candidates": len(ties) + 1},
            )
        posted = bool(self.closer(best.handle))
        if not posted:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"「{best.label()[:24]}」没能收到关闭请求（窗口可能刚好关了）",
                audit={"decision": "failed", "exe": best.exe},
            )
        # **「已经请它关闭」不是「关掉了」。** 一个挂着「要保存吗」的应用不会真的关，
        # 而那两句话在语音里差别很大 —— 说「关掉了」会让人以为东西没了。
        return ToolResult(
            tool=self.name,
            ok=True,
            output=f"已经让「{best.label()[:24]}」关闭",
            audit={"decision": "executed", "exe": best.exe, "title": best.title[:60]},
        )

    def _match(self, wanted: str, windows: list[Window]) -> tuple[Window | None, list[Window]]:
        """打分最高的那个，以及和它同分的其余。标题与进程名两路都试，取高的那一路。

        **同一个进程的多个窗口算一个** —— 一个开了三个标签页的浏览器不是三个候选，而按
        进程去重之后「有几个都像」才真的是歧义。
        """
        scored: dict[str, tuple[int, Window]] = {}
        for window in windows:
            # 空标题的窗口**在这里也挡一次**。枚举那一层已经挡过，但一个「不依赖上游过滤」
            # 的匹配器更健壮：使用者看不见名字的窗口，他不可能点名它。
            if not window.title.strip() or self._is_self(window):
                continue
            score = max(_score(wanted, window.title), _score(wanted, _stem(window.exe)))
            if score < MIN_SCORE:
                continue
            key = window.exe.casefold() or window.title.casefold()
            if key not in scored or score > scored[key][0]:
                scored[key] = (score, window)
        if not scored:
            return None, []
        ranked = sorted(scored.values(), key=lambda pair: -pair[0])
        top = ranked[0][0]
        winners = [window for score, window in ranked if score == top]
        # **标签相同的候选不算歧义。** 实测本机「设置」同时匹配 `SystemSettings.exe` 与
        # `ApplicationFrameHost.exe`（UWP 的宿主），两个窗口的标题都是「设置」—— 报一句
        # 「有几个都像『设置』：设置」既读不通，也让使用者无法用语言区分它们。同名就当同一
        # 个东西，关打分最高的那个。
        labels = {window.label().casefold() for window in winners}
        if len(labels) < 2:
            return winners[0], []
        return winners[0], winners[1:]

    @staticmethod
    def _is_self(window: Window) -> bool:
        """Vox 自己的窗口、以及跑它的那个终端。一个能把自己关掉的工具会让「关掉那个东西」
        变成一次自杀，而使用者说的是别的应用。"""
        blob = f"{window.exe} {window.title}".casefold()
        return any(marker in blob for marker in SELF_MARKERS)

    def _names(self, windows: list[Window]) -> str:
        """报「现在开着的」时按进程去重并截断 —— 一个念不完的清单等于没有清单。"""
        seen: list[str] = []
        for window in windows:
            if self._is_self(window):
                continue
            label = _stem(window.exe) or window.title
            if label and label not in seen:
                seen.append(label)
        return "、".join(seen[:6])


def _stem(exe: str) -> str:
    return exe.rsplit(".", 1)[0] if "." in exe else exe


__all__ = ["SELF_MARKERS", "WM_CLOSE", "AppCloseTool", "Window",
           "ask_to_close", "visible_windows"]
