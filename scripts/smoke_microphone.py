"""Local microphone + Silero VAD smoke test. Audio stays in memory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.providers import ProviderUnavailable, SherpaVadProvider


def parse_device(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture local microphone audio without saving it")
    parser.add_argument("--device", type=parse_device, default=None)
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--vad-model", default="models/silero_vad.onnx")
    args = parser.parse_args()

    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise ProviderUnavailable("install requirements-voice.txt in .venv") from exc

    frames = int(args.duration * args.sample_rate)
    started = time.perf_counter()
    audio = sd.rec(
        frames,
        samplerate=args.sample_rate,
        channels=1,
        dtype="float32",
        device=args.device,
        blocking=True,
    )[:, 0]
    capture_seconds = time.perf_counter() - started
    rms = float(np.sqrt(np.mean(audio * audio)))
    peak = float(np.max(np.abs(audio)))

    vad = SherpaVadProvider(ROOT / args.vad_model, sample_rate=args.sample_rate)
    status = vad.load()
    if not status.available:
        raise ProviderUnavailable(status.details["reason"])
    segments: list[dict[str, int]] = []
    for offset in range(0, len(audio), 512):
        segments.extend(vad.feed(audio[offset : offset + 512])["segments"])
    segments.extend(vad.flush()["segments"])
    vad.close()
    del audio

    print(json.dumps({
        "verification": "REAL_MICROPHONE_LOCAL",
        "device": args.device,
        "requested_seconds": args.duration,
        "capture_seconds": round(capture_seconds, 3),
        "rms": round(rms, 6),
        "peak": round(peak, 6),
        "vad_segments": segments,
        "audio_saved": False,
        "resources_released": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
