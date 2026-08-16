"""REAL-MIC acceptance: wake, speak, dispatch, answer -- the whole loop.

This is the script that turns every AUTO/SIM claim in the voice path into a real
one, and it is the only place the four devices meet: microphone, KWS model,
streaming recognizer and speaker. It **needs you in the room** -- that is why it
lives under scripts/acceptance/ and not under tests/.

What it proves when it works:

  1. the wake word fires on a live microphone (REAL-MIC),
  2. the voiceprint gate accepts you and nobody else (REAL-MIC),
  3. the follow-up sentence is transcribed locally (REAL-MIC),
  4. the transcription drives a real turn through the dispatcher,
  5. the answer is spoken back, and a second wake word cuts it off (barge-in).

Nothing here is asserted automatically. Print, listen, and record what you saw
in docs/research/prototype-results.md with the level it actually earned.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio import (
    SherpaKeywordProvider,
    SherpaStreamingAsrProvider,
    SherpaTtsProvider,
    SounddeviceWakeCapture,
    SpeakerVerificationProvider,
)
from evox_plugin.runtime import VoiceRuntime

KWS_DIR = ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
ASR_DIR = ROOT / "models" / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
TTS_DIR = ROOT / "models" / "vits-melo-tts-zh_en"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=None, help="input device index or name")
    parser.add_argument("--seconds", type=float, default=120.0, help="how long to listen")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the voiceprint gate (anyone can wake it -- say so in the writeup)",
    )
    parser.add_argument("--silent", action="store_true", help="do not speak the answer")
    args = parser.parse_args()

    runtime = VoiceRuntime(speaker=None if args.no_gate else "owner", with_desktop=False)
    report = runtime.start()
    print(f"tools:  {sorted(report.tools)}")
    print(f"agents: {sorted(report.agents)}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    if not args.silent:
        tts = SherpaTtsProvider(TTS_DIR)
        status = tts.load()
        print(f"tts:    {status.available} ({status.details})")
        if status.available:
            runtime.plugin.attach_tts(tts)

    asr = SherpaStreamingAsrProvider(ASR_DIR)
    verifier = None if args.no_gate else SpeakerVerificationProvider.from_config()
    capture = SounddeviceWakeCapture(
        SherpaKeywordProvider(KWS_DIR),
        on_wake=lambda keyword, score: None,
        device=args.device,
        verifier=verifier,
        require_verification=not args.no_gate,
        asr_provider=asr,
    )
    runtime.attach_microphone(capture)

    try:
        capture.start()
    except Exception as exc:
        print(f"cannot open the microphone: {type(exc).__name__}: {exc}")
        runtime.close()
        return 1

    print("")
    print("Say the wake word, then your request. Ctrl+C to stop.")
    print("To test barge-in, say the wake word again while it is answering.")
    deadline = time.monotonic() + args.seconds
    turns = 0
    try:
        while time.monotonic() < deadline:
            result = runtime.pump(timeout=0.5)
            if result is None:
                continue
            turns += 1
            print(f"[{turns}] route={result.route} ok={result.ok} -> {result.text or result.reason}")
    except KeyboardInterrupt:
        print("stopped")
    finally:
        capture.stop()
        runtime.close()

    print("")
    print(f"turns: {turns}")
    print("Record what you heard, at the level it earned: REAL-MIC for the wake,")
    print("the gate and the transcription; REAL-AGENT only if a real agent answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

