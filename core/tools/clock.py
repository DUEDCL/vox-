"""``time.now`` —— 现在几点，本机时钟直接答。

## 为什么这也算一个工具

「现在几点」派给一个 agent 要几秒钟和一次出网，而答案就在本机的时钟里。这类问题是快路径
存在的全部理由：**不是每个请求都需要一个模型**。实测对比：工具路径 15 ms，agent 路径
4.7 秒。

## 为什么不收时区参数

时区是本机的属性，不是这句话的属性。收一个时区参数意味着「现在几点」和「伦敦几点」走同一
个工具，而后者需要一张时区表和一个歧义消解规则；说「现在几点」的人问的是他自己那块表。

## 为什么星期是自己映射的

``%A`` 给的是 locale 的名字，而 Windows 上进程的 locale 取决于代码页和启动方式 —— 同一台
机器上「星期五」和「Friday」都可能出现。这句话要被念出来，所以它必须是确定的中文。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping

from .contract import ToolRequest, ToolResult

#: 星期几。``datetime.weekday()`` 是周一 = 0。
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

#: 时段。念出来的时间带上它才像人说的话：「下午三点」而不是「十五点」。
_PERIODS = ((5, "凌晨"), (8, "早上"), (11, "上午"), (13, "中午"), (18, "下午"), (23, "晚上"))


def _period(hour: int) -> str:
    for edge, name in _PERIODS:
        if hour < edge:
            return name
    return "深夜"


def _twelve(hour: int) -> int:
    """12 小时制的钟点。0 点说「12 点」，13 点说「1 点」。"""
    remainder = hour % 12
    return 12 if remainder == 0 else remainder


class TimeNowTool:
    """当前日期和时间，一句能直接念出来的中文。"""

    name = "time.now"

    def __init__(self, config: Mapping[str, Any] | None = None, *, clock: Any = None) -> None:
        self.config = dict(config) if config is not None else {}
        #: 注入的时钟，测试用。生产上是 ``datetime.now``。
        self.clock = clock or datetime.now

    def describe(self) -> Mapping[str, Any]:
        return {"name": self.name, "arguments": {}, "local_timezone": time.tzname[0]}

    def run(self, request: ToolRequest) -> ToolResult:
        del request  # 这个工具没有参数：时区是本机的属性，不是请求的
        now = self.clock()
        spoken = (
            f"现在是 {now.year} 年 {now.month} 月 {now.day} 日 "
            f"{_WEEKDAYS[now.weekday()]}，{_period(now.hour)} {_twelve(now.hour)} 点 {now.minute} 分"
        )
        return ToolResult(
            tool=self.name,
            ok=True,
            output=spoken,
            audit={
                "decision": "executed",
                # ISO 形式进审计，中文那句进回复：审计要能被机器读，回复要能被人听。
                "iso": now.isoformat(timespec="seconds"),
            },
        )


__all__ = ["TimeNowTool"]
