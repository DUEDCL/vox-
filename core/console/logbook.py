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

#: 环形缓冲的条数上限。500 条约等于几十轮对话的细节，够回答「刚才那次为什么失败」。
MAX_ENTRIES = 500

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

    def read(self, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        """游标之后的条目。``next`` 是下次该传的游标。

        缓冲被裁剪过时 ``dropped`` 是非零：客户端看到它就知道中间断了一截，而不是
        以为那段时间什么都没发生。
        """
        with self._lock:
            entries = [item for item in self._entries if int(item["seq"]) > int(cursor)]
            oldest = int(self._entries[0]["seq"]) if self._entries else self._next
            dropped = max(0, oldest - 1 - int(cursor)) if cursor else 0
            window = entries[: max(1, int(limit))]
            return {
                "entries": [dict(item) for item in window],
                "next": int(window[-1]["seq"]) if window else int(cursor),
                "dropped": dropped,
                "total": self._next - 1,
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = ["LEVELS", "MAX_ENTRIES", "MAX_VALUE_CHARS", "Logbook"]
