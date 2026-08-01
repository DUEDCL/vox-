"""Interactive voiceprint enrollment (声纹录入).

Run this once, in person, before the platform can be woken::

    .venv\\Scripts\\python.exe scripts/enroll_speaker.py --name <你的名字>

``enroll`` is append-only, so a weak first attempt can be topped up later rather
than re-recorded from scratch.

Two things this script deliberately does not do: it never writes the recorded
audio anywhere (only the embedding vectors reach disk), and it never prints a
vector. The enrollment file is biometric data and is listed in ``.gitignore``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import ProviderUnavailable, SpeakerVerificationProvider, load_speaker_config

PROMPTS = [
    "你好问问，今天天气怎么样",
    "你好问问，帮我读一下项目说明",
    "你好问问，现在几点了",
    "你好问问，把刚才那段再说一遍",
    "你好问问，谢谢",
]


def record(sounddevice, numpy, seconds: float, sample_rate: int, device) -> object:
    """Capture one phrase straight into memory."""
    frames = int(seconds * sample_rate)
    buffer = sounddevice.rec(
        frames, samplerate=sample_rate, channels=1, dtype="float32", device=device
    )
    sounddevice.wait()
    return numpy.asarray(buffer, dtype="float32").reshape(-1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enroll a speaker for the wake gate")
    parser.add_argument("--name", required=True, help="speaker name, used only as a label")
    parser.add_argument("--samples", type=int, default=3, help="how many phrases to record")
    parser.add_argument("--seconds", type=float, default=3.0, help="seconds per phrase")
    parser.add_argument("--device", default=None, help="input device index or name")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument(
        "--list-only", action="store_true", help="print enrollment status and exit"
    )
    args = parser.parse_args(argv)

    config = load_speaker_config()
    provider = SpeakerVerificationProvider.from_config()

    if args.list_only:
        print(json.dumps(provider.describe(), ensure_ascii=False, indent=2))
        return 0

    status = provider.load()
    if not status.available:
        print(f"声纹模型不可用：{status.details['reason']}", file=sys.stderr)
        print(
            "下载命令见 docs/routines.md「模型下载与完整性检查」一节。",
            file=sys.stderr,
        )
        return 2

    try:
        import numpy
        import sounddevice
    except ImportError as exc:
        print(f"缺少音频依赖：{exc}", file=sys.stderr)
        return 2

    device = args.device
    if device is not None and str(device).isdigit():
        device = int(device)

    print(f"声纹录入：{args.name}")
    print(f"模型 dim={status.details['dim']}，阈值 {config['threshold']}")
    print(f"共 {args.samples} 段，每段 {args.seconds:g} 秒。音频只在内存中，不落盘。\n")

    chunks = []
    for index in range(args.samples):
        prompt = PROMPTS[index % len(PROMPTS)]
        input(f"[{index + 1}/{args.samples}] 准备好后按回车，然后朗读：{prompt}")
        print("  录音中…", end="", flush=True)
        started = time.perf_counter()
        chunk = record(sounddevice, numpy, args.seconds, args.sample_rate, device)
        elapsed = time.perf_counter() - started
        peak = float(numpy.abs(chunk).max()) if chunk.size else 0.0
        print(f" 完成 {elapsed:.2f}s，峰值 {peak:.4f}")
        if peak < 0.01:
            print("  这段几乎是静音，已跳过。检查麦克风或换一个 --device。")
            continue
        chunks.append(chunk)

    if not chunks:
        print("没有可用的录音，未写入任何内容。", file=sys.stderr)
        return 1

    try:
        result = provider.enroll(args.name, chunks, sample_rate=args.sample_rate)
    except (ProviderUnavailable, ValueError) as exc:
        print(f"录入失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"\n已写入 {result.samples_used} 个向量（{result.total_seconds:.2f}s，dim={result.dim}）。"
    )
    print("注册状态（只有名字与样本数，不含向量）：")
    print(json.dumps(provider.describe(), ensure_ascii=False, indent=2))
    print("\nenrollment/ 是生物特征，已在 .gitignore 内，永不提交。")
    print("通过率不理想时重复本命令追加样本，不必删掉重录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
