"""`weather.now` —— 「今天天气怎么样」。

**在它之前那句话的回答必然是编的**：LLM 手上没有今天的数据，而一个把气温说错五度的助手比
一个说「我查不到」的助手糟得多 —— 前者不会被发现。所以这一整份测试守的是同一件事：
**要么给真数据，要么明说拿不到**，中间没有第三种。

Evidence level: AUTO（注入的 opener，**一次网络都不打**；真机读数见
`docs/research/prototype-results.md`）。
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dispatch.intent import RuleBasedIntentResolver
from core.tools import open_tools
from core.tools.contract import ToolRequest
from core.tools.weather import (
    FORECAST_URL,
    GEOCODE_URL,
    WEATHER_CODES,
    WeatherTool,
    describe_code,
)

GEOCODED = {"results": [{"name": "上海", "latitude": 31.22, "longitude": 121.46}]}

FORECAST = {
    "current": {
        "temperature_2m": 29.6,
        "apparent_temperature": 33.4,
        "weather_code": 2,
        "wind_speed_10m": 8.1,
    },
    "daily": {
        "temperature_2m_max": [30.2, 28.8],
        "temperature_2m_min": [23.7, 24.1],
        "precipitation_probability_max": [35, 66],
    },
}


class FakeOpener:
    """记下请求了哪些 URL，按顺序回预置的 JSON。一个字节都不出网。"""

    def __init__(self, *bodies, status: int = 200) -> None:
        self.bodies = list(bodies)
        self.urls: list[str] = []
        self.status = status

    def __call__(self, request, timeout=None):  # noqa: ANN001 - urlopen 的形状
        del timeout
        self.urls.append(request.full_url)
        body = self.bodies.pop(0) if self.bodies else {}
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return Response(raw)


def _tool(*bodies, **settings):
    opener = FakeOpener(*bodies)
    return WeatherTool(settings, opener=opener), opener


def _run(tool, **arguments):
    return tool.run(ToolRequest(tool="weather.now", arguments=arguments, origin="voice"))


# -- 一句能念的话 ------------------------------------------------------------


def test_the_answer_is_one_speakable_sentence_not_a_json_dump():
    """这个工具的 `output` 会被**原样念出来**，所以它不能是一份 JSON。"""
    tool, _ = _tool(GEOCODED, FORECAST)

    result = _run(tool, city="上海")

    assert result.ok
    assert result.output == "上海现在 30 度，多云，体感 33 度，今天 24 到 30 度，降水概率 35%"
    assert "{" not in result.output


def test_tomorrow_reads_the_second_day_not_the_first():
    """「明天会不会下雨」答成今天的降水概率是这个工具最容易犯又最难发现的错。"""
    tool, _ = _tool(GEOCODED, FORECAST)

    result = _run(tool, city="上海", day="tomorrow")

    assert result.ok
    assert "明天 24 到 29 度" in result.output
    assert "66%" in result.output, "降水概率取的是第二天那一格"


def test_tomorrow_never_borrows_todays_sky():
    """免费档的 daily 里没有 `weather_code`，所以明天只报温度与降水概率。

    拿**今天**的「多云」当明天的天气说出来，听起来完全正常而且是错的 —— 正是这个工具
    要消除的那一类回答。
    """
    tool, _ = _tool(GEOCODED, FORECAST)

    result = _run(tool, city="上海", day="tomorrow")

    assert "多云" not in result.output


def test_a_body_temperature_within_three_degrees_is_not_mentioned():
    """体感和气温差一度不携带信息，而每一个字都要念出来。"""
    payload = json.loads(json.dumps(FORECAST))
    payload["current"]["apparent_temperature"] = 30.4
    tool, _ = _tool(GEOCODED, payload)

    result = _run(tool, city="上海")

    assert "体感" not in result.output


def test_an_unknown_weather_code_is_reported_not_guessed():
    """认不出来就说「天气码 N」。猜一个相近的天气比说不出天气糟。"""
    assert describe_code(61) == "小雨"
    assert describe_code(3) == "阴"
    assert describe_code(1234) == "天气码 1234"
    assert describe_code(None) == "天气码 None"
    assert 0 in WEATHER_CODES and 95 in WEATHER_CODES


# -- 城市 --------------------------------------------------------------------


def test_no_city_and_no_default_asks_instead_of_guessing():
    """**不预填一个城市。** 在成都的人听到北京的天气，而那句话听起来完全正常 ——
    和这个项目反复记的那一类失败同形（不报错、不可疑、纯粹是错的）。"""
    tool, opener = _tool(GEOCODED, FORECAST)

    result = _run(tool)

    assert not result.ok
    assert "上海天气怎么样" in (result.error or ""), "失败要给下一步"
    assert opener.urls == [], "没有城市连请求都不该发"


def test_the_default_city_is_used_when_none_was_said():
    tool, opener = _tool(GEOCODED, FORECAST, default_city="上海")

    result = _run(tool)

    assert result.ok
    assert len(opener.urls) == 2


def test_a_place_that_does_not_exist_says_so():
    tool, _ = _tool({"results": []})

    result = _run(tool, city="不存在的地方")

    assert not result.ok
    assert "不存在的地方" in (result.error or "")


def test_the_place_lookup_is_cached_so_the_second_ask_is_one_round_trip():
    """一次查询本来是两个往返（实测 1.6 s + 1.6 s）。**城市不会搬家。**"""
    tool, opener = _tool(GEOCODED, FORECAST, FORECAST, default_city="上海")

    first = _run(tool)
    second = _run(tool)

    assert first.ok and second.ok
    assert len(opener.urls) == 3, "第二次只发预报那一个请求"
    assert first.audit["cached_place"] is False
    assert second.audit["cached_place"] is True, "这个字段在 `_locate` 之前问，否则恒真"


def test_the_city_name_is_percent_encoded_into_a_constant_host():
    """**主机是常量。** 没有任何输入能改变请求发到哪台机器 —— 这是这个文件唯一的安全性质。"""
    tool, opener = _tool(GEOCODED, FORECAST)

    _run(tool, city="上海")

    assert opener.urls[0].startswith("https://geocoding-api.open-meteo.com/")
    assert opener.urls[1].startswith("https://api.open-meteo.com/")
    assert "%E4%B8%8A%E6%B5%B7" in opener.urls[0], "城市名 percent-encode，不是原样拼进去"
    assert GEOCODE_URL.startswith("https://") and FORECAST_URL.startswith("https://")


# -- 失败 --------------------------------------------------------------------


def test_a_network_failure_is_a_sentence_not_a_traceback():
    class Broken:
        def __call__(self, request, timeout=None):
            raise OSError("网断了")

    tool = WeatherTool({}, opener=Broken())

    result = _run(tool, city="上海")

    assert not result.ok
    assert "上海" in (result.error or "") and "OSError" in (result.error or "")


def test_a_reply_that_is_not_json_is_a_failure_not_a_made_up_number():
    tool, _ = _tool(b"<html>502 Bad Gateway</html>")

    result = _run(tool, city="上海")

    assert not result.ok


def test_a_forecast_without_the_current_block_is_a_failure():
    """「拿到了但里面没有那个数」和「拿不到」在使用者那侧必须是同一句话。"""
    tool, _ = _tool(GEOCODED, {"daily": FORECAST["daily"]})

    result = _run(tool, city="上海")

    assert not result.ok
    assert "当下的读数" in (result.error or "")


# -- 快路径与注册 -------------------------------------------------------------


@pytest.mark.parametrize(
    "said, city, day",
    [
        ("今天天气怎么样", "", "today"),
        ("天气怎么样", "", "today"),
        ("上海天气怎么样", "上海", "today"),
        ("今天上海天气怎么样", "上海", "today"),
        ("上海明天天气", "上海", "tomorrow"),
        ("明天天气如何", "", "tomorrow"),
        ("北京气温多少度", "北京", "today"),
        ("今天会下雨", "", "today"),
        ("明天下雨", "", "tomorrow"),
        ("广州天气好不好", "广州", "today"),
    ],
)
def test_the_common_ways_of_asking_take_the_fast_path(said, city, day):
    """**这一句最该走快路径。** 派给 agent 要多一次 LLM 往返（实测 1.8–4 秒）换回来的
    只是同一个工具调用。"""
    intent = RuleBasedIntentResolver().resolve(said)

    assert intent.kind == "tool" and intent.tool == "weather.now", said
    assert intent.arguments == {"city": city, "day": day}


@pytest.mark.parametrize(
    "said",
    [
        "天气这个词怎么写",
        "帮我查一下天气预报的历史",
        "我想聊聊天气对农业的影响",
        "天气不错我们出去玩吧",
        "天气预报怎么说",
    ],
)
def test_talking_about_the_weather_is_not_asking_for_it(said):
    """整句锚定是这条规则的全部边界：这几句都在**谈论**天气，不是在问它。"""
    assert RuleBasedIntentResolver().resolve(said).kind == "agent", said


@pytest.mark.parametrize(
    "said",
    ["搜一下 武汉天气", "搜一下武汉天气", "帮我搜一下 武汉天气", "请搜一下 武汉天气"],
)
def test_an_explicit_verb_wins_over_the_weather_rule(said):
    """**显式动词赢。** 说「搜一下武汉天气」的人点名了要搜网页，即便这个平台有一个更准的
    天气工具。

    这条不是洁癖，它修的是一次真实的冲突：城市捕获组会把「搜一下 武汉」整段吃进去，于是
    第一版把这三句全抢成了 `weather.now`（`tests/test_intent.py` 的三条因此变红）。挡法是
    正则开头那个否定前视，**不是**规则顺序 —— 顺序只解决带空格的那一种，粘着写的
    「搜一下武汉天气」会让城市变成「搜一下武汉」，然后报「没找到叫『搜一下武汉』的地方」。
    """
    intent = RuleBasedIntentResolver().resolve(said)

    assert intent.tool == "web.search", said
    assert intent.arguments == {"query": "武汉天气"}


def test_the_config_can_switch_it_off():
    """它**是一次对外请求**，所以它有自己的开关 —— 和 `web.allow_internet` 同一个形状。"""
    off = open_tools({"weather": {"enabled": False}}, mcp=False)
    on = open_tools({"weather": {"enabled": True}}, mcp=False)

    assert "weather.now" not in off.tools
    assert "weather.now" in on.tools


def test_the_policy_gate_lets_it_through():
    """**「注册了」不等于「跑得动」，这是实测踩过的。**

    2026-09-05：`weather.now` 已经 `register()` 了、已经在 `REGISTERED` 里、已经在控制台
    页面上列出来了，而 `policy.check()` 因为 `contract.TOOL_NAMES` 里没有它一律回
    `unknown tool` —— 于是「今天天气怎么样」变成一句 `ok=False`，**而所有单元测试都是绿的**
    （它们验的是「在不在清单里」，不是「过不过那道门」）。

    所以这一条走 `runner.run()`（也就是政策门）而不是直接调工具，断言那句 refusal
    **不是** `unknown tool`：走到「没设默认城市」才说明门放行了。
    """
    from core.tools.contract import TOOL_NAMES

    assert "weather.now" in TOOL_NAMES
    runner = open_tools({"weather": {"enabled": True}}, mcp=False)

    result = runner.run(ToolRequest(tool="weather.now", arguments={}, origin="voice"))

    assert (result.error or "") != "unknown tool"
    assert "城市" in (result.error or ""), "门放行了，拒绝的理由该是工具自己的那一条"


def test_an_agent_can_reach_it_too():
    """`origin="agent"` 也得过门 —— 模型问天气是这个工具最主要的入口之一。"""
    runner = open_tools({"weather": {"enabled": True, "default_city": ""}}, mcp=False)

    result = runner.run(ToolRequest(tool="weather.now", arguments={}, origin="agent"))

    assert (result.error or "") not in {"unknown tool", "unknown origin"}


def test_the_agent_can_call_it_by_name():
    """问到天气时模型必须**先调它**，所以它得在 agent 看得到的白名单里。"""
    from core.agents.skills import REGISTERED, manifest

    assert "weather.now" in REGISTERED
    printed = manifest(("weather.now",))
    assert "weather.now" in printed
    assert "编" in printed, "提示里要写清「自己编一个气温不会被发现」"
