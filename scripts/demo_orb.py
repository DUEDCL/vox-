"""完整演示唤醒球的全部形态 —— 走真窗口 + 真事件通道，不是浏览器预览。

这个脚本 spawn 的是 `desktop/src-tauri/target/debug/vox.exe`（透明、无边框、置顶、
skip_taskbar、带托盘的真窗口），事件走 `core/desktop_bridge.py` 的父子进程管道，
前端用的是 `applyEnvelope()` 那条真分派。所以它覆盖的是 REAL-WIN 路径而不是 SIM。

**改完前端必须先 `npm run build` 再 `cargo build`** —— Tauri 把 `dist/` 在编译期嵌进
二进制，只跑 `npm run build` 的话这里起的还是旧界面。

演示顺序覆盖八种呈现 + 三条只在切换时存在的动效：

    ①  唤醒          wake.detected → 从一个点 350ms 铺张（窗口此前是隐藏的）
    ②  聆听          呼吸最深(0.20)、轮廓 ±6% 胀缩、片体向内吸、白核最亮
    ③  思考 1 路     6 片弧形透镜片散成一圈、角距恒 60°、青→磁沿环渐变、公转 3.8s/圈
    ④  思考 3 路     blobCount=3（只进指纹，界面上不再用角距编码路数）
    ⑤  回复          片体向外吐到中段、每句 tts.chunk 一次 8% 吐纳、文字逐字流出
    ⑥  闸门          确认卡 + 形态冻住但亮度仍在呼吸；强制显示、打断自动收回
    ⑦  异常          单侧拉扣(skew 0.22) + 心跳漏拍(每 6 秒压到 0.12 倍)
    ⑧  取消          散开、无白热核、仍在呼吸但最浅(0.035)
    ⑨  收摊          回 idle → 停 3 秒 → 700ms 收回成一个点 → 隐藏窗口

用法：

    .venv\\Scripts\\python.exe scripts\\demo_orb.py                 # 完整跑一遍
    .venv\\Scripts\\python.exe scripts\\demo_orb.py --hold 5        # 每段停 5 秒
    .venv\\Scripts\\python.exe scripts\\demo_orb.py --wait 6        # 开跑前停 6 秒（给录屏挂载）
    .venv\\Scripts\\python.exe scripts\\demo_orb.py --only 3 5      # 只跑第 3 与第 5 段
    VOX_ORB_SIZE=160 .venv\\Scripts\\python.exe scripts\\demo_orb.py  # 换个尺寸试

抓画面：球是 layered 窗口，**只能用 PrintWindow**（`.vox-ref/shoot.ps1`）。
`ffmpeg -f gdigrab` 会跳过 layered 窗口，抓到的是球背后的桌面。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.desktop_bridge import DesktopBridge, find_desktop_binary
from core.events import build_event

TURN = "demo-turn-1"


def state(bridge: DesktopBridge, to: str, *, why: str = "demo") -> None:
    """切态。`state.changed` 的 payload 里前端只读 `to`，其余键是给日志看的。"""
    bridge.send(build_event("state.changed", {"to": to, "from": "", "reason": why}))


def say_sentences(bridge: DesktopBridge, sentences: list[str], gap: float) -> None:
    """逐句说：每句先把字逐块流出（llm.delta），再报一条 tts.chunk 触发吐纳。

    真机上这两者由不同环节产出（LLM 流式 vs TTS 合成完一句），这里保持同样的次序：
    文字先到、声音后到，所以球的吐纳落在句尾而不是句首。
    """
    for i, sentence in enumerate(sentences):
        for k in range(0, len(sentence), 4):
            bridge.send(build_event("llm.delta", {"text": sentence[k:k + 4], "turn_id": TURN}))
            time.sleep(0.06)
        bridge.send(build_event("tts.chunk", {"index": i, "turn_id": TURN}))
        time.sleep(gap)


# 九段演示。每段 = (编号, 标题, 跑法)。分成小函数是为了 --only 能挑着跑。
def seg_wake(b: DesktopBridge, h: float) -> None:
    b.send(build_event("wake.detected", {"keyword": "vox", "score": 0.91}))
    state(b, "listening", why="wake")
    time.sleep(max(h, 2.0))


def seg_listening(b: DesktopBridge, h: float) -> None:
    state(b, "listening")
    time.sleep(max(h, 4.5))          # 至少一个完整呼吸周期（4s）才看得出胀缩
    b.send(build_event("asr.final", {"text": "读一下 README", "turn_id": TURN}))


def seg_think1(b: DesktopBridge, h: float) -> None:
    state(b, "thinking")
    b.send(build_event("turn.started", {"turn_id": TURN}))
    time.sleep(max(h, 8.0))          # 公转 3.8s/圈，至少走两圈


def seg_think3(b: DesktopBridge, h: float) -> None:
    b.send(build_event("task.progress", {"agents": ["claude", "codex", "opencode"]}))
    time.sleep(max(h, 8.0))


def seg_speaking(b: DesktopBridge, h: float) -> None:
    state(b, "speaking")
    say_sentences(b, [
        "已经读完 README.md 了，一共 42 行。",
        "它讲的是这个项目怎么起手：先建虚拟环境，再下模型。",
        "要我把起手那几条命令念一遍吗？",
    ], gap=max(0.9, h * 0.35))
    b.send(build_event("turn.done", {"turn_id": TURN, "ok": True}))
    time.sleep(h * 0.5)


def seg_gated(b: DesktopBridge, h: float) -> None:
    ev = build_event("tool.confirm_required", {
        "tool": "shell.run",
        "command": "git push --force origin main",
        "origin": "voice",
    })
    approved = b.await_confirmation(ev, timeout_s=max(8.0, h * 3))
    print(f"      确认结果：{approved}（没点就是超时落定为拒绝 —— fail-closed）")


def seg_error(b: DesktopBridge, h: float) -> None:
    state(b, "error")
    time.sleep(max(h, 7.0))          # 漏拍每 6 秒一次，至少给它一次机会


def seg_cancelled(b: DesktopBridge, h: float) -> None:
    b.send(build_event("turn.cancelled", {"turn_id": TURN, "reason": "wake_interrupt"}))
    state(b, "cancelled")
    time.sleep(max(h, 4.5))


def seg_retract(b: DesktopBridge, h: float) -> None:
    state(b, "idle")
    print("      停 3 秒 → 700ms 收回成一个点 → 隐藏窗口")
    time.sleep(6.0)


SEGMENTS = [
    (1, "唤醒 —— 从一个点 350ms 铺张为聆听", seg_wake),
    (2, "聆听 —— 呼吸最深、轮廓 ±6% 胀缩、片体向内吸", seg_listening),
    (3, "思考 1 路 —— 6 片弧形透镜片成环、青→磁沿环渐变、公转 3.8s/圈", seg_think1),
    (4, "思考 3 路 —— 路数只进指纹，角距仍恒 60°", seg_think3),
    (5, "回复 —— 片体向外吐到中段 + 每句一次 8% 吐纳", seg_speaking),
    (6, "闸门 —— 确认卡 + 形态冻住但亮度仍在呼吸", seg_gated),
    (7, "异常 —— 单侧拉扣 + 心跳漏拍", seg_error),
    (8, "取消 —— 散开、无白热核、呼吸最浅", seg_cancelled),
    (9, "收摊 —— 回 idle 后收回成点并隐藏窗口", seg_retract),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="完整演示唤醒球的全部形态（真窗口）")
    ap.add_argument("--hold", type=float, default=3.0, help="每段的基础停留秒数（下限由各段自己定）")
    ap.add_argument("--wait", type=float, default=0.0, help="开跑前先停多少秒（给录屏挂载）")
    ap.add_argument("--only", type=int, nargs="+", metavar="N", help="只跑这几段（编号见 --list）")
    ap.add_argument("--list", action="store_true", help="列出所有段落后退出")
    args = ap.parse_args()

    if args.list:
        for n, title, _ in SEGMENTS:
            print(f"  {n}  {title}")
        return 0

    binary = find_desktop_binary(ROOT)
    if binary is None:
        print("找不到 vox.exe —— 先 `cd desktop && npm run build`，再 `cd src-tauri && cargo build`")
        return 1
    print(f"orb binary: {binary}")
    print("提示：改过前端就必须重跑 cargo build，dist 是编译期嵌进二进制的\n")

    bridge = DesktopBridge()
    bridge.start()
    if not bridge.ready.wait(20):
        print("窗口没报 ready（20s 超时）")
        bridge.close()
        return 1

    picked = set(args.only) if args.only else None
    try:
        if args.wait > 0:
            state(bridge, "idle")
            print(f"等 {args.wait:.0f}s 再开始")
            time.sleep(args.wait)

        for n, title, run in SEGMENTS:
            if picked is not None and n not in picked:
                continue
            print(f"  {n}  {title}")
            run(bridge, args.hold)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        d = bridge.describe()
        print(f"\n事件：发出 {d['sent']} 条，丢弃 {d['dropped']} 条，挂起的确认 {d['pending_confirmations']} 个")
        if d["dropped"]:
            print("  ⚠ 有事件被丢弃 —— 大概是 payload 不合契约，去看 core/events.py 的校验")
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
