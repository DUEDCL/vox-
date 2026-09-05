"""``weather.now`` —— 「今天天气怎么样」。

## 为什么它值一个内置工具

「几点了」和「天气怎么样」是语音助手被问得最多的两句话，而在这个工具之前后一句的回答**必然
是编的**：LLM 手上没有今天的数据，而一个把气温说错五度的助手比一个说「我查不到」的助手糟得
多 —— 前者不会被发现。`web.search` 抓不来这个：搜索结果页的天气块是 JS 渲出来的，抓回去的
HTML 里没有那几个数字。

## 后端是 Open-Meteo，因为它不要 key

判据是本项目对新依赖那三条的同一套：**零注册、零 key、零 telemetry**。`api.open-meteo.com`
的免费档不需要注册也不需要 API key，所以这个工具**不引入任何新凭据** —— 也就没有「配了 key
但放错变量」这一整类失败（那一类在这个仓库里已经出现过三次）。

代价说清楚：**它是一次对外请求**，去 `open-meteo.com` 的两个固定主机。所以它有自己的开关
（`[weather] enabled`），和 `web.search` 的 `allow_internet`、`web.open` 的 `open_enabled`
同一个形状 —— 一个会出网的能力必须能被单独关掉，而不是藏在别的开关后面。

## 主机是常量，城市名只做 percent-encode

URL 由代码拼，主机写死在 `GEOCODE_URL` / `FORECAST_URL` 里。**没有任何输入能改变请求发到哪
台机器** —— 这是这个文件唯一的安全性质，比任何 URL 校验都强（那四份 `endpoint_problem` 存在
的理由是那些地方的地址来自配置）。

## 地名解析结果缓存在进程里

一次查询本来是两个往返（地名 → 经纬度 → 预报，实测 1.8 s + 1.2 s）。城市不会搬家，所以第一
次之后只剩一个往返。**只在内存里**：一个磁盘缓存要管失效、要管并发、要进 `.gitignore`，
而它省下的是重启后第一次查询的 1.8 秒。

## 没有默认城市时**问**，不猜

出厂 `default_city` 是空的。给它填一个（比如「北京」）会让在成都的人听到北京的天气，而那句
话听起来完全正常 —— 和这个项目反复记的那一类失败同形。空的时候工具报一句可执行的话：
「说『上海天气怎么样』，或者在控制台把默认城市填上」。
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import quote
from urllib.request import Request, urlopen

from .contract import ToolRequest, ToolResult

#: 地名 → 经纬度。`language=zh` 让它回中文名（实测 `Beijing` → `北京`），所以报出来的
#: 城市名和使用者说的那个对得上。
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search?name={name}&count=1&language=zh"

#: 预报。`current` 要当下那几个数，`daily` 要今天/明天的高低温与降水概率。
#: `timezone=auto` 让 daily 那两天按**当地**日界算 —— 按 UTC 算的话「明天」在东八区是错的。
FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=auto&forecast_days=2"
)

_UA = "vox-voice-assistant/1.0"

#: WMO 4677 天气码 → 一句能念的中文。**表是抄不掉的**：数字对使用者没有意义，而
#: 「weather_code 61」和「小雨」在语音里差的是全部。缺的码报「天气码 N」而不是猜一个
#: 相近的 —— 说错天气比说不出天气糟。
WEATHER_CODES: dict[int, str] = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴",
    45: "有雾", 48: "有冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "较强阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}

#: 「明天」这两个字的说法。落到 daily 数组的第 1 天（第 0 天是今天）。
TOMORROW_WORDS = ("明天", "明日", "tomorrow")


class WeatherError(RuntimeError):
    """拿不到数据。调用方把它变成一句 `ok=False` 的话。"""


def describe_code(code: Any) -> str:
    """天气码 → 中文。认不出来就如实说「天气码 N」，不猜一个相近的。"""
    try:
        return WEATHER_CODES[int(code)]
    except (TypeError, ValueError, KeyError):
        return f"天气码 {code}"


def _fetch(url: str, *, timeout: float, opener: Any) -> Any:
    """一个 GET，解成 JSON。``opener`` 是测试避开网络的注入点。"""
    request = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with (opener or urlopen)(request, timeout=timeout) as response:
        body = response.read()
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise WeatherError(f"回来的不是 JSON：{exc}") from exc


def _round(value: Any) -> str:
    """气温念整数。「二十八点三度」比「二十八度」长而且没有更有用。"""
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "?"


class WeatherTool:
    """查一个城市当下与今天/明天的天气。一次对外请求（Open-Meteo，无 key）。"""

    name = "weather.now"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        opener: Any = None,
    ) -> None:
        settings = dict(config or {})
        self.default_city = str(settings.get("default_city", "") or "").strip()
        self.timeout_s = float(settings.get("timeout_s", 8.0) or 8.0)
        #: 注入点，给测试用。默认是真的 HTTP。
        self.opener = opener
        #: 地名 → (中文名, 纬度, 经度)。城市不搬家，所以进程内缓存足够。
        self._places: dict[str, tuple[str, float, float]] = {}

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {
                "city": "哪个城市（不填就用默认城市）",
                "day": "today 或 tomorrow，不填是现在",
            },
            "default_city": self.default_city or "（没设）",
        }

    def run(self, request: ToolRequest) -> ToolResult:
        arguments = request.arguments or {}
        city = str(arguments.get("city", "") or "").strip() or self.default_city
        if not city:
            # **不猜一个默认城市。** 在成都的人听到北京的天气，而那句话听起来完全正常。
            return ToolResult(
                tool=self.name,
                ok=False,
                error="还不知道你在哪个城市 —— 说「上海天气怎么样」就行，"
                      "或者在控制台的技能那一栏把默认城市填上",
                audit={"decision": "refused", "reason": "no city"},
            )
        tomorrow = self._wants_tomorrow(arguments)
        # **在 `_locate` 之前问一次。** 之后再问永远是 `True`（那次查询刚把它放进去），
        # 于是这个字段会报一个恒真的读数 —— 比没有这个字段更糟。
        cached = city.casefold() in self._places
        try:
            label, lat, lon = self._locate(city)
            data = _fetch(
                FORECAST_URL.format(lat=lat, lon=lon),
                timeout=self.timeout_s,
                opener=self.opener,
            )
        except WeatherError as exc:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=str(exc),
                audit={"decision": "failed", "city": city[:40]},
            )
        except Exception as exc:  # noqa: BLE001 - 网络的失败形状很多，一律说得清
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"查不到「{city}」的天气（{type(exc).__name__}）",
                audit={"decision": "failed", "city": city[:40]},
            )
        try:
            spoken = self._speak(label, data, tomorrow=tomorrow)
        except WeatherError as exc:
            return ToolResult(
                tool=self.name,
                ok=False,
                error=str(exc),
                audit={"decision": "failed", "city": city[:40]},
            )
        return ToolResult(
            tool=self.name,
            ok=True,
            output=spoken,
            audit={
                "decision": "executed",
                # 城市名要留 —— 「它为什么报的是另一个城市」是这个工具最可能的一次投诉，
                # 而没有这一行答不出来。**经纬度不留**：那是另一回事。
                "city": label[:40],
                "day": "tomorrow" if tomorrow else "now",
                "cached_place": cached,
            },
        )

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _wants_tomorrow(arguments: Mapping[str, Any]) -> bool:
        """「明天」既可能在 `day` 里，也可能被模型塞进 `city`（「明天上海」）。

        两处都看一眼。判错的代价是报错一天的天气 —— 所以只认明确的字样，不做推断。
        """
        blob = " ".join(
            str(arguments.get(key, "") or "") for key in ("day", "when", "city")
        ).casefold()
        return any(word in blob for word in TOMORROW_WORDS)

    def _locate(self, city: str) -> tuple[str, float, float]:
        """地名 → (中文名, 纬度, 经度)。命中缓存就不出网。"""
        key = city.casefold()
        cached = self._places.get(key)
        if cached is not None:
            return cached
        data = _fetch(
            GEOCODE_URL.format(name=quote(city, safe="")),
            timeout=self.timeout_s,
            opener=self.opener,
        )
        results = (data or {}).get("results") or []
        if not results:
            raise WeatherError(f"没找到叫「{city}」的地方")
        first = results[0]
        try:
            found = (
                str(first.get("name") or city),
                float(first["latitude"]),
                float(first["longitude"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError(f"「{city}」的位置没解出来") from exc
        self._places[key] = found
        return found

    def _speak(self, label: str, data: Any, *, tomorrow: bool) -> str:
        """一句能直接念的话。**不摊 JSON** —— 这个工具的输出会被原样念出来。"""
        payload = data if isinstance(data, Mapping) else {}
        daily = payload.get("daily") if isinstance(payload.get("daily"), Mapping) else {}
        index = 1 if tomorrow else 0
        high = self._day_value(daily, "temperature_2m_max", index)
        low = self._day_value(daily, "temperature_2m_min", index)
        rain = self._day_value(daily, "precipitation_probability_max", index)
        if tomorrow:
            code = self._day_value(daily, "weather_code", index)
            head = f"{label}明天"
            if code is None:
                # 免费档的 daily 里没有 weather_code（要单独求），所以明天只报温度与降水
                # 概率。**不拿今天的天气冒充明天的** —— 那正是这个工具要消除的那类错误。
                body = self._range(high, low)
            else:
                body = f"{describe_code(code)}，{self._range(high, low)}"
            tail = f"，降水概率 {_round(rain)}%" if rain is not None else ""
            if not body:
                raise WeatherError(f"没拿到{label}明天的预报")
            return f"{head} {body}{tail}"
        current = payload.get("current") if isinstance(payload.get("current"), Mapping) else {}
        if "temperature_2m" not in current:
            raise WeatherError(f"没拿到{label}当下的读数")
        now = f"{label}现在 {_round(current.get('temperature_2m'))} 度"
        code = describe_code(current.get("weather_code"))
        feels = current.get("apparent_temperature")
        parts = [f"{now}，{code}"]
        # 体感只在和气温差 3 度以上时说 —— 差一度的体感不携带信息，而每一个字都要念。
        try:
            if feels is not None and abs(float(feels) - float(current["temperature_2m"])) >= 3:
                parts.append(f"体感 {_round(feels)} 度")
        except (TypeError, ValueError):
            pass
        span = self._range(high, low)
        if span:
            parts.append(f"今天 {span}")
        if rain is not None:
            parts.append(f"降水概率 {_round(rain)}%")
        return "，".join(parts)

    @staticmethod
    def _day_value(daily: Mapping[str, Any], key: str, index: int) -> Any:
        values = daily.get(key)
        if not isinstance(values, (list, tuple)) or index >= len(values):
            return None
        return values[index]

    @staticmethod
    def _range(high: Any, low: Any) -> str:
        if high is None or low is None:
            return ""
        return f"{_round(low)} 到 {_round(high)} 度"


__all__ = [
    "FORECAST_URL",
    "GEOCODE_URL",
    "TOMORROW_WORDS",
    "WEATHER_CODES",
    "WeatherError",
    "WeatherTool",
    "describe_code",
]
