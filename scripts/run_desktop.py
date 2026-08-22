"""Command line for ``VoiceRuntime``: one turn in, one answer out, orb watching.

This is the entry point ``vox_plugin/runtime.py`` names as its command line. It
assembles the plugin, dispatcher, tools, memory and the wake orb, then drives
turns. With no arguments it reads lines from stdin interactively; with arguments
it runs one turn and exits. Headless is a supported mode: when the orb is not
built the runtime reports it and keeps answering.

The behaviour this drives is covered by ``tests/test_runtime.py`` (AUTO, fake
dispatcher). Real turns need a built orb (REAL-WIN) and a real agent
(REAL-AGENT).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vox_plugin.runtime import VoiceRuntime


def main() -> int:
    runtime = VoiceRuntime()
    report = runtime.start()
    print("Vox runtime")
    print(f"  desktop: {report.desktop}")
    print(f"  tools:   {', '.join(report.tools) or '(none)'}")
    print(f"  agents:  {', '.join(report.agents) or '(none)'}")
    print(f"  memory:  {report.memory}")
    for warning in report.warnings:
        print(f"  warning: {warning}")

    try:
        if len(sys.argv) > 1:
            text = " ".join(sys.argv[1:])
            print(f"> {text}")
            result = runtime.say(text)
            print(result.text or result.reason)
            return 0 if (result.ok or result.needs_confirmation) else 1
        print("Say something, or Ctrl+C to stop.")
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            if text in {"quit", "exit", "退出"}:
                break
            result = runtime.say(text)
            print(f"> {result.text or result.reason}")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

