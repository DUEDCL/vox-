"""从 Vox 自己的麦克风注册声纹 —— 而不是从浏览器。

## 为什么需要这个脚本

控制台的注册按钮走浏览器 `getUserMedia`：设备由浏览器/系统挑，采样率由浏览器决定，
增益还可能被浏览器的自动增益控制动过。而 Vox 采集走的是 `config/voice.toml` 里
`[input] device` 指定的那个设备、固定 16 kHz、没有任何自动增益。

**两条路不同 = 注册和校验的信道不同 = 相似度上不去。** 2026-08-29 的实机日志：唤醒词
KWS 命中 16/16（那一层是好的），声纹全拒，分数落在 0.339–0.484，阈值 0.5。而这个模型
自己测试集上的分布是**同人 0.736 / 不同人 0.370** —— 0.34–0.48 正好是「不同人」那一段。
也就是说档案里那个声音，和麦克风里进来的这个声音，在模型看来不是一个人。

这个脚本让两条路变成同一条：**用 Vox 采集用的那个设备录，录完当场再录一段验一下**，
把「注册好了但唤不醒」这件事在注册时就暴露出来，而不是留到对着麦克风喊的时候。

用法：

    .venv\\Scripts\\python.exe scripts/enroll_speaker.py 你的名字

    --replace   先删掉同名的旧档案（默认是追加，追加语义见 provider.enroll）
    --samples N 录几段（默认 6，六条句子各不相同）
    --seconds S 每段多长（默认 3.0）

## 为什么默认 5 段，而且后两段要求换距离

**2026-08-30 实测（真实人声 + 真的 CAM++ 模型，见 `.vox-ref/probe_speaker_conditions.py`）**：

| 注册段数 | 相似度 |
|---|---|
| 1 段 | 0.706 |
| 2 段 | 0.772 |
| **3 段** | **0.794** |

sherpa 的 `SpeakerEmbeddingManager.add(name, [v1, v2])` 是**求质心**（实测：两条正交向量注册
在一个名字下，各自只得 0.7071 = 1/√2），所以多录几段等于把每次说话的随机偏差平均掉 ——
段数越多档案越稳，这是**可测的**，不是安慰。

距离那一条同理但更重要：只用近场注册时，加噪（SNR 6 dB，近似「离得远」）的校验只有
**0.607**；把一段远场也注册进同一个名字之后升到 **0.722**，而近场那一侧没有变差
（0.745 → 0.750）。这一条是 **SIM**（加白噪声不等于真远场，少了混响与方向性），真机结论
需要你在场时用「试一句」量。

所以：前几段用平时唤醒的距离，最后两段退开一点。要覆盖更多条件（戴耳机、另一只麦克风）
就**不加 `--replace` 再跑一次**，那是追加语义。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio.config import load_voice_config, resolve_device
from core.audio.enroll_prompts import DEFAULT_ROUNDS, rounds
from core.audio.speaker import SpeakerVerificationProvider, load_speaker_config

#: 削波比例上限（提示用，比 speaker.toml 的拒绝线更严）。
#:
#: `max_clip_ratio = 0.05` 是**拒绝**线：超过 5% 的样本贴在 ±1.0 才拒。但轻度削波虽然
#: 过得了这道门，仍然会把说话人特征削掉一部分 —— 而实机日志里每一次唤醒的块峰值都是
#: 1.000，说明这只麦克风的输入增益偏高。所以这里用一条更早的提示线：只要有样本到轨就说。
WARN_CLIP_RATIO = 0.002

#: 一段录音的**原始峰值**下限。低于它就拒绝这一段。
#:
#: 2026-08-31 实机的教训：`rms < 0.002` 这条线太低，挡不住「房间底噪」。那台机器五分钟内
#: 的原始峰值最大值是 **0.0587**（期间使用者在说话），而底噪 rms 约 0.01 —— 远高于 0.002，
#: 于是六段「录音」全部合格、注册出一个**房间的指纹**，再拿一段底噪去比它得 0.979「通过」。
#: 一道本该 fail-closed 的门就这样变成了 fail-open。
#:
#: 0.1 把两个实测状态分在两侧且余量都很宽：坏的时候 0.0587，早先能唤醒时块峰值 1.000。
#: 和 `core/console/routes.py` 的 `LIVE_MIN_PEAK` 是同一条线、同一个出处。
MIN_PEAK = 0.10


def _record(seconds: float, device, sample_rate: int = 16000):
    """录一段并报它的形状。返回 (samples, 诊断字典)。"""
    import numpy as np
    import sounddevice as sd

    frames = int(seconds * sample_rate)
    buffer = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32", device=device)
    sd.wait()
    samples = np.asarray(buffer, dtype="float32").reshape(-1)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    clipped = float(np.mean(np.abs(samples) >= 0.999)) if samples.size else 0.0
    return samples, {"peak": peak, "rms": rms, "clip": clipped}


def _verdict(stats: dict) -> str:
    """一段录音能不能用。这些判据和 provider 的质量门同源，但更早、更啰嗦。"""
    if stats["peak"] < 1e-4:
        return "**全零** —— 这个设备没在出声，换设备或到 Windows 声音设置里取消静音"
    if stats["peak"] < MIN_PEAK:
        return (
            f"**太轻**（峰值 {stats['peak']:.4f} < {MIN_PEAK}）—— 这个量级和「麦克风没在收音」"
            "区分不开。把 Windows 输入音量调上去，说话时峰值该在 0.2–0.7。"
            "\n     不修就注册的话，档案录到的是**房间底噪**而不是你的声音 ——"
            "\n     然后一段静音去比它会得 0.98「通过」，那道门就等于没有了"
        )
    if stats["rms"] < 0.002:
        return "**太轻** —— 说大声点，或者把输入音量调上去（低于声纹门的 min_rms）"
    if stats["clip"] > 0.05:
        return "**削波超标** —— 输入增益太高，声纹门会直接拒这一段"
    if stats["clip"] > WARN_CLIP_RATIO:
        return "偏响（有样本贴到轨了）—— 能用，但把输入增益降一点会让相似度更高"
    return "好"


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Vox 自己的麦克风注册声纹")
    parser.add_argument("name", help="说话人名字（唤醒时的已验身份就是它）")
    parser.add_argument(
        "--samples", type=int, default=DEFAULT_ROUNDS,
        help=f"录几段（默认 {DEFAULT_ROUNDS}，见 core/audio/enroll_prompts.py 的实测）",
    )
    parser.add_argument("--seconds", type=float, default=3.0, help="每段几秒（默认 3.0）")
    parser.add_argument("--replace", action="store_true", help="先删掉同名旧档案")
    args = parser.parse_args()

    config = load_voice_config()
    device = resolve_device(config)
    rate = int(config["input.sample_rate"])
    provider = SpeakerVerificationProvider.from_config()

    print("=" * 70)
    print("从 Vox 采集用的同一个设备注册声纹")
    print("=" * 70)
    print(f"设备      : {device if device is not None else '(系统默认)'}   ← 和 config/voice.toml 的 [input] device 一致")
    print(f"采样率    : {rate}")
    info = provider.describe()
    print(f"模型      : {'有' if info.get('available') else '**缺**'}   阈值 {info.get('threshold')}")
    print(f"已有档案  : {info.get('speakers')}  {info.get('samples_per_speaker', {})}")
    if not info.get("available"):
        print("声纹模型不在，注册不了。停。")
        return 1
    print()
    return _run(provider, args, device, rate)


def _run(provider, args, device, rate) -> int:
    if args.replace and provider.remove(args.name):
        print(f"已删掉旧档案 {args.name!r}")
        print()

    chunks = []
    plan = rounds(args.samples)
    for index, (condition, prompt) in enumerate(plan):
        print(f"第 {index + 1}/{args.samples} 段 —— {condition}，请念：「{prompt}」")
        for count in (3, 2, 1):
            print(f"  {count}...", end="", flush=True)
            time.sleep(0.7)
        print(f" 开始（{args.seconds:.0f} 秒）", flush=True)
        samples, stats = _record(args.seconds, device, rate)
        verdict = _verdict(stats)
        print(
            f"  peak={stats['peak']:.3f} rms={stats['rms']:.4f} "
            f"到轨比例={stats['clip']:.2%}  {verdict}"
        )
        if verdict.startswith("**"):
            print("  这一段不合格，重录这一段。")
            print()
            continue
        chunks.append(samples)
        print()

    if len(chunks) < args.samples:
        print(f"只拿到 {len(chunks)}/{args.samples} 段合格录音。先把上面提示的问题解决再来。")
        return 1

    try:
        result = provider.enroll(args.name, chunks, sample_rate=rate)
    except Exception as exc:  # noqa: BLE001 - 脚本要把原因说清楚而不是抛栈
        print(f"注册失败：{type(exc).__name__}: {exc}")
        return 1
    print(f"注册完成：{args.name}  本次 {result.samples_used} 段 / {result.total_seconds:.1f}s  维度 {result.dim}")
    print()

    # --- 当场闭环：再录一段，看这个档案认不认得自己 ---------------------------
    #
    # 这一步是这个脚本存在的一半理由。没有它，「注册成功」只是「文件写进去了」，而
    # 使用者要到对着麦克风喊的时候才发现相似度是 0.4。现在当场就知道。
    #
    # **2026-08-30 修了一条谎**：此前这里拿整段（3 s）去校验，而唤醒时门用的是「命中前
    # `verify_seconds`（默认 1.5）秒」。实测同一档案 1.0 s 窗得 0.774、3.0 s 窗得 0.846 ——
    # 也就是说这个数字系统性地比门实际给的高 0.07 以上。使用者报的「试一句和实机差 0.2」
    # 就是这条谎叠上「实机那个窗里有一截静音」（实测再掉 0.05–0.09）。
    # 现在**以门的窗长为准**，整段那个数字仍然打出来，但标明它是乐观值。
    window_s = float(load_speaker_config().get("verify_seconds", 1.5))
    print("=" * 70)
    print("闭环校验 —— 再念一句，看这个档案认不认得")
    print("=" * 70)
    print(f"请念：「{plan[0][1]}」（用平时唤醒的距离）")
    for count in (3, 2, 1):
        print(f"  {count}...", end="", flush=True)
        time.sleep(0.7)
    print(" 开始", flush=True)
    samples, stats = _record(args.seconds, device, rate)
    print(f"  peak={stats['peak']:.3f} rms={stats['rms']:.4f} 到轨比例={stats['clip']:.2%}")

    wanted = int(window_s * rate)
    window = samples[-wanted:] if len(samples) > wanted else samples
    verification = provider.verify(window, sample_rate=rate)
    optimistic = provider.verify(samples, sample_rate=rate)
    print()
    print(f"  相似度 = {verification.score:.3f}   阈值 = {provider.threshold}")
    print(f"  判定   = {'**通过**' if verification.accepted else '**拒绝**'}  ({verification.reason})")
    print(f"  （用的是门实际的窗长 {window_s:g}s。整段 {args.seconds:g}s 会得 "
          f"{optimistic.score:.3f} —— 那是乐观值，唤醒时看不到）")
    print()
    if verification.accepted:
        margin = verification.score - provider.threshold
        print(f"这个档案能用，余量 {margin:.3f}。")
        if margin < 0.1:
            print("余量偏薄 —— 换个距离或戴上耳机就可能掉到阈值下面。**不加 --replace 再跑一次**")
            print("（追加语义），段数越多质心越稳：实测 1 段 0.706 / 2 段 0.772 / 3 段 0.794。")
        else:
            print("要覆盖别的条件（更远、戴耳机、另一只麦克风），不加 --replace 再跑一次即可 ——")
            print("那是追加，实测把远场一并注册进来能把远处的相似度从 0.607 抬到 0.722。")
        return 0
    print("这个档案**认不出你自己**。按可能性排序：")
    print("  1. 输入增益太高（上面「到轨比例」不是 0 就是这条）—— 降低麦克风音量后重录")
    print("  2. 注册和这次说话的距离/姿势差太多 —— 用平时唤醒时的距离重录")
    print("  3. 档案是旧的浏览器注册留下的 —— 加 --replace 重来一次")
    print("  4. 环境噪声太大 —— 安静环境重录")
    print()
    print("阈值不要为了通过而调低：0.5 是这个模型上「同人 0.736 / 不同人 0.370」之间的间隙，")
    print("调到 0.4 以下就等于把别人也放进来了（ADR 002）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
