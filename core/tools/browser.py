"""``web.open`` —— 在默认浏览器里打开一个地址，或者打开一次搜索。

## 和 ``web.search`` 的分工

``web.search`` 抓结果**回来**给平台读（要一个后端、要出网、结果进回复）；这个只是把浏览器
**推出去**打开一个页面。「搜一下幂等是什么」是前者，「打开 B 站搜周杰伦」是后者 —— 后者不
需要任何搜索后端，因为渲染结果的是浏览器不是我们。

这条路让「通过搜索打开目标媒体网站播放」成立：本机没装某个播放器时，一个搜索页就是那个
播放器。

## 约束

- 只许 ``http`` / ``https``。``file://`` 能打开本机任意文件，``javascript:`` 能在浏览器里
  执行代码 —— 两个都不是「打开一个网页」。
- URL 里带凭据（``user:pass@``）一律拒绝，与 ``core/session_bridge.py`` 同一条规矩。
- 搜索词进 URL 前必须 percent-encode。不编码的话一个带 ``&`` 的词会变成第二个查询参数，
  而一个带换行的词（语音转写不会有，粘贴会有）会让某些浏览器把它当成第二条命令行。

**故意不做域名白名单**：「打开任意媒体站」是这个工具的用途，白名单会让它只能开预先想到的
那几个。风险边界在「打开一个页面」这个动作本身 —— 它不执行、不下载、不回传。
"""

from __future__ import annotations

import urllib.parse
import webbrowser
from typing import Any, Mapping

from .contract import ToolRequest, ToolResult
from .policy import load_tools_config, refuse

#: 搜索引擎模板。``{q}`` 是编码后的查询词。默认必应：它不需要 JS 就能出结果页，
#: 而这条路的成功判据是浏览器打开了一个能用的页面。
DEFAULT_SEARCH = "https://www.bing.com/search?q={q}"

#: 允许的协议。file 能读本机任意文件，javascript 能在页面里执行代码。
_SCHEMES = frozenset({"http", "https"})


def url_problem(raw: str) -> str | None:
    """这个地址能不能打开，不能的话为什么。``None`` = 可以。"""
    text = str(raw or "").strip()
    if not text:
        return "地址是空的"
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.casefold() not in _SCHEMES:
        return f"只打开 http/https，不打开 {parsed.scheme or '没有协议'} 的地址"
    if not parsed.netloc:
        return "地址里没有主机名"
    if "@" in parsed.netloc:
        return "地址里不许带凭据（user:pass@）"
    if any(char in text for char in ("\n", "\r", "\t")):
        # 语音转写不会产生这些，粘贴会 —— 而它们在某些浏览器的命令行里是分隔符。
        return "地址里有换行或制表符"
    return None


class WebOpenTool:
    """把一个地址或一次搜索交给默认浏览器。"""

    name = "web.open"

    def __init__(self, config: Mapping[str, Any] | None = None, *, opener: Any = None) -> None:
        self.config = dict(config) if config is not None else load_tools_config()
        self.settings = dict(self.config.get("web", {}))
        self.search_template = str(self.settings.get("open_search_url", "") or DEFAULT_SEARCH)
        #: 注入的打开函数，测试用。生产上是 ``webbrowser.open``。
        self.opener = opener or webbrowser.open

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("open_enabled", True))

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {"url": "str, http/https", "query": "str, 改成打开一次搜索"},
            "enabled": self.enabled,
            "search_template": self.search_template,
        }

    def run(self, request: ToolRequest) -> ToolResult:
        if not self.enabled:
            return refuse(self.name, "web.open 在 config/tools.toml 里是关的")
        url = str(request.arguments.get("url", "")).strip()
        query = str(request.arguments.get("query", "")).strip()
        if url and query:
            # 两个都给了说不清该开哪个。报错而不是挑一个：挑一个会让另一个静默丢掉。
            return refuse(self.name, "url 和 query 只能给一个")
        if query:
            url = self.search_template.format(q=urllib.parse.quote_plus(query))
            spoken = f"已经打开搜索：{query}"
        else:
            spoken = "已经打开了那个页面"
        problem = url_problem(url)
        if problem is not None:
            return refuse(self.name, problem)
        try:
            opened = self.opener(url)
        except Exception as exc:  # noqa: BLE001 - webbrowser 的异常形状不固定
            return refuse(self.name, f"打不开：{type(exc).__name__}: {exc}")
        if opened is False:
            # webbrowser.open 返回 False 表示它找不到能用的浏览器。
            return refuse(self.name, "系统没有可用的默认浏览器")
        host = urllib.parse.urlsplit(url).netloc
        return ToolResult(
            tool=self.name,
            ok=True,
            output=spoken,
            # 审计记主机名和是不是搜索，不记完整 URL：查询词可能是私事。
            audit={"decision": "executed", "host": host, "search": bool(query)},
        )


__all__ = ["DEFAULT_SEARCH", "WebOpenTool", "url_problem"]
