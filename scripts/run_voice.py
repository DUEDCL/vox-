"""Say the wake word, then talk. This is the voice entry point.

``run_desktop.py`` reads typed lines; this reads a microphone. It assembles the
voice stack from ``config/voice.toml`` (see ``vox_plugin/voice_stack.py``), points
it at a ``VoiceRuntime``, and runs turns until you stop it.

    .venv\\Scripts\\python.exe scripts/run_voice.py            # 全链路
    .venv\\Scripts\\python.exe scripts/run_voice.py --check    # 只看还缺什么
    .venv\\Scripts\\python.exe scripts/run_voice.py --silent   # 不出声

The audio callback only enqueues recognised text; the turn runs on this thread
(``runtime.pump``). Running a dispatch plus TTS playback inside the callback holds
the audio device and drops frames, which looks like a recognizer that mishears
rather than a hang.

Evidence level: the wiring is AUTO (``tests/test_voice_stack.py``). A turn that
actually starts from your voice is REAL-MIC and needs you in the room.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio import load_voice_config
from core.env_file import load_env_file
from vox_plugin.runtime import VoiceRuntime
from vox_plugin.voice_stack import open_voice_stack


def print_readiness(stack) -> bool:
    """Print the checklist. Returns whether everything required is ready."""
    print("")
    print("就绪清单：")
    blocking = False
    for row in stack.readiness():
        mark = "  ok " if row["ready"] else "  -- "
        print(f"{mark}{row['item']:<8} {row['detail']}")
        if row["hint"]:
            print(f"          {row['hint']}")
        if not row["ready"] and row["item"] in {"wake", "speaker"}:
            blocking = True
    return not blocking


def main() -> int:
    parser = argparse.ArgumentParser(description="Vox voice entry point")
    parser.add_argument("--device", default=None, help="input device index or name")
    parser.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds (0 = forever)")
    parser.add_argument("--silent", action="store_true", help="do not speak the answers")
    parser.add_argument("--no-orb", action="store_true", help="run headless, no wake orb")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="turn the voiceprint gate off -- anyone can wake it, debugging only",
    )
    parser.add_argument("--check", action="store_true", help="print the checklist and exit")
    args = parser.parse_args()

    # 密钥只从环境变量读，.env 是给这一个进程树补变量的地方。打印名字不打印值。
    loaded = load_env_file()
    if loaded:
        print(f"env: 从 .env 读到 {', '.join(loaded)}")

    config = load_voice_config()
    stack = open_voice_stack(
        config,
        require_verification=False if args.no_gate else None,
        with_tts=False if args.silent else None,
        device=args.device,
    )
    for warning in stack.warnings:
        print(f"warning: {warning}")

    ready = print_readiness(stack)
    if args.check:
        stack.close()
        return 0 if ready else 1
    if not ready:
        print("")
        print("必需项没就绪，先按上面的提示补齐（或用 --check 反复确认）。")
        stack.close()
        return 1

    with_desktop = bool(config["orb.enabled"]) and not args.no_orb
    runtime = VoiceRuntime(with_desktop=with_desktop, visible=bool(config["orb.visible"]))
    report = runtime.start()
    print("")
    print(f"orb:    {report.desktop}")
    print(f"tools:  {', '.join(report.tools) or '(none)'}")
    print(f"agents: {', '.join(report.agents) or '(none)'}")
    print(f"memory: {report.memory}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    if stack.tts is not None:
        status = stack.tts.load()
        if status.available:
            runtime.plugin.attach_tts(stack.tts)
        else:
            print(f"warning: tts did not load: {status.details.get('reason')}")

    runtime.attach_microphone(stack.capture)
    try:
        stack.capture.start()
    except Exception as exc:  # noqa: BLE001 - a closed device is the normal failure
        print(f"cannot open the microphone: {type(exc).__name__}: {exc}")
        runtime.close()
        stack.close()
        return 1

    print("")
    print("说唤醒词，然后说你的请求。Ctrl+C 停止。")
    print("回复播放中再说一次唤醒词可以打断它。")
    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None
    turns = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            result = runtime.pump(timeout=0.5)
            if result is None:
                continue
            turns += 1
            print(f"[{turns}] route={result.route} ok={result.ok} -> {result.text or result.reason}")
    except KeyboardInterrupt:
        print("stopped")
    finally:
        stack.capture.stop()
        runtime.close()
        stack.close()

    print("")
    print(f"turns: {turns}")
    print("把实测数字按它真正挣到的等级写进 docs/research/prototype-results.md。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
