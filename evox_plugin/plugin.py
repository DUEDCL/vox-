from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.state import VoiceState, VoiceStateMachine


@dataclass
class VoicePlugin:
    """EvoX-facing voice plugin facade.

    Audio engines are injected later; an optional ConversationTransport wires
    recognized text into an EvoX session. All emitted events follow
    contracts/voice-events.schema.json.
    """

    machine: VoiceStateMachine = field(default_factory=VoiceStateMachine)
    events: list[dict] = field(default_factory=list)
    running: bool = False
    paused: bool = False
    transport: Any = None
    audio_capture: Any = None
    last_turn_id: str | None = None
    last_reply: str | None = None

    def _event(self, event_type: str, payload: dict | None = None) -> dict:
        event = {
            "version": "1",
            "type": event_type,
            "id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        self.events.append(event)
        return event

    def _state_event(self, target: VoiceState, reason: str) -> dict:
        event = self.machine.transition(target, reason)
        self.events.append(event)
        return event

    # -- lifecycle tools -----------------------------------------------------

    def start(self) -> dict:
        self.running = True
        self.paused = False
        try:
            if self.audio_capture is not None:
                self.audio_capture.start()
        except Exception:
            self.running = False
            raise
        return self._event("state.changed", {"to": self.machine.state.value, "running": True})

    def stop(self) -> dict:
        if self.audio_capture is not None:
            self.audio_capture.stop()
        self.running = False
        self.paused = False
        # Direct reset is a deliberate escape hatch: stop must succeed from any
        # state, including ones the machine cannot transition out of.
        self.machine.state = VoiceState.IDLE
        return self._event("state.changed", {"to": "idle", "running": False})

    def pause(self) -> dict:
        if not self.running:
            raise RuntimeError("voice plugin is not running")
        if self.audio_capture is not None:
            self.audio_capture.stop()
        self.paused = True
        return self._event("state.changed", {"to": self.machine.state.value, "paused": True})

    def resume(self) -> dict:
        if not self.running:
            raise RuntimeError("voice plugin is not running")
        if self.audio_capture is not None:
            self.audio_capture.start()
        self.paused = False
        return self._event("state.changed", {"to": self.machine.state.value, "paused": False})

    # -- wake / turn tools ---------------------------------------------------

    def wake_detected(self, keyword: str, score: float) -> list[dict]:
        if not self.running:
            raise RuntimeError("voice plugin is not running")
        if self.paused:
            raise RuntimeError("voice plugin is paused")
        state_event = self._state_event(VoiceState.LISTENING, "wake detected")
        wake_event = self._event("wake.detected", {"keyword": keyword, "score": score})
        return [wake_event, state_event]

    def wake_test(self, keyword: str = "小沃小沃", score: float = 1.0) -> list[dict]:
        """Run a synthetic wake through the same path as a real detection."""
        events = self.wake_detected(keyword, score)
        events[0]["payload"]["synthetic"] = True
        return events

    def submit_text(self, text: str) -> list[dict]:
        if self.machine.state != VoiceState.LISTENING:
            raise RuntimeError("text can only be submitted while listening")
        events = [self._event("turn.started", {"text": text})]
        events.append(self._event("asr.final", {"text": text}))
        events.append(self._state_event(VoiceState.THINKING, "asr final"))
        if self.transport is not None:
            result = self.transport.send(text)
            self.last_turn_id = result.get("turn_id")
            self.last_reply = result.get("reply")
        return events

    def complete_turn(self, reply: str) -> list[dict]:
        """Finish the pending turn: reply -> speech -> back to listening."""
        if self.machine.state != VoiceState.THINKING:
            raise RuntimeError("turn can only complete while thinking")
        events = [
            self._event("llm.delta", {"text": reply}),
            self._event("tts.chunk", {"index": 0, "text": reply}),
        ]
        events.append(self._state_event(VoiceState.SPEAKING, "tts playback"))
        events.append(self._event("turn.done", {}))
        events.append(self._state_event(VoiceState.LISTENING, "continuous conversation"))
        self.last_turn_id = None
        self.last_reply = None
        return events

    def cancel(self) -> dict:
        if self.transport is not None and self.last_turn_id:
            self.transport.cancel(self.last_turn_id)
            self.last_turn_id = None
        if self.machine.state in {VoiceState.LISTENING, VoiceState.THINKING, VoiceState.SPEAKING}:
            self._state_event(VoiceState.CANCELLED, "user cancel")
        return self._event("turn.cancelled", {})

    # -- inspection tools ----------------------------------------------------

    def status(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "state": self.machine.state.value,
            "events": len(self.events),
        }

    def devices(self) -> dict:
        """List audio input devices when an audio backend is installed."""
        try:
            import sounddevice as sd  # type: ignore

            inputs = [
                {
                    "index": index,
                    "name": device["name"],
                    "channels": device["max_input_channels"],
                    "sample_rate": device["default_samplerate"],
                }
                for index, device in enumerate(sd.query_devices())
                if device["max_input_channels"] > 0
            ]
            return {"available": True, "inputs": inputs}
        except Exception as exc:
            return {"available": False, "inputs": [], "reason": f"{type(exc).__name__}: {exc}"}

    def diagnose(self) -> dict:
        """Report provider/bridge readiness without leaking credentials."""
        from core.providers import SherpaKeywordProvider, VoxCordAdapter

        provider = VoxCordAdapter().load()
        root = Path(__file__).resolve().parents[1]
        kws_root = Path(os.getenv(
            "EVOX_KWS_MODEL_DIR",
            root / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
        ))
        local_kws = SherpaKeywordProvider(kws_root)
        return {
            "running": self.running,
            "state": self.machine.state.value,
            "local_voice": {
                "kws_model_ready": local_kws.available,
                "kws_model_dir": str(kws_root),
                "vad_model_ready": (root / "models" / "silero_vad.onnx").is_file(),
            },
            "provider": {
                "available": provider.available,
                "source": provider.source,
                "details": provider.details,
            },
            "bridge": {
                "url": os.getenv("EVOX_VOICE_BRIDGE_URL", "http://localhost:8765"),
                "token_configured": bool(os.getenv("EVOX_VOICE_BRIDGE_TOKEN")),
            },
            "transport_attached": self.transport is not None,
            "capture_attached": self.audio_capture is not None,
            "audio_backend": self.devices()["available"],
        }
