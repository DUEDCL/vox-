"""真机验收：对着麦克风说话，量整条链的每一段。

## 为什么需要一个专门的脚本

在它之前每个唤醒率数字都是 **SIM**（音频从 wav 直接喂进回调）或者
**REAL-MIC(loopback)**（扬声器放录音、麦克风收 —— 空气是真的，说话人不是）。两者都不能
回答目标指标：**Wake → 第一声回答**。那个数字要一个活人说话才量得出来。

这个脚本把「需要你在场的那一步」压到最短：跑起来、按提示说 N 轮、读最后那张表。
它不问任何问题，也不需要你记住任何命令。

## 它量的每一段

    说唤醒词
      │  ← KWS 命中          （麦克风 → VAD → 增益 → 关键词识别）
      │  ← 声纹接受/拒绝      （3 秒环形缓冲比对）
      │  ← 应答音 + 球弹出
    说请求
      │  ← ASR 端点           （最后一个字之后 rule2 秒）
      │  ← 转写文本
      │  ← 派发开始
      │  ← agent 首个 chunk   （task.progress 的 first_chunk_ms）
      │  ← TTS 第一块音频到手
      └→ 第一声              ← **目标指标**

## 用法

    PYTHONUTF8=1 .venv\\Scripts\\python.exe scripts\\acceptance\\real_mic_e2e.py [--rounds 5]

等级：**REAL-MIC**（真人 + 真麦克风 + 真空气）+ **REAL-AGENT**（真实端点完成一轮）。
音频只在内存里，不落盘、不上传 —— 和生产同一条路。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio.acks import AckLibrary, parse_acks
from core.audio.config import ACK_CACHE_DIR, describe_device, load_voice_config, repo_root
from core.env_file import load_env_file

load_env_file()

from vox_plugin.runtime import VoiceRuntime
from vox_plugin.voice_stack import open_voice_stack


class Marks:
    """一轮里每个节点的时刻。线程安全靠「只写一次」：每个键 setdefault。"""

    def __init__(self) -> None:
        self._at: dict[str, float] = {}
        self.text = ""
        self.answer = ""
        self.score: float | None = None
        self.rejected: str | None = None

    def mark(self, name: str) -> None:
        self._at.setdefault(name, time.monotonic())

    def get(self, name: str) -> float | None:
        return self._at.get(name)

    def span(self, start: str, end: str) -> int | None:
        a, b = self.get(start), self.get(end)
        return None if a is None or b is None else int((b - a) * 1000)

    def clear(self) -> None:
        self._at.clear()
        self.text = ""
        self.answer = ""
        self.score = None
        self.rejected = None


def cell(value: int | None) -> str:
    return "—" if value is None else f"{value}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5, help="说几轮（默认 5）")
    parser.add_argument("--wait", type=float, default=45.0, help="每轮最多等多少秒")
    parser.add_argument("--device", default=None, help="输入设备；默认按 voice.toml")
    args = parser.parse_args()

    config = load_voice_config()
    # **不在这里预解析设备。** `device=` 是「调用方点名」的入口，它会跳过
    # `open_voice_stack` 里那条「名字解析不到就退到系统默认」的处理 —— 而那条处理正是
    # 蓝牙耳机断开时唯一让麦克风还能开起来的路。只有 `--device` 显式给了才走点名那条。
    stack = open_voice_stack(
        config, device=int(args.device) if args.device is not None else None
    )
    device = stack.capture.device if stack.capture is not None else None

    print("=== 就绪清单 ===")
    for row in stack.readiness():
        print(f"  {'OK' if row['ready'] else '!!'} {row['item']:<8} {row['detail']} {row['hint']}")
    for warning in stack.warnings:
        print(f"  warning: {warning}")
    print(f"  输入设备：{describe_device(device)}")
    if stack.capture is None or stack.kws is None:
        print("\n唤醒装不起来，验收无从谈起。先按上面的提示补齐。")
        stack.close()
        return 1
    if stack.asr is None:
        print("\n没有识别模型：能量到唤醒为止，转写与之后的段量不到。")

    marks = Marks()
    runtime = VoiceRuntime(with_desktop=bool(config["orb.enabled"]), visible=bool(config["orb.visible"]))
    report = runtime.start()
    print(f"\n=== 运行时 ===\n  agents: {', '.join(report.agents) or '(none)'}  球: {report.desktop}")
    for warning in report.warnings:
        print(f"  warning: {warning}")

    # agent 首个 chunk 的时刻从事件流里取 —— `task.progress` 带的就是它。
    sink = runtime.on_event

    def on_event(event):
        if event.get("type") == "task.progress":
            marks.mark("first_chunk")
        return sink(event)

    runtime.on_event = on_event
    runtime.plugin.on_event = on_event

    if stack.tts is not None and stack.tts.load().available:
        inner = stack.tts._player()

        class Timed:
            def play(self, samples, sample_rate):
                marks.mark("first_audio")
                return inner.play(samples, sample_rate)

            def stop(self):
                return inner.stop()

        stack.tts.playback = Timed()
        runtime.plugin.attach_tts(stack.tts)
        acks = AckLibrary(
            parse_acks(config["wake.acks"]), tts=stack.tts, cache_dir=repo_root() / ACK_CACHE_DIR
        )
        if acks.texts:
            runtime.attach_acks(acks)

    capture = stack.capture
    kws_sink, wake_sink, reject_sink = capture.on_kws_hit, capture.on_wake, capture.on_reject
    runtime.attach_microphone(capture)
    inner_kws, inner_wake, inner_reject = capture.on_kws_hit, capture.on_wake, capture.on_reject
    inner_recognized = capture.on_recognized
    del kws_sink, wake_sink, reject_sink

    # **每一次尝试单独记一条。** 只记「这一轮的第一次」会把重试混进耗时里：
    # 2026-09-02 那次验收的「KWS→声纹 7719 ms」就是这么来的 —— 第一次被声纹拒了，
    # 人又说了一遍，而两次被算成了同一次的耗时。
    attempts: list[dict] = []

    def window_now() -> dict:
        """声纹此刻看到的那段音频长什么样。只有统计量，没有音频、没有向量。"""
        try:
            if stack.verifier is None:
                return {}
            snapshot = capture.snapshot(capture.verify_seconds)
            return stack.verifier.input_quality(snapshot, capture.sample_rate)
        except Exception:  # noqa: BLE001 - 诊断不能自己炸
            return {}

    def on_kws(keyword):
        marks.mark("kws")
        attempts.append({"what": "kws", "at": time.monotonic()})
        return inner_kws(keyword)

    def on_wake(keyword, score=None):
        marks.mark("wake")
        marks.score = score
        attempts.append({"what": "accept", "at": time.monotonic(), "score": score, **window_now()})
        return inner_wake(keyword, score)

    def on_reject(keyword, reason="", score=0.0):
        marks.mark("reject")
        marks.rejected = f"{reason}（相似度 {score:.3f}）"
        attempts.append({"what": "reject", "at": time.monotonic(), "score": score,
                          "reason": reason, **window_now()})
        return inner_reject(keyword, reason, score)

    def on_recognized(text):
        marks.mark("final")
        marks.text = text
        return inner_recognized(text)

    capture.on_kws_hit = on_kws
    capture.on_wake = on_wake
    capture.on_reject = on_reject
    capture.on_recognized = on_recognized

    rows: list[tuple[int, Marks]] = []
    kws_hits = accepted = rejected = answered = 0

    print(
        "\n=== 开始 ===\n"
        f"  每轮：先说唤醒词「你好小沃」，听到应答音之后说一句话（比如「现在几点」）。\n"
        f"  一共 {args.rounds} 轮。说完等它回答，别抢。\n"
    )
    capture.start()
    try:
        for index in range(1, args.rounds + 1):
            marks.clear()
            print(f"--- 第 {index} 轮：请说「你好小沃」", flush=True)
            started = time.monotonic()
            result = None
            while time.monotonic() - started < args.wait:
                result = runtime.pump(timeout=0.5)
                if result is not None:
                    break
            if marks.get("kws"):
                kws_hits += 1
            if marks.get("wake"):
                accepted += 1
            if marks.get("reject"):
                rejected += 1
            if result is not None:
                answered += 1
                marks.answer = result.text
            snapshot = Marks()
            snapshot.__dict__.update({k: dict(v) if isinstance(v, dict) else v for k, v in marks.__dict__.items()})
            rows.append((index, snapshot))
            if marks.rejected:
                print(f"    声纹拒绝：{marks.rejected}")
            elif result is None:
                print("    这一轮没有走完（超时）")
            else:
                print(
                    f"    转写：{marks.text!r}"
                )
                print(
                    f"    路由：{result.route} ok={result.ok} "
                    f"{chr(47).join(result.agents) or chr(45)} {result.reason[:60]}"
                )
                print(f"    回答：{result.text[:90] or chr(40) + chr(31354) + chr(41)}")
    except KeyboardInterrupt:
        print("\n（中断）")
    finally:
        capture.stop()
        runtime.close()
        stack.close()

    print("\n=== 每一段的耗时（毫秒）===")
    header = ("轮", "KWS→声纹", "声纹→转写", "转写→首chunk", "首chunk→第一声", "**唤醒→第一声**")
    print("  " + " ".join(f"{name:>14}" for name in header))
    for index, m in rows:
        print(
            "  "
            + " ".join(
                f"{value:>14}"
                for value in (
                    index,
                    cell(m.span("kws", "wake")),
                    cell(m.span("wake", "final")),
                    cell(m.span("final", "first_chunk")),
                    cell(m.span("first_chunk", "first_audio")),
                    cell(m.span("kws", "first_audio")),
                )
            )
        )

    if attempts:
        print("")
        print("=== 每一次唤醒尝试（声纹看到的那段音频）===")
        print(f"  {'判定':<8}{'相似度':>9}{'窗长s':>8}{'有声':>7}{'语音s':>8}{'rms':>9}{'峰值':>8}  原因")
        for record in attempts:
            voiced = record.get("seconds", 0.0) * record.get("active", 0.0)
            score = record.get("score")
            shown = "—" if score is None else f"{score:.3f}"
            print(
                f"  {record['what']:<8}{shown:>9}"
                f"{record.get('seconds', 0.0):>8.2f}"
                f"{record.get('active', 0.0):>7.0%}"
                f"{voiced:>8.2f}"
                f"{record.get('rms', 0.0):>9.4f}"
                f"{record.get('peak', 0.0):>8.3f}"
                f"  {str(record.get('reason', ''))[:78]}"
            )
    print("\n=== 汇总 ===")
    print(f"  唤醒命中率：KWS {kws_hits}/{args.rounds}  声纹接受 {accepted}  拒绝 {rejected}")
    print(f"  走完整轮：{answered}/{args.rounds}")
    finals = [m.span("kws", "first_audio") for _i, m in rows if m.span("kws", "first_audio")]
    if finals:
        finals.sort()
        print(
            f"  唤醒→第一声：中位 {finals[len(finals) // 2]} ms  最快 {finals[0]}  最慢 {finals[-1]}"
        )
    scores = [m.score for _i, m in rows if m.score is not None]
    if scores:
        print(f"  声纹相似度：{', '.join(f'{s:.3f}' for s in scores)}")
    if capture.auto_gain is not None:
        print(f"  增益读数：{capture.auto_gain.describe()}")
    print(f"  唤醒漏斗：{runtime.wake_stats}")
    print("\n把这张表贴回来就是 REAL-MIC 验收证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
