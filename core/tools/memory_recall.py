"""``memory.recall`` —— 翻自己的记忆。**「有记忆的助手」与「问答机器」的分界线就在这里。**

## 为什么这一个工具值得单独存在

在它之前，记忆只以一种方式到达模型：`Dispatcher._recall_context()` 在每一轮**自动**塞进
`Task.context`（`facts()` + `recent_turns()`）。那是**被动召回** —— 按当前这句话去查，查到
什么给什么。于是「我上次说想买的那个东西叫什么」这类问题必然答不了：使用者这句话里没有那个
东西的名字，被动召回按这句话的词去检索，检索不到。

主动查把那一步交给模型：它知道自己缺什么，于是它自己写检索词。

## 出网面没有变大，这一点要说清楚

记忆文本**在这个工具之前就已经在出网了** —— `_recall_context()` 把它拼进发给云端 LLM 的
请求里。所以这里新增的不是「记忆会不会出网」，而是「谁决定查哪一条」。红线仍然成立：
记忆只存文本、永不存音频；**记忆事件**仍然只带 id / 计数 / 标签，本工具不发事件。

## 三条边界

1. **只读。** 没有 `memory.write`。写记忆由 `write_turn` 与隐式提炼（`promote.py`）负责，
   它们有自己的去重与密钥形状过滤。给模型一支能往长期记忆里写字的笔，等于让一次转写错误
   变成一条永久的「事实」。
2. **不查 `long` 层。** 那一层是工具审计与派发统计（谁跑了什么、成功率），对使用者的问题
   没有用，而它包含每一次命令执行的记录 —— 一句「搜一下我的历史」不该把审计日志念出来。
3. **命中为空是成功不是失败。** `ok=False` 会让模型以为记忆坏了然后向使用者道歉；
   「没有找到」是一个正确的答案，它该原样转达。
"""

from __future__ import annotations

from typing import Any, Mapping

from .contract import ToolRequest, ToolResult

#: 一次最多回几条。**语音的上限，不是数据库的上限** —— 念五条以上的记忆没人听得完，
#: 而模型拿到二十条只会挑前几条用，中间那些纯粹是延迟与费用。
DEFAULT_LIMIT = 5
MAX_LIMIT = 10

#: 每条截多长。整条塞进去会让一条长记忆挤掉其余四条。
SNIPPET = 200

#: 能查哪几层。``long`` 刻意不在里面（见模块头第 2 条）。
SCOPES: tuple[str, ...] = ("mid", "short")


class MemoryRecallTool:
    """按关键词翻记忆库，返回能直接念的几行。"""

    name = "memory.recall"

    def __init__(self, recaller: Any, config: Mapping[str, Any] | None = None) -> None:
        #: `MemoryRecaller`（或任何有 `recall(query, scope=, kind=, limit=)` 的东西）。
        #: **注入而不是自己开** —— 这个工具开一个自己的连接就等于绕过了记忆层的那把
        #: `RLock`，而控制台是多线程的（HTTP 工作线程 + pump + 音频回调）。
        self.recaller = recaller
        self.config = dict(config) if config is not None else {}

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {"query": "要找什么", "limit": f"最多几条（默认 {DEFAULT_LIMIT}）"},
            "scopes": list(SCOPES),
        }

    def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "") or "").strip()
        if not query:
            return ToolResult(
                tool=self.name,
                ok=False,
                error="要查什么没有说",
                audit={"decision": "refused", "reason": "empty query"},
            )
        limit = self._limit(request.arguments.get("limit"))
        rows: list[Any] = []
        seen: set[str] = set()
        for scope in SCOPES:
            try:
                hits = self.recaller.recall(query, scope=scope, limit=limit)
            except Exception as exc:  # noqa: BLE001 - 记忆坏了不该让这一轮崩
                return ToolResult(
                    tool=self.name,
                    ok=False,
                    error=f"记忆库读不了：{type(exc).__name__}: {exc}",
                    audit={"decision": "failed", "scope": scope},
                )
            for row in hits or ():
                key = str(getattr(row, "id", "") or getattr(row, "text", ""))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
            if len(rows) >= limit:
                break
        rows = rows[:limit]
        if not rows:
            # **空结果是成功。** `ok=False` 会让模型以为记忆坏了然后道歉，
            # 而「没找到」本身就是一个该被原样转达的答案。
            return ToolResult(
                tool=self.name,
                ok=True,
                output=f"记忆里没有关于「{query}」的内容。",
                audit={"decision": "executed", "hits": 0},
            )
        lines = [self._line(row) for row in rows]
        return ToolResult(
            tool=self.name,
            ok=True,
            output="\n".join(lines),
            # **审计不带正文。** 记忆是个人数据，而审计层是 `long` scope、会被长期保留 ——
            # 把查到的原文写进去等于把同一段个人数据抄进另一层。计数与层名够诊断了。
            audit={"decision": "executed", "hits": len(rows), "query_len": len(query)},
        )

    @staticmethod
    def _line(row: Any) -> str:
        """一条记忆一行。带上时间与层名，因为「什么时候说的」常常就是问题本身。"""
        text = str(getattr(row, "text", "") or "").strip().replace("\n", " ")
        when = str(getattr(row, "created_at", "") or "")[:16].replace("T", " ")
        scope = str(getattr(row, "scope", "") or "")
        label = {"mid": "记住的", "short": "最近说的"}.get(scope, scope)
        head = f"[{label}{' ' + when if when else ''}] " if label else ""
        return f"{head}{text[:SNIPPET]}"

    @staticmethod
    def _limit(raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_LIMIT
        return max(1, min(MAX_LIMIT, value))


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "SCOPES", "MemoryRecallTool"]
