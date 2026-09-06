from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from datetime import datetime, timezone
from typing import Any


class VoiceState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CANCELLED = "cancelled"
    ERROR = "error"


#: 允许的迁移。**六个状态本身是冻结的**（见 .claude/CLAUDE.md 与
#: `contracts/voice-events.schema.json` 的 SHA-256），这张表是它们之间的边。
#:
#: `LISTENING -> IDLE` 是 2026-08-30 加的一条边，理由是一个真实缺陷：唤醒之后没人说话时
#: 聆听会结束（流式识别器静默 2.4 秒就报端点），而此前没有任何一条边能表达「听完了但
#: 什么都没听到」—— 于是状态机只能停在 LISTENING，唤醒球一直显示「在听」，而采集已经
#: 回到唤醒模式了。用 CANCELLED 代替是错的：那一步会发 `turn.cancelled`，而这里根本没有
#: 过一个回合。`SPEAKING -> IDLE` 早就在表里，所以「从非终态回待机」不是新姿态。
_ALLOWED: dict[VoiceState, set[VoiceState]] = {
    VoiceState.IDLE: {VoiceState.LISTENING},
    VoiceState.LISTENING: {VoiceState.THINKING, VoiceState.IDLE, VoiceState.CANCELLED, VoiceState.ERROR},
    VoiceState.THINKING: {VoiceState.SPEAKING, VoiceState.CANCELLED, VoiceState.ERROR},
    VoiceState.SPEAKING: {VoiceState.LISTENING, VoiceState.IDLE, VoiceState.CANCELLED, VoiceState.ERROR},
    VoiceState.CANCELLED: {VoiceState.IDLE, VoiceState.LISTENING},
    VoiceState.ERROR: {VoiceState.IDLE, VoiceState.LISTENING},
}


@dataclass
class VoiceStateMachine:
    state: VoiceState = VoiceState.IDLE
    sequence: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def transition(self, target: VoiceState, reason: str = "") -> dict[str, Any]:
        if target not in _ALLOWED[self.state] and target != self.state:
            raise ValueError(f"invalid voice transition: {self.state} -> {target}")
        previous = self.state
        self.state = target
        self.sequence += 1
        event = {
            "version": "1",
            "type": "state.changed",
            "id": f"state-{self.sequence}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"from": previous.value, "to": target.value, "reason": reason},
        }
        self.history.append(event)
        return event

    def reset(self) -> dict[str, Any]:
        if self.state == VoiceState.IDLE:
            return self.transition(VoiceState.IDLE, "already idle")
        return self.transition(VoiceState.IDLE, "reset")
