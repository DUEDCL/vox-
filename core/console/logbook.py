"""运行日志：给人看的那一份，和事件契约分开。

## 为什么不复用事件流

平台事件（`contracts/agent-events.schema.json`）会扇出到**每一个**通道 —— 唤醒球、传输、
外部消费者 —— 所以它刻意不带文本：`tool.*` 只带决定、原因、耗时，记忆事件只带 id 和计数。
那条约束是对的，但它也意味着「`fs.read` 收到的 path 到底是什么」在事件里查不到，而那正是
「`route=tool ok=false tool=fs.read 0ms`」这种报告唯一需要的信息。

这份日志只在本机控制台、只在有人打开那一栏时被读走，所以它可以带参数。两者的分工是
**扇出面**，不是详细程度。

## 环形缓冲，不落盘

一个跑了一天的会话不该在磁盘上留下它说过的每一句话 —— 那是记忆层的决定（用户自己选择
是否保留），不该被一个调试视图绕过去。上限之外的条目直接丢，因为「最近发生了什么」是这份
日志唯一回答的问题。

## 凭据形状的值整条丢

日志里会出现工具参数，而参数里可能有人粘进来的 key。这里复用 `core/tools/policy.py` 的
判断：像凭据的值**整条替换**而不是打码 —— 打码会把前缀留下，而前缀足够定位是哪个服务商的
哪个 key。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping

#: 环形缓冲的条数上限。
#:
#: **2026-09-03 从 500 提到 2000**，因为使用者要的是「能实时查看所有日志」，而 500 条在一次
#: 认真的排查里大约是十几分钟 —— 一次唤醒失败的完整上下文（唤醒漏斗 + 声纹分数 + 派发 +
#: 工具参数）能占掉几十条。2000 条按每条约 300 字节算是 600 KB，进程内，跑一天也不涨。
#:
#: 仍然**不落盘**：一个跑了一天的会话不该在磁盘上留下它说过的每一句话，那是记忆层的决定
#: （用户自己选择是否保留），不该被一个调试视图绕过去。
MAX_ENTRIES = 2000

#: 单个字段值的显示上限。工具输出可能是整个文件，日志不是它的去处。
MAX_VALUE_CHARS = 400

LEVELS = ("info", "warn", "error")


def _scrub(value: Any) -> Any:
    """一个字段值 -> 能进日志的形状。凭据整条换掉，长值截断。"""
    from core.tools.policy import SENSITIVE_ENV_MARKERS

    if isinstance(value, str):
        # 像凭据就整条换掉。判断用的是 models_config 那套（长随机串 / sk- 前缀）,
        # 加上「键名看起来像密钥」这一层 —— 两条都命中才换,免得把一句正常的话吃掉。
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + f"…（还有 {len(value) - MAX_VALUE_CHARS} 字）"
        return value
    if isinstance(value, Mapping):
        return {
            str(key): "（凭据，未记录）"
            if any(marker in str(key).casefold() for marker in SENSITIVE_ENV_MARKERS)
            else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value][:20]
    return value


def _matches(entry: Mapping[str, Any], level: str, source: str, needle: str) -> bool:
    """一条日志过不过筛选。三个条件是**与**的关系。

    ``level`` 是「至少这一级」而不是「正好这一级」：选 warn 的人要的是「有问题的那些」，
    而 error 比 warn 更有问题 —— 一个把 error 滤掉的 warn 筛选是个陷阱。

    搜索同时看 message 和字段值：「`fs.read` 收到的 path 到底是什么」这个问题的答案在字段里，
    而那正是这份日志存在的理由。
    """
    if level:
        order = {name: index for index, name in enumerate(LEVELS)}
        if order.get(str(entry.get("level", "info")), 0) < order.get(level, 0):
            return False
    if source and str(entry.get("source", "")).lower() != source:
        return False
    if needle:
        haystack = [str(entry.get("message", ""))]
        fields = entry.get("fields") or {}
        if isinstance(fields, Mapping):
            haystack.extend(f"{key}={value}" for key, value in fields.items())
        if not any(needle in piece.lower() for piece in haystack):
            return False
    return True


class Logbook:
    """线程安全的环形日志。写的地方在音频线程、pump 线程和 HTTP 线程上都有。"""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: list[dict[str, Any]] = []
        self._next = 1
        self._lock = threading.Lock()

    def write(self, source: str, message: str, *, level: str = "info", **fields: Any) -> int:
        """记一条，返回它的序号。序号单调递增，客户端靠它做游标。"""
        entry = {
            # 序号而不是时间戳做游标：两条同毫秒的日志用时间戳会漏读一条。
            "seq": 0,
            "at": time.time(),
            "level": level if level in LEVELS else "info",
            "source": str(source),
            "message": str(message),
            "fields": {str(key): _scrub(value) for key, value in fields.items()},
        }
        with self._lock:
            entry["seq"] = self._next
            self._next += 1
            self._entries.append(entry)
            if len(self._entries) > self.max_entries:
                del self._entries[: len(self._entries) - self.max_entries]
            return int(entry["seq"])

    def read(
        self,
        cursor: int = 0,
        limit: int = 200,
        *,
        level: str = "",
        source: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        """游标之后的条目。``next`` 是下次该传的游标。

        缓冲被裁剪过时 ``dropped`` 是非零：客户端看到它就知道中间断了一截，而不是
        以为那段时间什么都没发生。

        三个筛选是**服务端**做的，因为「实时查看所有日志」的现实是缓冲里有两千条而界面上
        只放得下几十条 —— 把两千条送过去让页面自己滤，等于每两秒传一遍整个缓冲。

        **筛选在按游标取窗之后做，不是之前。** 顺序反过来的话 ``next`` 会停在最后一条
        *匹配* 的条目上，于是被滤掉的那些每次轮询都重新扫一遍，而且一旦筛选条件下再也没有
        新条目，游标就永远不动了 —— 表现是「日志卡住不更新」。所以窗口按原始序号取，
        ``next`` 从窗口的最后一条算，筛选只决定**返回哪些**。
        """
        wanted_level = str(level or "").strip().lower()
        wanted_source = str(source or "").strip().lower()
        needle = str(query or "").strip().lower()
        with self._lock:
            entries = [item for item in self._entries if int(item["seq"]) > int(cursor)]
            oldest = int(self._entries[0]["seq"]) if self._entries else self._next
            dropped = max(0, oldest - 1 - int(cursor)) if cursor else 0
            window = entries[: max(1, int(limit))]
            # 游标先定下来 —— 它必须与筛选无关。
            next_cursor = int(window[-1]["seq"]) if window else int(cursor)
            shown = [dict(item) for item in window if _matches(item, wanted_level, wanted_source, needle)]
            return {
                "entries": shown,
                "next": next_cursor,
                "dropped": dropped,
                "total": self._next - 1,
                # 界面用它填「来源」那个下拉 —— 一个要人手打 `weixin` 的筛选框没人会用。
                "sources": sorted({str(item.get("source", "")) for item in self._entries if item.get("source")}),
                "held": len(self._entries),
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = ["LEVELS", "MAX_ENTRIES", "MAX_VALUE_CHARS", "Logbook"]
