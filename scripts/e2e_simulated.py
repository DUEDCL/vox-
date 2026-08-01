"""Simulated end-to-end run: wake -> ASR -> EvoX bridge -> TTS -> continuous
conversation -> cancel -> stop. Uses a mock transport; no microphone, no real
EvoX session. Verification level: SIMULATED.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state import VoiceState
from evox_plugin import VoicePlugin


class MockTransport:
    """Stand-in for LocalEvoXTransport; echoes a deterministic reply."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.cancelled: list[str] = []

    def send(self, text: str, *, session_id: str | None = None) -> dict:
        self.sent.append(text)
        return {"turn_id": f"sim-turn-{len(self.sent)}", "reply": f"模拟回复：{text}"}

    def cancel(self, turn_id: str) -> dict:
        self.cancelled.append(turn_id)
        return {"cancelled": turn_id}


def main() -> int:
    transport = MockTransport()
    plugin = VoicePlugin(transport=transport)

    plugin.start()
    wake_events = plugin.wake_test()
    assert wake_events[0]["payload"]["synthetic"] is True

    plugin.submit_text("现在几点了")
    assert transport.sent == ["现在几点了"]
    assert plugin.machine.state == VoiceState.THINKING

    reply = plugin.last_reply or ""
    done_events = plugin.complete_turn(reply)
    types = [event["type"] for event in done_events]
    assert types == ["llm.delta", "tts.chunk", "state.changed", "turn.done", "state.changed"], types
    assert plugin.machine.state == VoiceState.LISTENING  # continuous conversation

    # Second turn, cancelled mid-flight.
    plugin.submit_text("明天天气怎么样")
    cancel_event = plugin.cancel()
    assert cancel_event["type"] == "turn.cancelled"
    assert transport.cancelled == ["sim-turn-2"]
    assert plugin.machine.state == VoiceState.CANCELLED

    plugin.stop()
    status = plugin.status()
    assert status["running"] is False and status["state"] == "idle"

    print(f"events emitted: {status['events']}")
    print(f"bridge sends: {len(transport.sent)}, cancels: {len(transport.cancelled)}")
    print("E2E SIMULATED OK (mock transport, no microphone, no real EvoX session)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
