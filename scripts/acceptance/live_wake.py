"""Real-time microphone wake-word test. Audio stays in memory, nothing is saved.

Wake only: no recognizer, no dispatch, no answer. Use this to measure hit rate
across the scenarios release blocker #1 asks for (quiet / far field / noise /
repeated), and ``live_conversation.py`` for the whole loop.

Two things changed here on 2026-08-28. The script used to build the capture with
neither a verifier nor ``require_verification=False``, and ``require_verification``
defaults to ``True`` -- so ``capture.start()`` refused with "speaker verification
is required but no verifier is attached" and the script could not run at all. It
now assembles through ``vox_plugin/voice_stack.py`` like every other entry point,
which also removes the hard-coded model directory.

The gate is **on** by default, because that is the configuration being shipped.
``--no-gate`` measures the KWS model alone; the JSON output records which one you
ran so a hit rate can never be quoted for the wrong configuration.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio import load_voice_config
from vox_plugin.voice_stack import open_voice_stack


def parse_device(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Live microphone wake test for 你好问问")
    parser.add_argument("--device", type=parse_device, default=None)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--threshold", type=float, default=None, help="override wake.keywords_threshold")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="measure the KWS model alone, with the voiceprint gate off",
    )
    parser.add_argument(
        "--all-hits",
        action="store_true",
        help="keep listening for the full duration instead of stopping at the first hit",
    )
    args = parser.parse_args()

    config = load_voice_config()
    if args.threshold is not None:
        config["wake.keywords_threshold"] = args.threshold

    stack = open_voice_stack(
        config,
        require_verification=False if args.no_gate else None,
        with_tts=False,
        with_asr=False,
        device=args.device,
    )
    for warning in stack.warnings:
        print(f"warning: {warning}", flush=True)

    hits: list[dict[str, float | str | None]] = []
    rejections: list[dict[str, float | str]] = []
    done = threading.Event()
    started_at = time.perf_counter()

    def on_wake(keyword: str, score: float | None) -> None:
        hits.append(
            {
                "keyword": keyword,
                "score": score,
                "at_seconds": round(time.perf_counter() - started_at, 3),
            }
        )
        print(f"WAKE HIT: {keyword!r} score={score} at {hits[-1]['at_seconds']}s", flush=True)
        if not args.all_hits:
            done.set()

    def on_reject(keyword: str, reason: str, score: float) -> None:
        rejections.append(
            {
                "keyword": keyword,
                "reason": reason,
                "score": round(score, 4),
                "at_seconds": round(time.perf_counter() - started_at, 3),
            }
        )
        print(f"WAKE REJECTED: {reason} score={score:.4f}", flush=True)

    stack.capture.on_wake = on_wake
    stack.capture.on_reject = on_reject

    try:
        stack.capture.start()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "listening": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "hit": False,
                    "audio_saved": False,
                    "resources_released": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        stack.close()
        return 1

    print(
        json.dumps(
            {
                "verification": "REAL_MICROPHONE_WAKE",
                "listening": True,
                "duration_seconds": args.duration,
                "threshold": config["wake.keywords_threshold"],
                "gate": "off" if args.no_gate else "on",
                "instruction": "请现在对着麦克风说「你好问问」，可重复多次。",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    done.wait(timeout=args.duration)
    stack.capture.stop()
    elapsed = time.perf_counter() - started_at
    stack.close()

    print(
        json.dumps(
            {
                "listening": False,
                "elapsed_seconds": round(elapsed, 3),
                "gate": "off" if args.no_gate else "on",
                "hits": hits,
                "rejections": rejections,
                "hit": len(hits) > 0,
                "callback_errors": stack.capture.callback_errors,
                "audio_saved": False,
                "resources_released": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
