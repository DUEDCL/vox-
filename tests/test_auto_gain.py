"""自适应输入增益与声纹归一化：两件让「音量」不再是用户的事的东西。

证据等级：**AUTO**。用合成的波形，不需要麦克风。

这一组的由来是使用者 2026-08-29 的两次报告，它们是同一个问题的两端：

| Windows 输入音量 | 唤醒峰值 | 结果 |
|---|---|---|
| 默认（约 100） | **1.000（削波）** | KWS 命中，声纹相似度稳定 0.34–0.48，进不去 |
| 调到 **7** | 偏低 | 能命中，但「声纹识别率有点低」 |

原话是「不可能让用户每次都自己调节输入音量的大小」。对。
"""

from __future__ import annotations

import numpy as np
import pytest

from core.audio.gain import AutoGain
from core.audio.speaker import EMBED_TARGET_PEAK, normalise_for_embedding


def _tone(peak: float, samples: int = 1600) -> np.ndarray:
    return (np.sin(np.linspace(0.0, 40.0, samples)) * peak).astype(np.float32)


# ------------------------------------------------------------------ AutoGain


def test_a_quiet_input_is_brought_up_toward_the_target():
    """「输入音量 7」那一端：偏轻的信号要被抬起来。

    断言的是**收敛到目标电平**，不是某一块的值 —— 增益跟的是缓释包络。**2026-09-01 起
    那条包络慢得多**（`release` 0.05 → 0.005），因为实测增益动得快会扣唤醒命中：同一段
    真录音在峰值 0.10 那一档，快包络把「你好小沃」的命中从 3/3 打成 2/3（见
    `core/audio/gain.py` 的 `DEFAULT_RELEASE`）。所以这里要跑更多块才收敛，而那正是
    要的行为：一句话之内增益基本不动。
    """
    gain = AutoGain()
    quiet = _tone(0.05)  # 峰值 0.05 ≈ 实测「输入音量 7」的量级
    for _ in range(3000):
        out = gain.apply(quiet)
    assert gain.gain > 8.0, "0.05 峰值该被抬到接近目标"
    assert float(np.max(np.abs(out))) == pytest.approx(gain.target_peak, abs=0.05)


def test_a_pause_inside_a_sentence_does_not_push_the_gain_up():
    """**一句话里的气口不许把增益推上去。** 这一条是 2026-09-01 那次唤醒失效的直接教训。

    `wanted` 曾按**当前块**的峰值算。一句话里大部分 100 ms 的块是气口、轻辅音、字与字
    之间 —— 那些块峰值只有 0.03，于是增益一路往上爬，紧接着的重音块被乘过轨、被裁平。
    现在跟的是**输入峰值包络**（起音立刻跟上、回落缓慢），所以安静的那几块只会让它缓慢
    回落，不会让它冲高。

    数量级的差别：旧行为 10 块之后增益约 5.9 倍，现在基本不动。
    """
    gain = AutoGain()
    gain.apply(_tone(0.75), is_speech=True)
    settled = gain.gain

    for _ in range(10):  # 1 秒的「安静但仍在说话」
        gain.apply(_tone(0.03), is_speech=True)

    assert gain.gain <= settled * 1.02, f"气口把增益从 {settled} 推到了 {gain.gain}"


def test_the_gain_never_makes_its_own_clipping():
    """**这一级绝不许自己造出削波。**

    2026-09-01 实机：使用者「注册正常、试一句正常，就是无法真实唤醒」。根因是这里 ——
    `wanted` 曾按**当前块**的峰值算，而一句话里的气口、轻辅音那些块峰值只有 0.03，于是
    增益爬到 4–6 倍；紧接着的重音块（峰值 0.75）被乘成 1.7，然后被 `np.clip` 裁平。
    裁平就是削波。实测「你好小沃」念三遍的命中：3/3 → **1/3**。

    修法是**上限在乘之前生效**（`ceiling / peak`），所以这件事在构造上不可能再发生。
    """
    gain = AutoGain()
    for _ in range(3000):  # 先让增益爬到高位
        gain.apply(_tone(0.02))
    assert gain.gain > 8.0

    out = gain.apply(_tone(0.9))  # 突然来一块正常电平的

    peak = float(np.max(np.abs(out)))
    assert peak <= gain.ceiling + 1e-6, f"输出峰值 {peak} 越过了上限"
    assert peak < 0.999, "贴轨就是削波 —— 这一级不许制造它"
    assert gain.describe()["limited_blocks"] >= 1, "被上限压过要记账，否则软件削波不可见"


def test_the_gain_moves_slowly_not_in_one_block():
    """一块就跳到目标 = 逐块归一 = 动态被抹平。这条钉住「慢」。"""
    gain = AutoGain()
    gain.apply(_tone(0.05))
    assert gain.gain < 3.0, "第一块不该一步跳到 10 倍"


def test_a_loud_input_is_pulled_down_faster_than_a_quiet_one_is_pushed_up():
    """压低走 attack（快），抬高走 release（慢）—— 失真的代价比偏轻大。

    比的是**一块之内走完了距离的几成**，不是增益的绝对变化量：偏轻那一侧的目标距离
    本来就远得多，比绝对量会得出反过来的结论（那是这条测试第一版写错的地方）。
    """
    down = AutoGain()
    down.apply(_tone(1.0))
    wanted_down = max(down.min_gain, min(down.max_gain, down.target_peak / 1.0))
    covered_down = (1.0 - down.gain) / (1.0 - wanted_down)

    up = AutoGain()
    up.apply(_tone(0.05))
    wanted_up = max(up.min_gain, min(up.max_gain, up.target_peak / 0.05))
    covered_up = (up.gain - 1.0) / (wanted_up - 1.0)

    assert covered_down > covered_up * 5, "压低必须比抬高快一个量级"
    assert covered_down == pytest.approx(down.attack, abs=1e-6)
    assert covered_up == pytest.approx(up.release, abs=1e-6)


def test_silence_is_never_amplified():
    """底噪被放大 12 倍是一片嘶声，而 KWS 会在那上面误命中。"""
    gain = AutoGain()
    for _ in range(50):
        out = gain.apply(np.zeros(1600, dtype=np.float32))
    assert gain.gain == pytest.approx(1.0)
    assert float(np.max(np.abs(out))) == 0.0
    assert gain.speech_blocks == 0


def test_clipping_is_counted_not_claimed_to_be_fixed():
    """削波发生在声卡的 ADC 里 —— 采样进到这里已经是平顶，软件只能等比缩小。

    所以这个类对削波的姿态是**报告**：`clipping` 为真就说明该降的是 OS 那一侧的输入音量。
    一个声称能修削波的增益器会让人以为问题解决了。
    """
    gain = AutoGain()
    railed = np.ones(1600, dtype=np.float32)
    gain.apply(railed)
    report = gain.describe()
    assert report["clipping"] is True
    assert report["clipped_blocks"] == 1
    assert gain.apply(_tone(0.3)) is not None
    assert gain.describe()["clipped_blocks"] == 1, "干净的块不该增加削波计数"


def test_the_output_never_leaves_the_rails():
    """包络是滞后的，所以乘完仍可能过轨。硬裁一次好过把失真交给下游模型。"""
    gain = AutoGain()
    for _ in range(200):
        gain.apply(_tone(0.02))
    out = gain.apply(_tone(0.9))  # 增益还停在高位，突然来一块大的
    assert float(np.max(np.abs(out))) <= 1.0


def test_describe_carries_no_audio():
    """读数进控制台和日志，所以它只能有数字。"""
    gain = AutoGain()
    gain.apply(_tone(0.3))
    report = gain.describe()
    assert set(report) == {
        "gain", "target_peak", "last_peak", "envelope", "blocks", "speech_blocks",
        "clipped_blocks", "clipping", "limited_blocks",
    }
    assert all(isinstance(value, (int, float, bool)) for value in report.values())


# --------------------------------------------------- 声纹进模型前的峰值归一化


def test_enrolment_and_verification_land_on_the_same_level():
    """**这是「声纹识别率低」的核心修法。**

    注册与校验走的是不同的路（浏览器 getUserMedia 带自动增益 / `sd.rec` 裸录 /
    sounddevice 采集回调），电平各不相同。不归一化的话，余弦相似度测的有一部分是
    「音量差」而不是「说话人差」—— 实测同一个人同一台机器落在 0.339–0.484，
    而这个模型自己测试集上「不同人」是 0.370。

    归一化放在 `embed()` 里而不是采集里，因为**那里是两条路唯一的交汇点**。
    """
    loud = _tone(0.95, 16000)
    quiet = (loud * 0.03).astype(np.float32)

    a = normalise_for_embedding(loud)
    b = normalise_for_embedding(quiet)

    assert float(np.max(np.abs(a))) == pytest.approx(EMBED_TARGET_PEAK, abs=1e-5)
    assert float(np.max(np.abs(b))) == pytest.approx(EMBED_TARGET_PEAK, abs=1e-5)
    # 归一化之后两段应当几乎逐点相同 —— 它们本来就是同一条波形的两个音量。
    assert float(np.max(np.abs(a - b))) < 1e-3


def test_normalising_silence_does_not_divide_by_zero():
    """全静音原样返回。那种输入该被质量门拒掉，不该被放大成噪声。"""
    silence = np.zeros(16000, dtype=np.float32)
    assert float(np.max(np.abs(normalise_for_embedding(silence)))) == 0.0
    assert len(normalise_for_embedding(np.zeros(0, dtype=np.float32))) == 0


def test_normalising_leaves_headroom():
    """目标是 0.7 不是 1.0：归一化到满幅会让后续的重采样插值与模型内部预加重碰到轨,
    那等于自己制造削波。"""
    assert EMBED_TARGET_PEAK < 1.0
    out = normalise_for_embedding(_tone(0.5, 16000))
    assert float(np.max(np.abs(out))) < 1.0


# ------------------- 增益放在哪里（2026-08-31：它曾经伪造出一段「健康语音」）


class _Kws:
    def __init__(self) -> None:
        self.seen: list[float] = []

    def load(self):
        from core.audio.base import ProviderStatus

        return ProviderStatus(True, "stub", {})

    def create_stream(self):
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, sample_rate
        self.seen.append(float(np.max(np.abs(samples))))
        return []

    def close(self):
        pass


def _capture_with_gain():
    from core.audio import SounddeviceWakeCapture

    kws = _Kws()
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        blocksize=1600,
        require_verification=False,
        auto_gain=AutoGain(),
    )
    capture._inference_stream = kws.create_stream()
    return capture, kws


def test_the_ring_keeps_raw_audio_while_kws_gets_the_gained_copy():
    """**环形缓冲存原始音频，增益只喂 KWS/ASR。**

    此前两边都吃加过增益的样本，那条路会伪造现实。2026-08-31 实机：设备原始峰值 0.0587
    （五分钟最大值，期间使用者在说话），底噪高于 AutoGain 的 floor_peak(0.004)，于是增益
    爬到约 10 倍，缓冲里的「静音」变成 rms 0.21 / peak 0.53 —— 看上去是一段健康语音。
    三层同时失效：声纹质量门（跑在增益之后）永不触发；从缓冲注册的档案录到的是**放大后的
    房间底噪**；再拿一段底噪去比它，余弦 **0.979「通过」** —— 一道本该 fail-closed 的门
    变成了 fail-open。使用者的原话是「我没说话，等了一会，相似度 0.9」。

    声纹路径本来就在 `embed()` 里按峰值归一化，所以增益对它一点好处都没有，只有
    「让质量门失灵」这一个作用。
    """
    capture, kws = _capture_with_gain()
    quiet = np.full((1600, 1), 0.05, dtype="float32")

    for _ in range(200):
        capture._callback(quiet, 1600, None, None)

    assert kws.seen[-1] > 0.3, "KWS 要看到抬起来的电平（它需要接近训练电平）"
    ring_peak = float(np.max(np.abs(capture.recent_audio(1.0))))
    assert ring_peak == pytest.approx(0.05, abs=1e-4), "缓冲必须是原始电平，不许被增益改写"


def test_the_quality_floor_can_still_fire_after_the_gain_is_wired():
    """反向断言，而且它是这一组的要点：一道跑在增益之后的质量门等于没有门。

    原始底噪 0.002 会被 20 倍增益抬到 0.04 —— 远高于 `min_rms`，于是「太轻」永远不触发。
    缓冲存原始音频之后，同一段音频仍然是 0.002，门照常拒。
    """
    from core.audio.speaker import SpeakerVerificationProvider

    capture, _kws = _capture_with_gain()
    whisper = np.full((1600, 1), 0.0015, dtype="float32")
    for _ in range(200):
        capture._callback(whisper, 1600, None, None)

    window = capture.recent_audio(1.5)
    verdict = SpeakerVerificationProvider(model_path="nope.onnx", store_path="nope.json").verify(window)

    assert verdict.accepted is False
    assert "too quiet" in verdict.reason


def test_the_gain_only_adapts_on_speech_when_a_vad_says_so():
    """**这是 2026-08-31 那次 fail-open 的正解。**

    在它之前，「要不要抬增益」由一条峰值线（`floor_peak`=0.004）决定，而房间底噪高于那条
    线 —— 于是增益把底噪一路抬到目标电平，下游看到一段 rms 0.21 的「健康语音」，声纹从它
    注册出一个房间的指纹，再拿一段底噪去比得 0.979「通过」。

    能量统计量分不清「轻的语音」和「没有语音」。给它一个 VAD 的答案，底噪就再也抬不上来。
    """
    noise = _tone(0.03)  # 峰值 0.03，远高于 floor_peak(0.004)

    without_vad = AutoGain()
    for _ in range(3000):
        without_vad.apply(noise)

    with_vad = AutoGain()
    for _ in range(3000):
        with_vad.apply(noise, is_speech=False)

    assert without_vad.gain > 8.0, "旧行为：底噪被当成语音，增益爬上去（这就是那个 bug）"
    assert with_vad.gain == pytest.approx(1.0), "VAD 说不是语音 -> 包络一步都不动"


def test_a_quiet_talker_still_gets_lifted_when_the_vad_agrees():
    """反向断言：VAD 说是语音时，偏轻的信号照样要被抬起来 —— 那是 AutoGain 存在的理由。

    块数比直觉多，因为 `release` 从 0.05 降到了 0.005（快包络会扣唤醒命中，见
    `test_the_gain_barely_moves_inside_one_utterance`）。**这一级是救援不是常态 AGC**：
    实测这个 KWS 在 0.02–0.75 之间不加增益也是满分，增益真正救回的只有 0.01 那一档。
    """
    gain = AutoGain()
    quiet = _tone(0.05)
    for _ in range(3000):
        out = gain.apply(quiet, is_speech=True)

    assert float(np.max(np.abs(out))) == pytest.approx(0.5, abs=0.05)
