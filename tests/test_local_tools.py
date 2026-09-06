"""「简单的事平台自己做」那三个工具：查时间、开应用、开网页。

产品逻辑是这样分的：查时间、放音乐、打开一个页面这类请求派给 agent 要几秒和一次出网，
而答案就在本机；写代码、做项目这类才值得一次派发。这三个工具是前一半的全部实现。

每一个的风险边界都不同，所以测的重点也不同：
- ``time.now`` 没有参数、不碰任何外部东西 —— 测的是**输出能不能念**
- ``app.open`` 能启动进程 —— 测的是**白名单挡不挡得住**
- ``web.open`` 能打开任意地址 —— 测的是**协议和凭据的限制**

证据等级：AUTO（启动函数和浏览器都是注入的假的）。真的听到时间、真的看到网易云弹出来
是 REAL。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.tools.apps import AppOpenTool
from core.tools.browser import DEFAULT_SEARCH, WebOpenTool, url_problem
from core.tools.clock import TimeNowTool
from core.tools.contract import ToolRequest


def ask(tool: str, **arguments) -> ToolRequest:
    return ToolRequest(tool=tool, arguments=arguments, origin="voice", speaker="du")


# -- time.now -----------------------------------------------------------------


def test_the_time_is_spoken_as_chinese_with_a_period_of_day():
    """输出要能直接念。「下午 3 点」而不是「15:00」—— 后者念出来是「十五比零零」。"""
    tool = TimeNowTool(clock=lambda: datetime(2026, 8, 29, 15, 7))

    result = tool.run(ask("time.now"))

    assert result.ok is True
    assert result.output == "现在是 2026 年 8 月 29 日 星期六，下午 3 点 7 分"
    assert result.audit["iso"] == "2026-08-29T15:07:00"


@pytest.mark.parametrize(
    "hour, period, spoken_hour",
    [(0, "凌晨", 12), (3, "凌晨", 3), (7, "早上", 7), (10, "上午", 10),
     (12, "中午", 12), (15, "下午", 3), (20, "晚上", 8), (23, "深夜", 11)],
)
def test_the_period_of_day_matches_how_people_say_it(hour, period, spoken_hour):
    """0 点说「凌晨 12 点」不说「0 点」，13 点说「下午 1 点」不说「13 点」。

    时段的边界按中文习惯而不是均分：凌晨到 5 点、深夜从 23 点起，中间那几档窄一些。
    """
    tool = TimeNowTool(clock=lambda: datetime(2026, 8, 29, hour, 0))

    output = tool.run(ask("time.now")).output

    assert period in output
    assert f"{spoken_hour} 点" in output


def test_the_weekday_is_always_chinese_regardless_of_locale():
    """``%A`` 给的是 locale 的名字，而 Windows 上那取决于代码页和启动方式 —— 同一台机器上
    「星期五」和「Friday」都可能出现。这句话要被念出来，所以它必须是确定的。"""
    for day, name in enumerate(("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")):
        tool = TimeNowTool(clock=lambda d=day: datetime(2026, 8, 24 + d, 12, 0))
        assert name in tool.run(ask("time.now")).output


def test_arguments_are_ignored_rather_than_refused():
    """时区是本机的属性，不是这句话的属性。多给的参数不该让它失败。"""
    tool = TimeNowTool(clock=lambda: datetime(2026, 8, 29, 9, 0))

    assert tool.run(ask("time.now", timezone="UTC", nonsense=1)).ok is True


# -- app.open -----------------------------------------------------------------


@pytest.fixture
def apps(tmp_path):
    """两个「装好了」的应用和一个配了但文件不在的。"""
    music = tmp_path / "CloudMusic" / "cloudmusic.exe"
    music.parent.mkdir()
    music.write_bytes(b"MZ")
    other = tmp_path / "KuGou.exe"
    other.write_bytes(b"MZ")
    return {
        "apps": {
            "enabled": True,
            "default_music": "网易云音乐",
            "entries": {
                "网易云音乐": str(music),
                "酷狗音乐": str(other),
                "已卸载的东西": str(tmp_path / "gone.exe"),
            },
        }
    }


def tool_with(config, launched: list):
    return AppOpenTool(config, spawn=lambda path: launched.append(str(path)))


def test_a_whitelisted_app_is_launched(apps):
    launched: list[str] = []

    result = tool_with(apps, launched).run(ask("app.open", name="网易云音乐"))

    assert result.ok is True
    assert result.output == "已经打开网易云音乐"
    assert launched and launched[0].endswith("cloudmusic.exe")


def test_a_shortened_name_reaches_the_same_app(apps):
    """配了「网易云音乐」的人说「打开网易云」也该开得起来 —— 剥掉「音乐」这类尾巴。"""
    launched: list[str] = []

    assert tool_with(apps, launched).run(ask("app.open", name="网易云")).ok is True
    assert tool_with(apps, launched).run(ask("app.open", name="酷狗")).ok is True
    assert len(launched) == 2


def test_a_name_outside_the_whitelist_is_refused_and_says_what_is_available(apps):
    """**这是这个工具的核心断言。** 能启动任意可执行文件等于代码执行，而输入来自语音转写
    ——「打开记事本」和「打开记账本」在一个 14M 的识别器上是同一个音。"""
    launched: list[str] = []

    result = tool_with(apps, launched).run(ask("app.open", name="记事本"))

    assert result.ok is False
    assert "不在可启动的应用里" in (result.error or "")
    # 失败要有信息：告诉他能开什么，而不只是「不行」。
    assert "网易云音乐" in (result.error or "")
    assert launched == []


def test_a_path_is_never_accepted_as_a_name(apps):
    """接受路径等于放弃白名单：任何可执行文件都能被写成一个路径。"""
    launched: list[str] = []
    for candidate in ("C:/Windows/System32/cmd.exe", "../../evil.exe", "cmd.exe"):
        result = tool_with(apps, launched).run(ask("app.open", name=candidate))
        assert result.ok is False
    assert launched == []


def test_a_configured_app_whose_file_is_gone_says_so(apps):
    """「配了但文件不在」和「不在白名单里」是两件事：前者是装的位置变了，后者是没配。"""
    launched: list[str] = []

    result = tool_with(apps, launched).run(ask("app.open", name="已卸载的东西"))

    assert result.ok is False
    assert "文件不在" in (result.error or "")
    assert launched == []


def test_no_name_falls_back_to_the_default_music_app(apps):
    """「放点音乐」不带名字。开哪个由 ``apps.default_music`` 定。"""
    launched: list[str] = []

    result = tool_with(apps, launched).run(ask("app.open"))

    assert result.ok is True
    assert launched and launched[0].endswith("cloudmusic.exe")


def test_no_name_and_no_default_is_refused_rather_than_guessed(apps):
    """装了三个播放器的机器上「挑一个」是抽奖，而抽错的那次用户还得自己去关。"""
    apps["apps"]["default_music"] = ""
    launched: list[str] = []

    result = tool_with(apps, launched).run(ask("app.open"))

    assert result.ok is False
    assert "default_music" in (result.error or "")
    assert launched == []


def test_the_tool_can_be_turned_off(apps):
    apps["apps"]["enabled"] = False
    launched: list[str] = []

    assert tool_with(apps, launched).run(ask("app.open", name="网易云音乐")).ok is False
    assert launched == []


def test_available_names_only_counts_apps_whose_file_exists(apps):
    names = AppOpenTool(apps).available_names()

    assert "网易云音乐" in names
    assert "已卸载的东西" not in names


def test_describe_reports_presence_not_paths(apps):
    """路径会进事件流和日志，而它暴露磁盘布局。"""
    described = AppOpenTool(apps).describe()

    assert "cloudmusic.exe" not in str(described)
    assert described["enabled"] is True


# -- app.open：网页版与「带一个搜索词打开」（2026-09-03） -------------------------


def opening(config):
    """记下起了哪个 exe（带 argv）和开了哪个网页。"""
    launched: list[tuple[str, tuple[str, ...]]] = []
    opened: list[str] = []
    tool = AppOpenTool(
        config,
        spawn=lambda path, *args: launched.append((str(path), args)),
        opener=lambda url: opened.append(url) or True,
    )
    return tool, launched, opened


def test_a_site_alias_opens_the_web_version(apps):
    """「给我打开抖音」在这台机器上要的是网页版（使用者原话：「我习惯使用网页版刷视频」），
    而抖音根本没装客户端。

    **「打开 X」是一句话一个意图** —— 让意图层去分「X 是应用还是网站」等于让它知道这台
    机器装了什么，那正是它不该知道的。所以两张白名单都在工具这一层，先应用后网页。
    """
    config = {"apps": dict(apps["apps"], sites={"抖音": "https://www.douyin.com/"})}
    tool, launched, opened = opening(config)

    result = tool.run(ask("app.open", name="抖音"))

    assert result.ok is True
    assert opened == ["https://www.douyin.com/"]
    assert launched == [], "网页版不该顺手起一个进程"
    assert "网页版" in result.output


def test_a_local_app_wins_over_a_site_of_the_same_name(apps):
    """同名时本机应用赢：先查 entries 再查 sites。装了客户端的人说「打开网易云音乐」
    要的是那个客户端。"""
    config = {"apps": dict(apps["apps"], sites={"网易云音乐": "https://music.163.com/"})}
    tool, launched, opened = opening(config)

    assert tool.run(ask("app.open", name="网易云音乐")).ok is True
    assert launched and launched[0][0].endswith("cloudmusic.exe")
    assert opened == []


def test_a_query_reaches_the_default_player_through_a_web_template(apps):
    """「我想听薛之谦的歌」：名字不给，开哪个由 ``default_music`` 定，词交给模板。

    ``http(s)://`` 的模板交给浏览器 —— **这是唯一验证过能真的放出声的那条路**
    （网页播放器打开搜索页就能点播放）。
    """
    config = {
        "apps": dict(apps["apps"], play={"网易云音乐": "https://music.163.com/#/search/m/?s={q}"})
    }
    tool, launched, opened = opening(config)

    result = tool.run(ask("app.open", query="薛之谦"))

    assert result.ok is True
    assert opened and opened[0].endswith("s=%E8%96%9B%E4%B9%8B%E8%B0%A6"), "查询词必须编码"
    assert launched == []


def test_a_non_url_template_goes_in_as_one_argv(apps):
    """``orpheus://search/{q}`` 这类模板作为**一个** argv 传给 exe。

    一个 —— 不是拼成一行再让 shell 切开。这条路的输入是语音转写，而那是最不该交给第二个
    解析器的输入源（见模块头 `start` 那一节）。
    """
    config = {"apps": dict(apps["apps"], play={"网易云音乐": "orpheus://search/{q}"})}
    tool, launched, opened = opening(config)

    assert tool.run(ask("app.open", query="薛之谦")).ok is True
    assert len(launched) == 1
    path, args = launched[0]
    assert path.endswith("cloudmusic.exe")
    assert args == ("orpheus://search/%E8%96%9B%E4%B9%8B%E8%B0%A6",)
    assert opened == []


def test_a_query_without_a_template_says_it_could_not_search(apps):
    """**不假装搜过了。** 一个说「已经放上薛之谦的歌」而其实只打开了播放器的回答，
    会让人以为是播放器的问题；说清楚缺什么，那句话才可行动。"""
    tool, launched, _opened = opening(apps)

    result = tool.run(ask("app.open", query="薛之谦"))

    assert result.ok is True
    assert launched, "照旧要把播放器打开"
    assert "没配搜索模板" in result.output and "薛之谦" in result.output


def test_a_search_shaped_site_needs_a_word(apps):
    """网页配的是搜索地址（带 `{q}`）而这句话里没有词时，报错而不是打开一个 `{q}` 页面。"""
    config = {"apps": dict(apps["apps"], sites={"油管": "https://www.youtube.com/results?q={q}"})}
    tool, _launched, opened = opening(config)

    assert tool.run(ask("app.open", name="油管")).ok is False
    assert opened == []


def test_a_site_that_is_not_http_is_refused(apps):
    """``file://`` 能打开本机任意文件，``javascript:`` 能在浏览器里执行代码 ——
    配歪的网页表不该变成一条新的执行路径。"""
    config = {"apps": dict(apps["apps"], sites={"坏的": "file:///C:/Windows/win.ini"})}
    tool, _launched, opened = opening(config)

    assert tool.run(ask("app.open", name="坏的")).ok is False
    assert opened == []


def test_the_refusal_lists_sites_as_well_as_apps(apps):
    """有信息的失败：能开的应用**和**能开的网页都要列出来，否则使用者只能一个个试。"""
    config = {"apps": dict(apps["apps"], sites={"抖音": "https://www.douyin.com/"})}
    tool, _launched, _opened = opening(config)

    result = tool.run(ask("app.open", name="记事本"))

    assert result.ok is False
    assert "抖音" in str(result.audit) + str(result.output) + str(result.error or "")


# -- web.open -----------------------------------------------------------------


def browser(config=None):
    opened: list[str] = []
    tool = WebOpenTool(config or {}, opener=lambda url: opened.append(url) or True)
    return tool, opened


def test_a_query_becomes_an_encoded_search_url():
    """不编码的话一个带 ``&`` 的词会变成第二个查询参数，而那个词就丢了。"""
    tool, opened = browser()

    result = tool.run(ask("web.open", query="周杰伦 稻香 & 晴天"))

    assert result.ok is True
    assert opened == [DEFAULT_SEARCH.format(q="%E5%91%A8%E6%9D%B0%E4%BC%A6+%E7%A8%BB%E9%A6%99+%26+%E6%99%B4%E5%A4%A9")]
    assert result.audit["search"] is True


def test_an_http_url_is_opened_as_given():
    tool, opened = browser()

    result = tool.run(ask("web.open", url="https://www.bilibili.com/video/BV1"))

    assert result.ok is True
    assert opened == ["https://www.bilibili.com/video/BV1"]
    assert result.audit["host"] == "www.bilibili.com"


@pytest.mark.parametrize(
    "url, reason",
    [
        ("file:///C:/Windows/win.ini", "http/https"),
        ("javascript:alert(1)", "http/https"),
        ("ftp://example.com/x", "http/https"),
        ("https://user:pass@example.com", "凭据"),
        ("https://", "主机名"),
        ("", "空"),
        ("https://example.com/\nmalicious", "换行"),
    ],
)
def test_addresses_that_are_not_a_web_page_are_refused(url, reason):
    """``file://`` 能读本机任意文件，``javascript:`` 能在页面里执行代码 —— 两个都不是
    「打开一个网页」。带凭据的 URL 与 ``core/session_bridge.py`` 同一条规矩。"""
    tool, opened = browser()

    result = tool.run(ask("web.open", url=url))

    assert result.ok is False
    assert reason in (result.error or "")
    assert opened == []


def test_url_and_query_together_are_refused_rather_than_one_being_dropped():
    """挑一个会让另一个静默丢掉，而调用方以为两个都生效了。"""
    tool, opened = browser()

    result = tool.run(ask("web.open", url="https://a.example", query="b"))

    assert result.ok is False
    assert opened == []


def test_a_missing_default_browser_is_reported():
    """``webbrowser.open`` 返回 False 表示它找不到能用的浏览器 —— 那不是成功。"""
    tool = WebOpenTool({}, opener=lambda url: False)

    result = tool.run(ask("web.open", url="https://example.com"))

    assert result.ok is False
    assert "浏览器" in (result.error or "")


def test_the_audit_records_the_host_but_not_the_query():
    """查询词可能是私事，而审计会进事件流。"""
    tool, _ = browser()

    audit = tool.run(ask("web.open", query="我的体检报告")).audit

    assert "我的体检报告" not in str(audit)
    assert audit["host"]


def test_a_custom_search_template_is_used():
    tool, opened = browser({"web": {"open_search_url": "https://search.bilibili.com/all?keyword={q}"}})

    tool.run(ask("web.open", query="稻香"))

    assert opened == ["https://search.bilibili.com/all?keyword=%E7%A8%BB%E9%A6%99"]


def test_the_tool_can_be_turned_off():
    tool, opened = browser({"web": {"open_enabled": False}})

    assert tool.run(ask("web.open", url="https://example.com")).ok is False
    assert opened == []


def test_url_problem_is_reusable_on_its_own():
    """这个判断被工具和意图层都用到，所以它是个模块级函数而不是一个方法。"""
    assert url_problem("https://example.com") is None
    assert url_problem("javascript:alert(1)") is not None
