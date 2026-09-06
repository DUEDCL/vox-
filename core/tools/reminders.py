"""``timer.remind`` —— 「二十分钟后提醒我关火」。

## 为什么这一个工具和别的不一样

其余八个工具都是**被动**的：使用者说一句，Vox 做一件事，回一句话。提醒是第一个让它**主动
开口**的能力 —— 时间到了没人问它，它自己说。那是「一个会应答的程序」和「一个助手」之间的
差别，也是这个文件存在的全部理由。

## 落盘，因为提醒的价值全在于它一定会来

存 `.vox/reminders.json`（gitignored，和确认音缓存同一个目录）。只在内存里的话，Vox 重启
一次就把「二十分钟后提醒我关火」丢了 —— 而使用者不会知道，他只会在锅烧干的时候发现。

代价说清楚：**提醒文本是使用者说的话，它会落盘**。记忆库（`memory/`）已经是同一个立场，
而这里的量小得多、也更短命（播报完就删）。凭据形状的文本仍然整条拒绝，理由同记忆层。

## 过了时间才开机怎么办

Vox 关着的时候提醒到期了 —— **仍然要说**，但要说清它迟了（「这是两小时前的提醒」）。
静默丢掉是最坏的选择：使用者以为提醒会来，而它永远不来，一次之后他就不会再用这个功能了。
超过 `STALE_HOURS` 的直接丢掉并留一条日志 —— 一个三天前的「记得倒垃圾」念出来只是噪声。

## 时间怎么给

    {"after_minutes": 20, "text": "关火"}      二十分钟后
    {"at": "14:30", "text": "开会"}            今天下午两点半（已经过了就算明天）
    {}                                        列出现在有哪些
    {"cancel": "关火"}                        按名字取消

`after_minutes` 与 `at` 同时给时 `at` 赢 —— 一个具体时刻比一个相对量更接近使用者的意思，
和 `system.volume` 的 `level`/`delta` 同一条。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .contract import ToolRequest, ToolResult

#: 最多同时挂几条。**上限不是数据库限制，是「念得完」**：十条提醒里第七条到期时使用者早就
#: 忘了他设过它。也顺带挡住一个把工具当循环用的模型。
MAX_PENDING = 12

#: 迟到超过这么久就不念了，只留一条日志。三天前的「记得倒垃圾」念出来只是噪声。
STALE_HOURS = 6.0

#: 一条提醒的文本上限。它会被念出来，而念一段 500 字的提醒没有人要。
MAX_TEXT = 120


@dataclass(frozen=True)
class Reminder:
    """一条待播报的提醒。``at`` 是本地时间的 ISO 字符串（秒精度）。"""

    id: str
    at: str
    text: str

    def due_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.at)
        except ValueError:
            # 坏掉的时间戳当「现在就到期」处理：宁可早念一次，也不要让它永远挂着。
            return datetime.now()

    def spoken_when(self, now: datetime | None = None) -> str:
        """「还有多久」的人话。提醒被列出来时使用者问的就是这个。"""
        left = self.due_at() - (now or datetime.now())
        seconds = int(left.total_seconds())
        if seconds <= 0:
            return "已经到点了"
        if seconds < 90:
            return f"还有 {seconds} 秒"
        if seconds < 3600:
            return f"还有 {seconds // 60} 分钟"
        hours, minutes = divmod(seconds // 60, 60)
        return f"还有 {hours} 小时" + (f" {minutes} 分钟" if minutes else "")


@dataclass
class ReminderStore:
    """`.vox/reminders.json` 的读写。**一把锁** —— 工具在 HTTP/派发线程上写，
    到期检查在 pump 线程上读，而这两个线程在控制台里一定同时存在。"""

    path: Path
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def load(self) -> list[Reminder]:
        with self._lock:
            return self._read()

    def _read(self) -> list[Reminder]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found: list[Reminder] = []
        for row in raw if isinstance(raw, list) else ():
            if not isinstance(row, Mapping):
                continue
            text = str(row.get("text", "") or "").strip()
            at = str(row.get("at", "") or "").strip()
            if text and at:
                found.append(Reminder(str(row.get("id") or uuid.uuid4().hex), at, text))
        return sorted(found, key=lambda item: item.at)

    def _write(self, rows: list[Reminder]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [{"id": row.id, "at": row.at, "text": row.text} for row in rows],
            ensure_ascii=False,
            indent=2,
        )
        # 原子替换：一个被写坏一半的提醒文件会让**所有**提醒消失，而不只是这一条。
        temp = self.path.with_suffix(".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    def add(self, reminder: Reminder) -> int:
        """加一条，返回加完之后还剩几条。超上限时抛 `ValueError`。"""
        with self._lock:
            rows = self._read()
            if len(rows) >= MAX_PENDING:
                raise ValueError(f"已经挂着 {len(rows)} 条提醒了（上限 {MAX_PENDING}）")
            rows.append(reminder)
            self._write(sorted(rows, key=lambda item: item.at))
            return len(rows)

    def remove(self, ids: set[str]) -> int:
        with self._lock:
            rows = self._read()
            keep = [row for row in rows if row.id not in ids]
            if len(keep) != len(rows):
                self._write(keep)
            return len(rows) - len(keep)

    def take_due(self, now: datetime | None = None) -> tuple[list[Reminder], list[Reminder]]:
        """取走已到期的。返回 (要念的, 迟太久只记日志的)。**取走即删** —— 一条被念过的
        提醒如果还留在文件里，下一次 tick 会再念一遍。"""
        moment = now or datetime.now()
        with self._lock:
            rows = self._read()
            due = [row for row in rows if row.due_at() <= moment]
            if not due:
                return [], []
            self._write([row for row in rows if row not in due])
        cutoff = moment - timedelta(hours=STALE_HOURS)
        fresh = [row for row in due if row.due_at() >= cutoff]
        stale = [row for row in due if row.due_at() < cutoff]
        return fresh, stale


__all__ = ["MAX_PENDING", "MAX_TEXT", "STALE_HOURS", "Reminder", "ReminderStore"]


def default_store_path() -> Path:
    """`.vox/reminders.json`，和确认音缓存同一个目录（都是产物，都 gitignored）。"""
    return Path(__file__).resolve().parents[2] / ".vox" / "reminders.json"


class TimerRemindTool:
    """设 / 列 / 取消提醒。**到时候由运行时播报，这一层只登记。**

    工具不持有 TTS：一个能自己开口的工具会绕过状态机（球不会亮、静音窗不会挂、打断不会
    生效）。所以到期检查在 `pump()` 那一侧，走的是和普通回答同一条播报路径。
    """

    name = "timer.remind"

    def __init__(self, config: Mapping[str, Any] | None = None, *, store: Any = None,
                 clock: Any = None) -> None:
        self.config = dict(config) if config is not None else {}
        self.store = store if store is not None else ReminderStore(default_store_path())
        #: 注入的时钟，测试用。生产上是 `datetime.now`。
        self.clock = clock or datetime.now

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {
                "after_minutes": "几分钟后提醒",
                "at": "在某个时刻提醒，HH:MM（已经过了就算明天）",
                "text": "提醒我什么",
                "cancel": "按名字取消一条",
            },
        }

    def run(self, request: ToolRequest) -> ToolResult:
        arguments = request.arguments or {}
        try:
            if arguments.get("cancel"):
                return self._cancel(str(arguments["cancel"]))
            text = str(arguments.get("text", "") or "").strip()
            when = self._when(arguments)
            if when is None and not text:
                return self._list()
            if when is None:
                return ToolResult(
                    tool=self.name, ok=False,
                    error="没说什么时候 —— 给 after_minutes 或者 at",
                    audit={"decision": "refused", "reason": "no time"},
                )
            if not text:
                return ToolResult(
                    tool=self.name, ok=False,
                    error="没说提醒什么",
                    audit={"decision": "refused", "reason": "no text"},
                )
            return self._set(when, text)
        except ValueError as exc:
            return ToolResult(
                tool=self.name, ok=False, error=str(exc),
                audit={"decision": "refused", "reason": "full"},
            )
        except Exception as exc:  # noqa: BLE001 - 存不下来要说得清
            return ToolResult(
                tool=self.name, ok=False,
                error=f"存不下来：{type(exc).__name__}: {exc}",
                audit={"decision": "failed"},
            )

    def _when(self, arguments: Mapping[str, Any]) -> datetime | None:
        """`at` 赢过 `after_minutes` —— 一个具体时刻比一个相对量更接近使用者的意思。"""
        now = self.clock()
        at = str(arguments.get("at", "") or "").strip()
        if at:
            for shape in ("%H:%M", "%H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(at, shape)
                except ValueError:
                    continue
                if shape.startswith("%Y"):
                    return parsed
                target = now.replace(
                    hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
                )
                # 已经过了就算明天 —— 说「六点提醒我」的人在晚上八点说的是明天早上。
                return target if target > now else target + timedelta(days=1)
        raw = arguments.get("after_minutes", arguments.get("after"))
        if raw is None or isinstance(raw, bool):
            return None
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        return now + timedelta(minutes=minutes)

    def _set(self, when: datetime, text: str) -> ToolResult:
        reminder = Reminder(uuid.uuid4().hex, when.replace(microsecond=0).isoformat(),
                            text[:MAX_TEXT])
        pending = self.store.add(reminder)
        clock = when.strftime("%H:%M")
        return ToolResult(
            tool=self.name, ok=True,
            output=f"好，{clock} 提醒你「{reminder.text}」（{reminder.spoken_when(self.clock())}）",
            # **正文不进审计。** 提醒是使用者说的话，而审计层长期保留 —— 和 memory.recall
            # 同一条立场：计数够诊断了。
            audit={"decision": "executed", "action": "set", "at": reminder.at,
                   "pending": pending},
        )

    def _list(self) -> ToolResult:
        rows = self.store.load()
        if not rows:
            return ToolResult(tool=self.name, ok=True, output="现在没有提醒。",
                              audit={"decision": "executed", "action": "list", "pending": 0})
        now = self.clock()
        lines = [f"{row.due_at().strftime('%H:%M')} {row.text}（{row.spoken_when(now)}）"
                 for row in rows]
        return ToolResult(
            tool=self.name, ok=True, output="；".join(lines),
            audit={"decision": "executed", "action": "list", "pending": len(rows)},
        )

    def _cancel(self, wanted: str) -> ToolResult:
        needle = wanted.strip().casefold()
        rows = self.store.load()
        hits = {row.id for row in rows if needle and needle in row.text.casefold()}
        if not hits:
            return ToolResult(
                tool=self.name, ok=False,
                error=f"没有叫「{wanted}」的提醒" + (f"。现在有：{'、'.join(r.text[:12] for r in rows)}"
                                                if rows else "（现在一条都没有）"),
                audit={"decision": "refused", "reason": "no match", "pending": len(rows)},
            )
        removed = self.store.remove(hits)
        return ToolResult(
            tool=self.name, ok=True, output=f"取消了 {removed} 条提醒",
            audit={"decision": "executed", "action": "cancel", "removed": removed},
        )


__all__ = ["MAX_PENDING", "MAX_TEXT", "STALE_HOURS", "Reminder", "ReminderStore",
           "TimerRemindTool", "default_store_path"]
