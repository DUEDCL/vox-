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

The stack comes from ``vox_plugin/voice_stack.py`` -- the same assembly
``scripts/run_voice.py`` uses -- so model paths live in one place and the verified
speaker arrives from the gate rather than from a constant. This script used to
pass ``speaker="owner"``, which meant ``shell.run``'s one credential was a string
literal even while the gate was running.

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

from core.audio import load_voice_config
from vox_plugin.runtime import VoiceRuntime
from vox_plugin.voice_stack import open_voice_stack


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

    config = load_voice_config()
    stack = open_voice_stack(
        config,
        require_verification=False if args.no_gate else None,
        with_tts=False if args.silent else None,
        device=args.device,
    )
    for warning in stack.warnings:
        print(f"warning: {warning}")
    for row in stack.readiness():
        print(f"{'ok  ' if row['ready'] else '--  '}{row['item']:<8} {row['detail']}")

    runtime = VoiceRuntime(with_desktop=False)
    report = runtime.start()
    print(f"tools:  {sorted(report.tools)}")
    print(f"agents: {sorted(report.agents)}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    if stack.tts is not None:
        status = stack.tts.load()
        print(f"tts:    {status.available} ({status.details})")
        if status.available:
            runtime.plugin.attach_tts(stack.tts)

    runtime.attach_microphone(stack.capture)

    try:
        stack.capture.start()
    except Exception as exc:
        print(f"cannot open the microphone: {type(exc).__name__}: {exc}")
        runtime.close()
        stack.close()
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
            # ``verified_by`` is the gate's own answer, printed so the writeup can
            # state whether the turn ran as an authorised speaker or as nobody.
            print(
                f"[{turns}] route={result.route} ok={result.ok} "
                f"verified_by={runtime.effective_speaker!r} "
                f"-> {result.text or result.reason}"
            )
    except KeyboardInterrupt:
        print("stopped")
    finally:
        stack.capture.stop()
        runtime.close()
        stack.close()

    print("")
    print(f"turns: {turns}")
    print("Record what you heard, at the level it earned: REAL-MIC for the wake,")
    print("the gate and the transcription; REAL-AGENT only if a real agent answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
