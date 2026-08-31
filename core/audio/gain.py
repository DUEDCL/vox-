"""自适应输入增益：让「麦克风音量调多少」不再是用户的事。

## 为什么需要它

2026-08-29 实测的两个状态，都是同一个问题的两端：

| Windows 输入音量 | 唤醒峰值 | 结果 |
|---|---|---|
| 默认（约 100） | **1.000（削波）** | KWS 命中，但声纹相似度稳定 0.34–0.48，进不去 |
| 调到 **7** | 偏低 | 能命中了，但识别率偏低 |

也就是说这套东西**只在一个很窄的音量窗口里工作**，而那个窗口的位置取决于用哪只麦克风、
戴不戴耳机、离多远。让人每次自己去声音设置里试，是把一个工程问题外包给用户。

## 它能做什么、不能做什么（这条必须说清）

**能**：把偏轻的信号抬到模型习惯的电平。这修的是「音量调到 7 之后偏低」那一端。

**不能**：救回已经削波的信号。削波发生在**声卡的 ADC 里**，等采样进到 Python 已经是一排
贴着 ±1.0 的平顶，信息已经没了 —— 任何软件增益只能把平顶等比缩小，不能把它变回波形。
所以这个类对削波的处理是**报告**而不是修复：`clipping` 计数升上去就说明 OS 那一侧的
输入音量仍然太高，该降的是那个。**OS 那一侧现在能自己调**（`core/audio/winlevel.py` +
控制台的「校准输入音量」），所以这条不再只是一句建议。

## 它有多重要：比原先以为的小得多（2026-09-01 实测）

把一段真录音（本人念三遍「你好小沃」）等比缩到不同峰值，喂进生产那条回调路径数命中，
**完全不加增益** 在 0.02–0.75 之间是满分 —— 这个 KWS 的特征是归一化的，绝对电平几乎不影响
它。增益真正救回的只有 0.01 那一档。**而增益动得快是会扣命中的**：见 `DEFAULT_RELEASE`
的表，旧的 `release=0.05` 在 0.10 那一档把 3/3 打成 2/3。

所以这一级的定位是**救援**，不是常态 AGC：默认几乎不动（`release=0.005`），只在电平低到
模型撑不住时才慢慢抬上去。

真正的「自动调 OS 音量」已经落地：`core/audio/winlevel.py`（纯 ctypes 走 Core Audio 的
`IAudioEndpointVolume`）+ 控制台的「校准输入音量」（闭环二分）。

## 为什么是包络跟随而不是逐块归一化

逐块归一化（每 100 ms 各自归一）会把**语音自身的动态**抹平：一句话里的重音和气口被压成
同一个电平，而 KWS 和声纹模型都是在有动态的语音上训练的。所以这里跟的是一条**缓释包络**：
增益随峰值缓慢变化，一句话内部基本恒定，跨场景（换耳机、离远）才移动。

- 抬升慢（`release`）：避免在停顿处把底噪抬成一片嘶声，也避免改写语音的时间包络。
- 压低快（`attack`）：一句话突然变响时先保住不失真。
- 底噪不动（`floor_peak`）：低于这个峰值一律按 1.0 处理，静默永远不被放大。
- **输出封顶（`ceiling`）：乘之前就压，绝不让这一级自己造出削波。**
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: 目标峰值。0.5 是折中：离轨还有一倍余量（避免自己制造削波），又足够让模型看到清晰的语音。
DEFAULT_TARGET_PEAK = 0.5

#: 增益上下限。上限 20 倍 ≈ 26 dB —— 够把「Windows 输入音量 7」那种电平（实测峰值
#: 0.02–0.1）抬回 0.5，又不至于把一间静室的底噪放大成风声（底噪由 floor_peak 挡住，
#: 增益在静默块上根本不动）。下限 0.1 是给「OS 音量拉满」留一条不再二次失真的退路 ——
#: 那种情况削波已经在 ADC 里发生，缩小只是别让下游再叠一层。
DEFAULT_MAX_GAIN = 20.0
DEFAULT_MIN_GAIN = 0.1

#: 低于这个峰值的块被当成静默：增益不动、包络不更新。
DEFAULT_FLOOR_PEAK = 0.004

#: **每块输出的硬上限，在乘之前就按它压。**
#:
#: 0.95 而不是 1.0：留一点余量给后续处理的插值/重采样，也让「贴着轨」和「过轨」能分开。
#: 这一条是 2026-09-01 那个缺陷的正解 —— 见下面 `ENVELOPE_DECAY` 的注释。
DEFAULT_CEILING = 0.95

#: 输入峰值包络每块（100 ms）的回落系数。0.97 ≈ 落 10 倍要 3.8 秒。
#:
#: **这个包络是 2026-09-01 那个缺陷的另一半。** 在它之前，`wanted` 直接由**当前这一块**的
#: 峰值算：`target / peak`。而一句话里大部分 100 ms 的块是气口、轻辅音、字与字之间 ——
#: 那些块峰值只有 0.03，于是 `wanted` 冲到 16 倍、增益一路往上爬；紧接着的重音块（峰值
#: 0.75）被乘成 1.7，硬裁到 1.0。**削波，而且是我们自己在软件里造的。**
#:
#: 实测（`.vox-ref/rec/你好小沃 你好小沃 你好小沃.wav`，本人真实录音，原始峰值 0.746，
#: 喂进生产那条回调路径）：
#:
#: | 配置 | 「你好小沃」命中 | 增益末值 | 输出峰值 |
#: |---|---|---|---|
#: | 逐块峰值算 wanted（旧） | **1 / 3** | 3.86 | **1.0（削波）** |
#: | 不加增益 | **3 / 3** | 1.0 | 0.746 |
#:
#: 也就是说：为了救「设备太轻」而加的这一级，在一台**电平正常**的机器上把唤醒率打成了
#: 三分之一。使用者报的「注册正常、试一句正常，就是无法真实唤醒」就是它 —— 声纹读的是
#: 原始环形缓冲（不过增益），KWS 读的是增益之后的（被自己削平）。
ENVELOPE_DECAY = 0.97

#: 抬高的追赶比例。**0.005 是量出来的，不是调出来的。**
#:
#: 增益在**一句话之内**必须基本恒定。0.05 时它每块动 5%，一秒钟能移 40% —— 那等于把语音
#: 的时间包络改写了一遍，而 KWS 是在没有被这样改写过的语音上训练的。同一段真录音喂进生产
#: 那条回调路径，把整段等比缩到不同峰值（「你好小沃」念三遍，满分 3）：
#:
#: | 配置 | 0.746 | 0.30 | 0.10 | 0.05 | 0.02 | 0.01 |
#: |---|---|---|---|---|---|---|
#: | 完全不加增益 | 3 | 3 | 3 | 3 | 3 | **2** |
#: | target 0.5 / release **0.05**（旧） | 3 | 3 | **2** | 3 | 3 | 3 |
#: | target 0.5 / release **0.005** | **3** | **3** | **3** | **3** | **3** | **3** |
#: | target 0.3 / release 0.005 | 3 | **2** | 3 | 3 | 3 | 3 |
#:
#: 两件事同时被这张表钉住：①这个 KWS 模型本身在 0.02–0.75 之间对绝对电平几乎不敏感
#: （它的特征是归一化的），所以增益的价值只在 0.01 那一档 —— 一级「救援」而不是一级 AGC；
#: ②增益动得快反而扣命中。压低方向（`attack`）保持快，因为那一侧的风险是失真。
#:
#: **这张表的每一行都已经带着包络与封顶两处修法**（否则第一行就是 1/3，见 `ENVELOPE_DECAY`）
#: —— 所以 0.10 那一档的 2/3 是 `release` 单独的账，不是削波的账。
DEFAULT_RELEASE = 0.005
DEFAULT_ATTACK = 0.5


class AutoGain:
    """一条缓释包络 + 一个增益。线程亲和：只在音频回调线程上被调。"""

    def __init__(
        self,
        *,
        target_peak: float = DEFAULT_TARGET_PEAK,
        max_gain: float = DEFAULT_MAX_GAIN,
        min_gain: float = DEFAULT_MIN_GAIN,
        floor_peak: float = DEFAULT_FLOOR_PEAK,
        ceiling: float = DEFAULT_CEILING,
        attack: float = DEFAULT_ATTACK,
        release: float = DEFAULT_RELEASE,
    ) -> None:
        self.target_peak = float(target_peak)
        self.max_gain = float(max_gain)
        self.min_gain = float(min_gain)
        self.floor_peak = float(floor_peak)
        self.ceiling = float(ceiling)
        #: 每块允许的追赶比例。attack 用在「要压低」的方向，release 用在「要抬高」的方向。
        self.attack = float(attack)
        self.release = float(release)
        self.gain = 1.0
        #: 输入峰值包络（**不是**当前块的峰值）。见 ENVELOPE_DECAY。
        self.envelope = 0.0
        #: 观测量，给控制台与诊断脚本用。不含音频。
        self.blocks = 0
        self.speech_blocks = 0
        self.clipped_blocks = 0
        #: 被上限压过的块数。**它不为零不是故障**，是这一级在正常工作；一直增长说明包络
        #: 还是偏高（或者 OS 那一侧音量该降）。
        self.limited_blocks = 0
        self.last_peak = 0.0

    def apply(self, block: Any, *, is_speech: bool | None = None) -> Any:
        """处理一块音频，返回加过增益的副本。原数组不改（调用方可能还要用它）。

        ``is_speech`` 来自 VAD。**这个参数是 2026-08-31 事故的修法**：在它之前，「要不要
        抬增益」由一条峰值线（``floor_peak``=0.004）决定，而房间底噪高于那条线 —— 于是
        增益把底噪一路抬到目标电平，下游看到一段 rms 0.21 的「健康语音」，声纹从它注册出
        一个房间的指纹，再拿一段底噪去比得 0.979「通过」。

        峰值、RMS、削波比例都是能量统计量，而「是不是人声」不是能量问题；用能量去近似它
        必然在某台设备上翻车。实测（`core/audio/vad.py` 的冒烟）：同一段底噪放大 10 倍后
        VAD 判 False，而真实人声缩到峰值 **0.01** 仍然判 True —— 这正是「无论何种设备、
        音量」要的那条判据。

        ``None`` = 没有 VAD，退回原来的峰值线（那条路仍然会被底噪骗，所以生产上要接 VAD）。
        """
        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        self.blocks += 1
        if samples.size == 0:
            return samples
        peak = float(np.max(np.abs(samples)))
        self.last_peak = peak
        if peak >= 0.999:
            # 削波已经在 ADC 里发生了，这里只能记账。见模块头。
            self.clipped_blocks += 1
        voiced = (peak >= self.floor_peak) if is_speech is None else bool(is_speech)
        if voiced and peak > 0.0:
            self.speech_blocks += 1
            # 包络：起音立刻跟上，回落缓慢。**不许用当前块的峰值直接算增益** —— 一句话里
            # 的气口和轻辅音那几块峰值只有 0.03，会把增益一路推上去，紧接着的重音就被自己
            # 削平。见 ENVELOPE_DECAY 的实测表。
            self.envelope = max(peak, self.envelope * ENVELOPE_DECAY)
            wanted = self.target_peak / max(self.envelope, self.floor_peak)
            wanted = max(self.min_gain, min(self.max_gain, wanted))
            # 压低走 attack（快），抬高走 release（慢）。
            rate = self.attack if wanted < self.gain else self.release
            self.gain += (wanted - self.gain) * rate
        # **上限在乘之前生效。** 包络是滞后的，所以任何一块都可能算出一个会过轨的增益；
        # 事后 np.clip 把它裁平，而裁平就是削波 —— 实测那让「你好小沃」的命中从 3/3 掉到
        # 1/3。这一行让「我们自己造出削波」在构造上不可能发生。
        applied = self.gain
        if peak > 0.0:
            applied = min(applied, self.ceiling / peak)
            if applied < self.gain - 1e-6:
                self.limited_blocks += 1
        if abs(applied - 1.0) < 1e-3:
            return samples
        # 仍然留着这一裁：它现在是不该被触发的安全网，不是设计的一部分。
        return np.clip(samples * applied, -1.0, 1.0).astype(np.float32)

    def describe(self) -> dict[str, Any]:
        """给就绪清单与诊断用的读数。**削波比例是这里最重要的一个数字** ——
        它不为零就说明该降的是 OS 那一侧的输入音量，不是这里的参数。"""
        return {
            "gain": round(self.gain, 3),
            "target_peak": self.target_peak,
            "last_peak": round(self.last_peak, 4),
            "envelope": round(self.envelope, 4),
            "blocks": self.blocks,
            "speech_blocks": self.speech_blocks,
            "clipped_blocks": self.clipped_blocks,
            "clipping": self.clipped_blocks > 0,
            # 上限压过多少块。**这个数字是「我们自己有没有在造削波」的答案** ——
            # 它此前根本不存在，于是软件削波在每一处读数里都是不可见的。
            "limited_blocks": self.limited_blocks,
        }


__all__ = [
    "DEFAULT_ATTACK",
    "DEFAULT_CEILING",
    "DEFAULT_FLOOR_PEAK",
    "DEFAULT_MAX_GAIN",
    "DEFAULT_MIN_GAIN",
    "DEFAULT_RELEASE",
    "DEFAULT_TARGET_PEAK",
    "ENVELOPE_DECAY",
    "AutoGain",
]
