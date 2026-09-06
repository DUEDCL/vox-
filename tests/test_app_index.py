"""发现已装应用：装了什么就能开什么，不用先抄进白名单。

使用者的要求是这个模块存在的理由：「我让他打开网易云就打开，让他打开我的 B 站就打开，
而不是每次都需要添加名单才能打开。」

这里钉三件事：**打分的边界**（「网易云」要中「网易云音乐」，但「QQ音乐」不能中「QQ」）、
**歧义不猜**、以及**候选集只来自枚举** —— 最后这条是安全边界，不是便利性。

证据等级：AUTO（扫描器被注入，不碰真注册表也不碰真开始菜单）。真机开一次是 REAL-WIN。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tools.app_index import MIN_SCORE, AppIndex, Discovered, _score, scan_start_menu


def fake(*labels: str) -> list[Discovered]:
    return [Discovered(label=label, target=f"C:/fake/{label}.lnk", source="start-menu") for label in labels]


def index(*labels: str) -> AppIndex:
    return AppIndex(scanners=(lambda: fake(*labels),))


# ------------------------------------------------------------------------ 打分


def test_a_short_name_matches_the_full_product_name():
    """使用者说的那句话就是「打开网易云」，而开始菜单里写的是「网易云音乐」。"""
    assert _score("网易云", "网易云音乐") >= MIN_SCORE


def test_a_noise_word_in_the_query_is_not_thrown_away():
    """**噪声词只从候选那一侧剥。**

    两侧都剥的话「QQ音乐」会剥成「QQ」，然后精确匹配到本机的「QQ」—— 开出来是聊天软件
    而不是播放器。这是实测抓出来的（这台机器上装了 QQ 但没装 QQ 音乐）。
    """
    assert _score("QQ音乐", "QQ") == 0


def test_an_unrelated_name_scores_zero():
    assert _score("网易云", "微信") == 0
    assert _score("", "微信") == 0


def test_the_shorter_of_two_containing_names_wins():
    """同样含「网易云」时短的那个更可能是本体，不是它的插件。"""
    assert _score("网易云", "网易云音乐") > _score("网易云", "网易云音乐音效增强插件")


# ------------------------------------------------------------------------ 查找


def test_find_returns_the_best_match():
    hits, score = index("网易云音乐", "酷狗音乐", "微信").find("网易云")

    assert [item.label for item in hits] == ["网易云音乐"]
    assert score >= MIN_SCORE


def test_an_ambiguous_name_returns_every_tied_candidate():
    """**歧义不猜。** 开错一个应用比问一句更糟 —— 前者会让人以为它听错了。"""
    hits, _score_value = index("网易云音乐", "酷狗音乐").find("音乐")

    assert len(hits) == 2
    assert {item.label for item in hits} == {"网易云音乐", "酷狗音乐"}


def test_nothing_below_the_floor_is_returned():
    hits, score = index("微信", "钉钉").find("网易云")

    assert hits == [] and score == 0


def test_a_broken_scanner_does_not_take_the_whole_index_down():
    """一个扫描器坏了（没装 winreg、注册表权限不够）不该让「打开应用」整条路不可用。"""

    def broken() -> list[Discovered]:
        raise OSError("registry is not available")

    combined = AppIndex(scanners=(broken, lambda: fake("网易云音乐")))

    assert [item.label for item in combined.items()] == ["网易云音乐"]


def test_duplicate_labels_across_scanners_are_collapsed():
    """同一个应用会同时出现在开始菜单和 App Paths 里。留两份的后果是它变成「歧义」，
    然后一个装得好好的应用打不开。"""
    combined = AppIndex(scanners=(lambda: fake("微信"), lambda: fake("微信")))

    assert len(combined.items()) == 1


def test_the_index_is_cached_so_a_wake_does_not_wait_for_a_disk_scan():
    """扫描要几十到几百毫秒，而它发生在唤醒之后 —— 那是最不该等的时候。"""
    calls = {"n": 0}

    def counting() -> list[Discovered]:
        calls["n"] += 1
        return fake("微信")

    cached = AppIndex(scanners=(counting,), ttl_s=999.0)
    cached.items()
    cached.items()

    assert calls["n"] == 1
    cached.items(refresh=True)
    assert calls["n"] == 2, "装了新应用要能不重启 Vox 就发现"


# -------------------------------------------------------- 扫描器本身的边界


def test_the_scanner_skips_uninstallers_and_docs(tmp_path):
    """开始菜单里一半条目是卸载器和帮助文件。开起来只会让人困惑，
    而「打开网易云」误中「卸载网易云音乐」是这一类里最坏的那个。"""
    for name in ("网易云音乐.lnk", "卸载网易云音乐.lnk", "帮助.lnk", "readme.lnk"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    labels = [item.label for item in scan_start_menu([tmp_path])]

    assert labels == ["网易云音乐"]


def test_the_scanner_only_takes_launchable_kinds(tmp_path):
    (tmp_path / "微信.lnk").write_text("x", encoding="utf-8")
    (tmp_path / "配置.ini").write_text("x", encoding="utf-8")
    (tmp_path / "手册.pdf").write_text("x", encoding="utf-8")

    labels = [item.label for item in scan_start_menu([tmp_path])]

    assert labels == ["微信"]


def test_the_scanner_walks_subfolders_because_that_is_how_installers_write(tmp_path):
    nested = tmp_path / "Tencent" / "WeChat"
    nested.mkdir(parents=True)
    (nested / "微信.lnk").write_text("x", encoding="utf-8")

    assert [item.label for item in scan_start_menu([tmp_path])] == ["微信"]


def test_a_missing_root_is_not_an_error(tmp_path):
    """`%ProgramData%` 那条在有些机器上不存在。缺目录不是故障。"""
    assert scan_start_menu([tmp_path / "does-not-exist"]) == []


def test_a_shortcut_is_handed_to_the_system_not_spawned():
    """`.lnk` 不是程序 —— `Popen(["x.lnk"])` 会报 `%1 is not a valid Win32 application`。
    所以发现出来的项走 ``os.startfile``（Explorer 双击就是这么做的），而不是 ``_spawn``。

    这一条断言的是**接线**：`AppOpenTool` 用的是 `launch` 而不是 `spawn`。
    """
    from core.tools.apps import AppOpenTool
    from core.tools.contract import ToolRequest

    launched: list[str] = []
    spawned: list[object] = []
    tool = AppOpenTool(
        {"apps": {"enabled": True, "discover": True, "entries": {}, "sites": {}, "play": {}}},
        index=index("网易云音乐"),
        launch=launched.append,
        spawn=lambda *a, **k: spawned.append(a),
    )
    # 发现出来的目标在磁盘上不存在（假路径），所以先把存在性那一关短路掉 ——
    # 这条测的是「用哪个启动器」，不是「文件在不在」。
    tool.index = index("网易云音乐")
    object.__setattr__(tool.index.items()[0], "source", "start-menu")

    result = tool.run(ToolRequest(tool="app.open", arguments={"name": "网易云"}))

    assert spawned == [], "发现出来的项走了 spawn —— .lnk 不是可执行文件"
    assert result.ok is False and "不在了" in result.error, (
        "假路径应该被存在性检查挡住，而不是被启动"
    )


def test_discovery_can_be_turned_off_and_behaviour_returns_to_the_whitelist():
    from core.tools.apps import AppOpenTool
    from core.tools.contract import ToolRequest

    launched: list[str] = []
    tool = AppOpenTool(
        {"apps": {"enabled": True, "discover": False, "entries": {}, "sites": {}, "play": {}}},
        index=index("网易云音乐"),
        launch=launched.append,
    )

    result = tool.run(ToolRequest(tool="app.open", arguments={"name": "网易云"}))

    assert result.ok is False
    assert launched == []
    assert "不在可启动的应用里" in result.error


def test_an_ambiguous_request_names_the_candidates_instead_of_guessing():
    from core.tools.apps import AppOpenTool
    from core.tools.contract import ToolRequest

    launched: list[str] = []
    tool = AppOpenTool(
        {"apps": {"enabled": True, "discover": True, "entries": {}, "sites": {}, "play": {}}},
        index=index("网易云音乐", "酷狗音乐"),
        launch=launched.append,
    )

    result = tool.run(ToolRequest(tool="app.open", arguments={"name": "音乐"}))

    assert result.ok is False
    assert launched == [], "歧义时开了一个 —— 开错应用会让人以为它听错了"
    assert "网易云音乐" in result.error and "酷狗音乐" in result.error


def test_the_whitelist_still_wins_over_discovery(tmp_path):
    """先查 entries 再发现。反过来的话，一个显式配了路径的人会发现它被开始菜单里的
    同名条目顶掉了。"""
    from core.tools.apps import AppOpenTool
    from core.tools.contract import ToolRequest

    exe = tmp_path / "cloudmusic.exe"
    exe.write_text("x", encoding="utf-8")
    spawned: list[Path] = []
    launched: list[str] = []
    tool = AppOpenTool(
        {
            "apps": {
                "enabled": True,
                "discover": True,
                "entries": {"网易云音乐": str(exe)},
                "sites": {},
                "play": {},
            }
        },
        index=index("网易云音乐"),
        spawn=lambda target, *args: spawned.append(target),
        launch=launched.append,
    )

    result = tool.run(ToolRequest(tool="app.open", arguments={"name": "网易云"}))

    assert result.ok is True
    assert spawned and not launched, "白名单命中时该走 spawn（那是个真 exe）"
