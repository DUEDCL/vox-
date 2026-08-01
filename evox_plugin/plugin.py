from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.events import build_event
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
    rejections: int = 0
    last_rejection: dict | None = None
    memory_writer: Any = None
    memory_recaller: Any = None

    def _event(self, event_type: str, payload: dict | None = None) -> dict:
        event = build_event(event_type, payload)
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

    def wake_detected(self, keyword: str, score: float | None) -> list[dict]:
        if not self.running:
            raise RuntimeError("voice plugin is not running")
        if self.paused:
            raise RuntimeError("voice plugin is paused")
        state_event = self._state_event(VoiceState.LISTENING, "wake detected")
        wake_event = self._event("wake.detected", {"keyword": keyword, "score": score})
        return [wake_event, state_event]

    def wake_rejected(self, keyword: str, reason: str, score: float = 0.0) -> dict:
        """Record a wake hit the speaker gate refused.

        Rejection is silent by design (ADR 002): no state transition, no orb, no
        sound, no reply. The event exists only so ``diagnose()`` and the logs can
        answer "why did nothing happen" afterwards -- a security control should
        not confirm its own decision boundary to whoever tripped it.

        Unlike ``wake_detected`` this does not require the plugin to be running:
        the gate can refuse during startup, and losing that record would hide
        exactly the case worth seeing.
        """
        self.last_rejection = {"keyword": keyword, "reason": reason, "score": score}
        self.rejections += 1
        return self._event("wake.rejected", {"keyword": keyword, "reason": reason, "score": score})

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
        self._remember(text, role="user")
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
        self._remember(reply, role="assistant")
        self.last_turn_id = None
        self.last_reply = None
        return events

    # -- memory --------------------------------------------------------------

    def attach_memory(self, writer: Any = None, recaller: Any = None) -> dict:
        """Wire the short-term layer into the turn path.

        Opt-in rather than automatic: attaching is what creates a database file,
        and a voice client that has not been asked to remember anything should
        not leave one behind.
        """
        self.memory_writer = writer
        self.memory_recaller = recaller
        return {"memory_attached": writer is not None or recaller is not None}

    def _remember(self, text: str, *, role: str) -> None:
        """Store one turn, if memory is attached.

        Failures here are swallowed on purpose. Memory is an enhancement to the
        turn, not a precondition for it -- a locked database must not be able to
        break a conversation. The writer's own credential filter still applies,
        so this is also the point where FR-12.6 takes effect.
        """
        if self.memory_writer is None or not (text or "").strip():
            return
        try:
            self.memory_writer.write_turn(text, role=role)
        except Exception:
            pass

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
            "rejections": self.rejections,
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
            "speaker": self._diagnose_speaker(),
            "memory": self._diagnose_memory(),
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

    def _diagnose_speaker(self) -> dict:
        """Speaker gate status: names and counts only, never a vector.

        The verifier's own ``describe()`` is the single sanctioned view of
        enrollment data (it is biometric), so this method reads that and adds the
        capture-side wiring. When the gate is off the report carries an explicit
        ``warnings`` entry -- an escape hatch that looks like a normal
        configuration line is an escape hatch nobody notices is open.
        """
        from core.audio import SpeakerVerificationProvider

        capture = self.audio_capture
        verifier = getattr(capture, "verifier", None)
        if verifier is None:
            verifier = SpeakerVerificationProvider.from_config()
        described = verifier.describe()
        require = bool(getattr(capture, "require_verification", True)) if capture is not None else True
        gate_active = bool(getattr(capture, "gate_active", False)) if capture is not None else False

        warnings: list[str] = []
        if not require:
            warnings.append(
                "require_verification is False: anyone can wake the platform"
            )
        if not described["available"]:
            warnings.append("speaker model is missing; the gate cannot admit anyone")
        if not described["speakers"]:
            warnings.append("nobody is enrolled; run scripts/enroll_speaker.py")
        if capture is not None and require and not gate_active:
            warnings.append("capture requires verification but has no verifier attached")

        return {
            "speaker_model_ready": described["available"],
            "model": described["model"],
            "store": described["store"],
            "enrolled_count": len(described["speakers"]),
            "enrolled": described["speakers"],
            "samples_per_speaker": described.get("samples_per_speaker", {}),
            "threshold": described.get("threshold"),
            "require_verification": require,
            "gate_active": gate_active,
            "rejections": self.rejections,
            "last_rejection": self.last_rejection,
            "warnings": warnings,
        }

    def _diagnose_memory(self) -> dict:
        """Memory readiness: paths and counts, never a remembered sentence.

        Reads the writer's own ``describe()`` for the same reason the speaker
        section reads the verifier's -- it is the one view of the store that is
        guaranteed to carry no text, including no refused text.
        """
        writer = self.memory_writer
        recaller = self.memory_recaller
        report: dict = {
            "attached": writer is not None or recaller is not None,
            "writer_attached": writer is not None,
            "recaller_attached": recaller is not None,
        }
        if writer is None:
            report["warnings"] = ["memory is not attached: this session will not be remembered"]
            return report
        try:
            described = writer.describe()
        except Exception as exc:
            report["warnings"] = [f"memory store unreadable: {type(exc).__name__}: {exc}"]
            return report
        report.update(
            {
                "db_path": described.get("path"),
                "db_exists": described.get("exists"),
                "facts_dir": described.get("facts_dir"),
                "records": described.get("records"),
                "by_scope": described.get("by_scope"),
                "refusals": described.get("refusals"),
                "last_refusal": described.get("last_refusal"),
                "warnings": [],
            }
        )
        return report
