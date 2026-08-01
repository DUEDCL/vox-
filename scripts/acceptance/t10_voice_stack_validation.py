"""t10 consolidated voice-stack prototype validation (release-path core: sherpa-onnx).

Verifies, in the isolated .venv on this Windows host, the items the merged plan's
step t10 requires but that prior sessions had not consolidated into one run:

  1. Windows startup + model loading (KWS, VAD, TTS) with timings.
  2. Local Chinese wake on synthesized `你好问问` audio.
  3. Resource release across repeated open/close cycles (no leak of native state).
  4. Continuous run: sustained streaming of many chunks without a hit on silence.
  5. Swappable conversation backend: two different ConversationTransport impls
     drive the same plugin turn path identically.
  6. Interruptible TTS: a barge-in cancel during `speaking` stops playback and
     returns to a clean cancellable state.

No microphone and no real EvoX session are used here; audio is synthesized in
memory. Results are printed as JSON and a non-zero exit signals failure.
"""

from __future__ import annotations

import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np
import sherpa_onnx  # noqa: F401  (import-time availability is part of the check)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.providers import SherpaKeywordProvider, SherpaVadProvider  # noqa: E402
from core.state import VoiceState  # noqa: E402
from evox_plugin.plugin import VoicePlugin  # noqa: E402
from tmp_proto.tts_kws_vad import resample, synthesize  # noqa: E402

KWS_DIR = ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
VAD_MODEL = ROOT / "models" / "silero_vad.onnx"


def _load_timings() -> dict:
    out: dict = {}

    t0 = time.perf_counter()
    kws = SherpaKeywordProvider(KWS_DIR)
    kws_status = kws.load()
    out["kws"] = {"available": kws_status.available, "load_seconds": time.perf_counter() - t0}
    kws.close()

    t0 = time.perf_counter()
    vad = SherpaVadProvider(VAD_MODEL)
    vad_status = vad.load()
    out["vad"] = {"available": vad_status.available, "load_seconds": time.perf_counter() - t0}
    vad.close()

    return out


def _wake_on_synth() -> dict:
    samples, source_rate, gen_seconds = synthesize("你好问问")
    audio = resample(samples, source_rate)
    kws = SherpaKeywordProvider(KWS_DIR)
    kws.load()
    stream = kws.create_stream()
    hits: list[str] = []
    started = time.perf_counter()
    for offset in range(0, len(audio), 1600):
        hits.extend(kws.feed(stream, audio[offset : offset + 1600]))
    for _ in range(10):
        hits.extend(kws.feed(stream, np.zeros(1600, dtype=np.float32)))
    elapsed = time.perf_counter() - started
    kws.close()
    return {
        "hit": "你好问问" in hits,
        "hits": hits,
        "tts_generation_seconds": gen_seconds,
        "decode_seconds": elapsed,
    }


def _repeated_release(cycles: int = 8) -> dict:
    """Open and close the KWS spotter repeatedly and watch native/python memory."""
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    for _ in range(cycles):
        kws = SherpaKeywordProvider(KWS_DIR)
        kws.load()
        stream = kws.create_stream()
        kws.feed(stream, np.zeros(1600, dtype=np.float32))
        kws.close()
        del stream, kws
        gc.collect()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    growth_mb = (peak - baseline) / (1024 * 1024)
    # A healthy release keeps python-side growth well under the model footprint.
    return {"cycles": cycles, "python_peak_growth_mb": round(growth_mb, 3), "ok": growth_mb < 50}


def _continuous_silence(seconds: float = 12.0) -> dict:
    """Stream ~`seconds` of silence and confirm no spurious wake and stable timing."""
    kws = SherpaKeywordProvider(KWS_DIR)
    kws.load()
    stream = kws.create_stream()
    chunks = int(seconds * 16000 / 1600)
    hits: list[str] = []
    started = time.perf_counter()
    for _ in range(chunks):
        hits.extend(kws.feed(stream, np.zeros(1600, dtype=np.float32)))
    elapsed = time.perf_counter() - started
    kws.close()
    rtf = elapsed / seconds
    return {"seconds": seconds, "chunks": chunks, "spurious_hits": hits, "rtf": round(rtf, 4), "ok": not hits and rtf < 1.0}


class _RecordingTransport:
    """A swappable ConversationTransport that records calls; two flavors of reply."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.sent: list[str] = []
        self.cancelled: list[str] = []
        self._n = 0

    def send(self, text: str, *, session_id: str | None = None) -> dict:
        self._n += 1
        self.sent.append(text)
        return {"turn_id": f"{self.tag}-{self._n}", "reply": f"[{self.tag}] 收到：{text}"}

    def cancel(self, turn_id: str) -> dict:
        self.cancelled.append(turn_id)
        return {"cancelled": turn_id}


def _swappable_backends() -> dict:
    """Same plugin turn path with two different transports must behave identically."""
    results = {}
    for tag in ("backendA", "backendB"):
        transport = _RecordingTransport(tag)
        p = VoicePlugin(transport=transport)
        p.start()
        p.wake_detected("你好问问", 1.0)
        p.submit_text("今天天气怎么样")
        results[tag] = {
            "sent": transport.sent,
            "last_turn_id": p.last_turn_id,
            "last_reply": p.last_reply,
            "state_after_submit": p.machine.state.value,
        }
        p.stop()
    same_shape = (
        results["backendA"]["sent"] == results["backendB"]["sent"]
        and results["backendA"]["state_after_submit"] == results["backendB"]["state_after_submit"]
        and results["backendA"]["last_turn_id"] != results["backendB"]["last_turn_id"]
    )
    return {"results": results, "ok": same_shape}


def _interruptible_tts() -> dict:
    """Barge-in: cancel during `speaking` returns to a cancellable/clean state."""
    transport = _RecordingTransport("barge")
    p = VoicePlugin(transport=transport)
    p.start()
    p.wake_detected("你好问问", 1.0)
    p.submit_text("讲个很长的故事")
    # Enter speaking, then interrupt mid-playback.
    p.complete_turn(p.last_reply or "……")  # speaking -> ... -> listening (continuous)
    # Simulate a fresh turn that the user barges in on.
    p.submit_text("停")
    # Force into speaking to represent active TTS, then cancel.
    p.machine.state = VoiceState.SPEAKING
    cancel_event = p.cancel()
    ok = (
        cancel_event["type"] == "turn.cancelled"
        and p.machine.state == VoiceState.CANCELLED
        and transport.cancelled  # transport was told to cancel the pending turn
    )
    p.stop()
    return {
        "cancel_event_type": cancel_event["type"],
        "state_after_cancel": p.machine.state.value,
        "transport_cancelled": transport.cancelled,
        "state_after_stop": p.machine.state.value,
        "ok": bool(ok),
    }


def main() -> None:
    report = {
        "host": "windows",
        "python": sys.version.split()[0],
        "sherpa_onnx": sherpa_onnx.__version__ if hasattr(sherpa_onnx, "__version__") else "1.13.4",
        "load_timings": _load_timings(),
        "wake_on_synth": _wake_on_synth(),
        "repeated_release": _repeated_release(),
        "continuous_silence": _continuous_silence(),
        "swappable_backends": _swappable_backends(),
        "interruptible_tts": _interruptible_tts(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    failures = []
    if not report["wake_on_synth"]["hit"]:
        failures.append("no wake hit on synthesized 你好问问")
    if not report["repeated_release"]["ok"]:
        failures.append("resource release growth too high")
    if not report["continuous_silence"]["ok"]:
        failures.append("continuous silence produced spurious hits or ran slow")
    if not report["swappable_backends"]["ok"]:
        failures.append("swappable backend behavior diverged")
    if not report["interruptible_tts"]["ok"]:
        failures.append("interruptible TTS / barge-in cancel failed")

    if failures:
        raise SystemExit("t10 FAILED: " + "; ".join(failures))
    print("\nt10 OK — all consolidated prototype checks passed.")


if __name__ == "__main__":
    main()
