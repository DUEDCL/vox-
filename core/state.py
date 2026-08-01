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


_ALLOWED: dict[VoiceState, set[VoiceState]] = {
    VoiceState.IDLE: {VoiceState.LISTENING},
    VoiceState.LISTENING: {VoiceState.THINKING, VoiceState.CANCELLED, VoiceState.ERROR},
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
