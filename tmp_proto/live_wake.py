"""Real-time microphone wake-word test. Audio stays in memory, nothing is saved."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.providers import SherpaKeywordProvider, SounddeviceWakeCapture


def parse_device(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Live microphone wake test for 你好问问")
    parser.add_argument("--device", type=parse_device, default=None)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--threshold", type=float, default=0.25)
    args = parser.parse_args()

    hits: list[dict[str, float | str]] = []
    done = threading.Event()
    started_at = time.perf_counter()

    def on_wake(keyword: str, score: float) -> None:
        hits.append({"keyword": keyword, "score": score, "at_seconds": round(time.perf_counter() - started_at, 3)})
        print(f"WAKE HIT: {keyword!r} at {hits[-1]['at_seconds']}s", flush=True)
        done.set()

    provider = SherpaKeywordProvider(
        ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
        keywords_threshold=args.threshold,
    )
    capture = SounddeviceWakeCapture(provider, on_wake, device=args.device)
    capture.start()
    print(json.dumps({
        "verification": "REAL_MICROPHONE_WAKE",
        "listening": True,
        "duration_seconds": args.duration,
        "threshold": args.threshold,
        "instruction": "请现在对着麦克风说「你好问问」，可重复多次。",
    }, ensure_ascii=False), flush=True)

    done.wait(timeout=args.duration)
    capture.stop()

    elapsed = time.perf_counter() - started_at
    print(json.dumps({
        "listening": False,
        "elapsed_seconds": round(elapsed, 3),
        "hits": hits,
        "hit": len(hits) > 0,
        "audio_saved": False,
        "resources_released": True,
    }, ensure_ascii=False, indent=2))
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
