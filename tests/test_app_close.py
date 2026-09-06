"""`app.close` —— 「把网易云关掉」。

`app.open` 的另一半。在它之前「关掉网易云」的回答是一句「这个我做不到」，而一个只会开不会
关的助手在使用路径上是半个。

这里的每一条钉的都是**「关错东西」这一类后果**：不杀进程（用 WM_CLOSE，应用自己守住未保存
的数据）· 不碰自己的窗口 · 不碰不可见的窗口 · 歧义不猜 · 「已经请它关闭」不说成「关掉了」。

Evidence level: AUTO（假窗口表 + 假 closer，不碰真窗口）。真机枚举那一次在提交信息里（REAL-WIN）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import open_tools
from core.tools.app_close import WM_CLOSE, AppCloseTool, Window
from core.tools.contract import ToolRequest


def _tool(windows, *, posted: bool = True):
    sent: list[int] = []

    def closer(handle: int) -> bool:
        sent.append(handle)
        return posted

    return AppCloseTool({}, lister=lambda: list(windows), closer=closer), sent


def _run(tool, name):
    return tool.run(ToolRequest(tool="app.close", arguments={"name": name}, origin="voice"))


MUSIC = Window(101, "网易云音乐", "cloudmusic.exe")
EDGE = Window(102, "抖音-记录美好生活 - 个人 - Microsoft Edge", "msedge.exe")
SETTINGS_A = Window(103, "设置", "SystemSettings.exe")
SETTINGS_B = Window(104, "设置", "ApplicationFrameHost.exe")
SELF = Window(105, "Vox 控制台", "python.exe")


def test_a_named_app_gets_a_close_request():
    tool, sent = _tool([MUSIC, EDGE])

    result = _run(tool, "网易云")

    assert result.ok
    assert sent == [101]
    assert result.audit == {"decision": "executed", "exe": "cloudmusic.exe", "title": "网易云音乐"}


def test_the_answer_says_requested_not_done():
    """**「已经请它关闭」不是「关掉了」。** 一个挂着「要保存吗」对话框的应用不会真的关，
    而那两句话在语音里差别很大 —— 说「关掉了」会让人以为东西没了。"""
    tool, _sent = _tool([MUSIC])

    result = _run(tool, "网易云")

    assert "已经让" in result.output
    assert "关掉了" not in result.output


def test_the_close_uses_wm_close_rather_than_killing():
    """`WM_CLOSE` 等于点窗口右上角那个 X。`TerminateProcess` 会让未存的东西静默消失，
    而「关掉」这句话里没有那层意思。"""
    assert WM_CLOSE == 0x0010


def test_our_own_windows_are_never_closed():
    """一个能把自己关掉的工具会让「关掉那个东西」变成一次自杀，而使用者说的是别的应用。"""
    tool, sent = _tool([SELF])

    result = _run(tool, "Vox")

    assert not result.ok
    assert sent == []


def test_two_windows_with_the_same_label_are_one_thing_not_an_ambiguity():
    """实测本机「设置」同时匹配 `SystemSettings.exe` 与 `ApplicationFrameHost.exe`（UWP 的
    宿主），两个窗口的标题都是「设置」。报一句「有几个都像『设置』：设置」既读不通，也让
    使用者**无法用语言区分**它们 —— 那是一个提不出答案的问题。"""
    tool, sent = _tool([SETTINGS_A, SETTINGS_B])

    result = _run(tool, "设置")

    assert result.ok
    assert len(sent) == 1


def test_genuinely_different_candidates_are_refused_not_guessed():
    """关错一个应用的代价比开错一个高，而使用者说清楚只要一句话。

    **两个候选必须真的同分才算歧义** —— `_score` 的长度惩罚本来就是为了打破平局
    （「音乐盒」80 vs「音乐工作室」79，那时选前者是对的，不是猜）。所以这里用两个等长的。
    """
    tool, sent = _tool([Window(107, "音乐盒", "musicbox.exe"), Window(106, "音乐库", "mlib.exe")])

    result = _run(tool, "音乐")

    assert not result.ok
    assert "说清楚是哪个" in (result.error or "")
    assert sent == []


def test_an_app_that_is_not_running_says_what_is():
    """「没有开着的 X」后面必须跟着「现在开着的是这些」—— 一句光说失败的话让人没有下一步。"""
    tool, _sent = _tool([MUSIC])

    result = _run(tool, "记事本")

    assert not result.ok
    assert "cloudmusic" in (result.error or "")
    assert result.audit["reason"] == "not running"


def test_a_window_without_a_title_is_not_a_candidate():
    """没有标题的可见顶层窗口多是壳（任务栏、输入法、隐藏的宿主）。枚举那一层已经挡过，
    匹配层**再挡一次** —— 使用者看不见名字的窗口，他不可能点名它。"""
    tool, sent = _tool([Window(108, "   ", "svchost.exe")])

    result = _run(tool, "svchost")

    assert not result.ok
    assert sent == []


def test_the_process_name_matches_too():
    """使用者可能说「关掉 edge」而窗口标题里只有一个网页的名字。"""
    tool, sent = _tool([EDGE])

    assert _run(tool, "edge").ok
    assert sent == [102]


def test_a_failed_post_is_not_reported_as_success():
    """`PostMessage` 失败通常意味着窗口刚好自己关了。报成功会让下一句「怎么还开着」
    没有解释。"""
    tool, _sent = _tool([MUSIC], posted=False)

    result = _run(tool, "网易云")

    assert not result.ok
    assert result.audit["decision"] == "failed"


def test_an_empty_name_is_refused():
    tool, sent = _tool([MUSIC])

    result = _run(tool, "  ")

    assert not result.ok
    assert sent == []


def test_a_broken_enumerator_fails_readably():
    tool = AppCloseTool({}, lister=lambda: (_ for _ in ()).throw(OSError("拒绝访问")), closer=None)

    result = tool.run(ToolRequest(tool="app.close", arguments={"name": "x"}, origin="voice"))

    assert not result.ok
    assert "列不出窗口" in (result.error or "")


@pytest.mark.skipif(sys.platform != "win32", reason="窗口枚举是 user32 的 ctypes 绑定")
def test_it_ships_registered_next_to_app_open():
    """两个是一对，所以它们共用 `apps.enabled` 那一个开关。"""
    tools = open_tools({}, mcp=False).tools

    assert {"app.open", "app.close"} <= set(tools)
    assert "app.close" not in open_tools({"apps": {"enabled": False}}, mcp=False).tools
