from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.events import build_event
from core.state import VoiceState, VoiceStateMachine


_PUNCT_ONLY_RE = re.compile(r"^[\s。！？；…!?;,.，、]+$")


def split_speech(text: str) -> list[str]:
    """Split a reply into speakable sentences for the TTS queue.

    The orchestrator owns the split (ADR 001 puts the queue outside model
    inference): each segment becomes one ``tts.chunk`` event and one unit of
    playback, so audio starts after the *first* sentence renders instead of
    after the whole reply. Sentence enders are CJK 。！？；… plus ! ? ; ; and
    newline, and an ASCII dot -- except between digits, so 「3.14」 stays one
    utterance.

    ponytail: abbreviations like e.g. still split; prosody cost only, add a
    table if real replies ever hit it.
    """
    segments: list[str] = []
    start = 0
    length = len(text)
    for index, char in enumerate(text):
        if char not in "。！？；…!?;;\n":
            if char != ".":
                continue
            # A dot between digits is a decimal point, not a sentence end.
            digit_before = index > 0 and text[index - 1].isdigit()
            digit_after = index + 1 < length and text[index + 1].isdigit()
            if digit_before and digit_after:
                continue
        segment = text[start : index + 1].strip()
        if segment:
            segments.append(segment)
        start = index + 1
    tail = text[start:].strip()
    if tail:
        segments.append(tail)
    # A lone trailing punctuation run reads worse than it speaks:
    # fold it into the sentence it belongs to.
    merged: list[str] = []
    for segment in segments:
        if merged and _PUNCT_ONLY_RE.match(segment):
            merged[-1] += segment
        else:
            merged.append(segment)
    return merged


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
    tools: Any = None
    tts: Any = None
    #: Where events go besides ``self.events``. One validated envelope, one
    #: positional argument -- the same shape the tool runner, memory, the
    #: dispatcher and the breaker already take, so a desktop bridge, a logger or
    #: a test recorder all attach without an adapter.
    #:
    #: A sink that raises must not end a turn: the conversation is the product,
    #: the event stream is the telemetry. Failures are counted, not propagated.
    on_event: Any = None
    sink_failures: int = 0

    def _emit(self, event: dict) -> dict:
        """Record, then fan out. Every event in this class goes through here."""
        self.events.append(event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                self.sink_failures += 1
        return event

    def _event(self, event_type: str, payload: dict | None = None) -> dict:
        return self._emit(build_event(event_type, payload))

    def _state_event(self, target: VoiceState, reason: str) -> dict:
        return self._emit(self.machine.transition(target, reason))

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
        # Barge-in: a wake hit while the assistant is answering interrupts the
        # turn. The capture loop keeps running through TTS playback, so this is
        # the one path that can stop a speaking turn mid-utterance. ``cancel()``
        # stops the TTS and the transport, then the wake proceeds as usual.
        if self.machine.state in {VoiceState.THINKING, VoiceState.SPEAKING}:
            self.cancel()
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
        """Finish the pending turn: reply -> speech -> back to listening.

        The reply is spoken as a queue of sentences (see ``split_speech``):
        one ``tts.chunk`` per sentence, audio starting once the first one
        renders. A barge-in mid-turn drops the not-yet-spoken remainder;
        memory stores the full reply either way.
        """
        if self.machine.state != VoiceState.THINKING:
            raise RuntimeError("turn can only complete while thinking")
        chunks = split_speech(reply) or [reply]
        events = [self._event("llm.delta", {"text": reply})]
        for index, chunk in enumerate(chunks):
            events.append(self._event("tts.chunk", {"index": index, "text": chunk}))
        events.append(self._state_event(VoiceState.SPEAKING, "tts playback"))
        if self.tts is not None:
            # Audio is the enhancement; a TTS failure must not end the turn.
            try:
                batch = getattr(self.tts, "speak_segments", None)
                if callable(batch):
                    batch(chunks)
                else:
                    # Legacy engines only know single utterances; drain them
                    # here and honour a cancellation marker between sentences.
                    stopped = getattr(self.tts, "is_stopped", None)
                    for chunk in chunks:
                        if callable(stopped) and stopped():
                            break
                        self.tts.speak(chunk)
            except Exception:
                pass
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

    # -- tools ---------------------------------------------------------------

    def attach_tools(self, runner: Any = None) -> dict:
        """Wire the tool gate in. Opt-in, like memory, and for the same reason.

        The plugin does not build a runner of its own. One funnel is what makes
        FR-9.8 true: an agent must not be able to reach a capability by a path the
        user's own voice could not take, and two runners would be two paths.
        """
        self.tools = runner
        return {"tools_attached": runner is not None}

    def attach_tts(self, tts: Any = None) -> dict:
        """Wire a TTS engine into the speak path. Opt-in, like memory and tools.

        Without one, ``complete_turn`` still emits ``tts.chunk`` (the text that
        *would* be spoken) and the state machine still walks speaking -> done; a
        TTS engine only adds the actual audio. A caller that wants silence builds
        a plugin without one.
        """
        self.tts = tts
        return {"tts_attached": tts is not None}

    def attach_capture(self, capture: Any = None, *, on_recognized: Any = None) -> dict:
        """Wire a microphone capture in and point its callbacks here.

        The capture's ``on_wake`` / ``on_reject`` are (re)pointed at this plugin,
        so a wake hit during playback reaches ``wake_detected`` and barges in. The
        caller still chooses the capture (KWS provider, verifier, device); the
        plugin only owns the state machine the hits drive.

        ``on_recognized`` is where transcribed speech lands. It defaults to
        ``submit_text``, which walks the state machine and the memory write but
        does **not** dispatch -- a caller that wants the whole turn (dispatch,
        answer, TTS) passes its own, which is what ``VoiceRuntime`` does with
        ``say``. Defaulting to the dispatching path instead would make the
        plugin depend on a dispatcher it deliberately does not own.
        """
        self.audio_capture = capture
        if capture is not None:
            capture.on_wake = self.wake_detected
            capture.on_reject = self.wake_rejected
            capture.on_recognized = on_recognized or self.submit_text
        return {"capture_attached": capture is not None}

    def run_tool(
        self,
        tool: str,
        arguments: dict | None = None,
        *,
        origin: str = "voice",
        speaker: str | None = None,
    ) -> dict:
        """Run one tool through the attached gate.

        ``speaker`` is supplied by the caller rather than invented here. The
        capture layer knows who was verified; the plugin does not, and guessing
        would hand ``shell.run`` the one credential it exists to demand. With no
        speaker the gate refuses it -- which is the correct answer until the
        dispatcher threads the verified name through.
        """
        from core.tools import ToolRequest

        if self.tools is None:
            return {"ok": False, "error": "tools are not attached"}
        result = self.tools.run(
            ToolRequest(
                tool=tool,
                arguments=dict(arguments or {}),
                origin=origin,
                speaker=speaker,
            )
        )
        return {
            "ok": result.ok,
            "output": result.output,
            "error": result.error,
            "needs_confirmation": result.needs_confirmation,
        }

    def cancel(self) -> dict:
        if self.transport is not None and self.last_turn_id:
            self.transport.cancel(self.last_turn_id)
            self.last_turn_id = None
        if self.tts is not None:
            stopper = getattr(self.tts, "stop", None)
            if callable(stopper):
                try:
                    stopper()
                except Exception:
                    pass
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
        from core.providers import SherpaKeywordProvider, SherpaTtsProvider, VoxCordAdapter

        provider = VoxCordAdapter().load()
        root = Path(__file__).resolve().parents[1]
        kws_root = Path(os.getenv(
            "VOX_KWS_MODEL_DIR",
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
                "tts_model_ready": SherpaTtsProvider(root / "models" / "vits-melo-tts-zh_en").available,
            },
            "speaker": self._diagnose_speaker(),
            "memory": self._diagnose_memory(),
            "tools": self._diagnose_tools(),
            "provider": {
                "available": provider.available,
                "source": provider.source,
                "details": provider.details,
            },
            "bridge": {
                "url": os.getenv("VOX_VOICE_BRIDGE_URL", "http://localhost:8765"),
                "token_configured": bool(os.getenv("VOX_VOICE_BRIDGE_TOKEN")),
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

    def _diagnose_tools(self) -> dict:
        """What the tool gate is enforcing right now.

        The runner's own ``describe()`` is the sanctioned view: it reports
        decisions, counters and flags, and no argument ever passed through it. The
        interesting field is ``warnings`` -- if ``shell.run`` has been switched on,
        this is where the user finds out without reading the config file.
        """
        runner = self.tools
        if runner is None:
            return {
                "attached": False,
                "warnings": ["tools are not attached: the platform can only talk"],
            }
        try:
            described = runner.describe()
        except Exception as exc:
            return {
                "attached": True,
                "warnings": [f"tool gate unreadable: {type(exc).__name__}: {exc}"],
            }
        gate = described.get("policy", {})
        return {
            "attached": True,
            "registered": described.get("registered", []),
            "executed": described.get("executed"),
            "refused": described.get("refused"),
            "confirmations": described.get("confirmations"),
            "audit_attached": described.get("audit_attached"),
            "audit_dropped": described.get("audit_dropped"),
            "roots": gate.get("roots", []),
            "shell_enabled": gate.get("shell_enabled"),
            "shell_allow_count": gate.get("shell_allow_count"),
            "dangerous_patterns": gate.get("dangerous_patterns"),
            "refusals": gate.get("refusals", {}),
            "warnings": list(gate.get("warnings", [])),
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
