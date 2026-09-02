"""Gate hardening: quality floors, brute-force cooldown, multi-window vote.

These run without the speaker model on purpose -- the cheap input-side gates
are placed *before* the model check in ``verify``, so their behaviour is fully
testable here. The hardening is heuristics against junk inputs and brute-force
attempts; it does NOT claim replay-attack detection (ADR 002 limitation).

Evidence level: AUTO.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.audio import SpeakerVerificationProvider
from core.audio.speaker import load_speaker_config


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _provider(tmp_path, **kwargs) -> SpeakerVerificationProvider:
    return SpeakerVerificationProvider(
        tmp_path / "absent-model.onnx",
        store_path=tmp_path / "voiceprints.json",
        **kwargs,
    )


# -- quality gates (run before the model, hence testable model-free) -----------


def test_silence_is_rejected_before_the_model_check(tmp_path):
    result = _provider(tmp_path).verify([0.0] * 16000)

    assert result.accepted is False
    assert "too quiet" in result.reason


def test_clipped_audio_is_rejected(tmp_path):
    loud = np.array([1.0, -1.0] * 8000, dtype=np.float32)
    result = _provider(tmp_path).verify(loud)

    assert result.accepted is False
    assert "clipped" in result.reason


def test_healthy_amplitude_still_reaches_the_model_gate(tmp_path):
    speech_like = (np.sin(np.linspace(0, 400, 16000)) * 0.2).astype(np.float32)
    result = _provider(tmp_path).verify(speech_like)

    assert result.accepted is False
    assert "not found" in result.reason  # quality passed; model gate answered


def test_empty_buffer_is_quality_rejected(tmp_path):
    assert "empty" in _provider(tmp_path).verify(np.zeros(0, dtype=np.float32)).reason


# -- brute-force cooldown -------------------------------------------------------


def test_repeated_junk_locks_the_gate_then_it_expires(tmp_path):
    clock = FakeClock(start=100.0)
    provider = _provider(
        tmp_path,
        max_consecutive_rejections=2,
        cooldown_s=30.0,
        clock=clock,
    )
    silence = [0.0] * 16000

    first = provider.verify(silence)
    second = provider.verify(silence)
    assert first.accepted is False and second.accepted is False
    clock.now += 1.0

    third = provider.verify(silence)  # even good audio is refused now
    assert third.accepted is False
    assert "cooling down" in third.reason

    clock.now += 31.0  # past the cooldown
    expired = provider.verify(silence)
    assert "cooling down" not in expired.reason


def test_streak_window_older_than_a_cooldown_starts_fresh(tmp_path):
    clock = FakeClock(start=100.0)
    provider = _provider(
        tmp_path,
        max_consecutive_rejections=2,
        cooldown_s=30.0,
        clock=clock,
    )
    silence = [0.0] * 16000

    provider.verify(silence)
    clock.now += 91.0  # longer than the streak window (max(cooldown, 60))
    provider.verify(silence)  # old pressure forgotten; this is strike one

    assert provider.gate_stats["consecutive_rejections"] == 1


def test_gate_stats_count_each_path(tmp_path):
    clock = FakeClock()
    provider = _provider(tmp_path, max_consecutive_rejections=99, clock=clock)
    silence = [0.0] * 16000

    provider.verify(silence)
    provider.verify((np.ones(16000, dtype=np.float32)))  # clipped

    stats = provider.gate_stats
    assert stats["rejected_quality"] == 2
    assert stats["accepted"] == 0
    assert stats["consecutive_rejections"] == 2


# -- multi-window verification (scripted embeddings, no model) ------------------


def _speech_like(samples: int) -> np.ndarray:
    """Audible buffer that clears the RMS floor and the clip ceiling."""
    return (np.sin(np.linspace(0, samples * 0.05, samples)) * 0.2).astype(np.float32)


class ScriptedManager:
    def __init__(self, speakers) -> None:
        self.all_speakers = list(speakers)
        self.num_speakers = len(self.all_speakers)

    def score(self, name, vector):
        # The scripted embedding carries the score plus an index of which
        # enrolled speaker this window matched; other names score below zero.
        matched = self.all_speakers[int(vector[1]) % len(self.all_speakers)]
        if name != matched:
            return -1.0
        return float(vector[0])


class ScriptedSpeaker(SpeakerVerificationProvider):
    """Real gate flow, scripted embeddings: no model, no enrollment store."""

    def __init__(self, scores, *, tmp_path, speakers=("due",), **kwargs):
        kwargs.setdefault("clock", FakeClock())
        super().__init__(
            tmp_path / "absent-model.onnx",
            store_path=tmp_path / "voiceprints.json",
            **kwargs,
        )
        self._scores = [list(entry) for entry in scores]
        self._extractor = object()  # _require() sees a loaded engine
        self._manager = ScriptedManager(speakers)

    def embed(self, samples, sample_rate=16000):
        entry = self._scores.pop(0)
        score = entry[0]
        speaker_index = entry[1] if len(entry) > 1 else 0
        return [float(score), float(speaker_index)]


def test_multi_window_requires_unanimous_match(tmp_path):
    tts = ScriptedSpeaker([[0.9], [0.85]], tmp_path=tmp_path, verify_windows=2)

    result = tts.verify(_speech_like(4 * 16000))

    assert result.accepted is True
    assert result.reason == "all windows match"
    assert result.score == pytest.approx(0.9)


def test_one_weak_window_rejects_the_whole_attempt(tmp_path):
    tts = ScriptedSpeaker([[0.9], [0.3]], tmp_path=tmp_path, verify_windows=2)

    result = tts.verify(_speech_like(4 * 16000))

    assert result.accepted is False
    assert "window 1 below threshold" in result.reason


def test_disagreeing_windows_reject(tmp_path):
    tts = ScriptedSpeaker(
        [[0.9, 0], [0.9, 1]],
        tmp_path=tmp_path,
        verify_windows=2,
        speakers=("due", "intruder"),
    )

    result = tts.verify(_speech_like(4 * 16000))

    assert result.accepted is False
    assert "disagree" in result.reason


def test_short_buffer_cannot_run_multi_window(tmp_path):
    tts = ScriptedSpeaker([], tmp_path=tmp_path, verify_windows=2)

    result = tts.verify(_speech_like(8000))  # < 2 x 0.6 s

    assert result.accepted is False
    assert "not enough audio" in result.reason


def test_multi_window_accept_resets_the_streak(tmp_path):
    tts = ScriptedSpeaker([[0.9], [0.9]], tmp_path=tmp_path, verify_windows=2)
    tts._rejection_streak = 4
    tts.gate_stats["consecutive_rejections"] = 4

    tts.verify(_speech_like(4 * 16000))

    assert tts.gate_stats["consecutive_rejections"] == 0


# -- configuration and audit surface --------------------------------------------


def test_hardening_keys_flow_through_from_config(tmp_path):
    config = tmp_path / "speaker.toml"
    config.write_text(
        "[speaker]\n"
        "min_rms = 0.02\n"
        "verify_windows = 3\n"
        "cooldown_s = 12\n",
        encoding="utf-8",
    )
    merged = load_speaker_config(config)

    assert merged["min_rms"] == 0.02
    assert merged["verify_windows"] == 3
    assert merged["cooldown_s"] == 12
    # Defaults stay secure when a key is absent.
    assert merged["max_clip_ratio"] == 0.05
    assert merged["max_consecutive_rejections"] == 5


def test_describe_reports_gate_config_and_counts_without_vectors(tmp_path):
    import json
    provider = _provider(tmp_path, verify_windows=2, cooldown_s=15.0)
    provider.store.save({"due": [[0.1, 0.2], [0.3, 0.4]]}, dim=2)
    provider.verify([0.0] * 16000)  # one quality rejection for the counter

    described = provider.describe()

    assert described["gate"]["verify_windows"] == 2
    assert described["gate"]["cooldown_s"] == 15.0
    assert described["gate_stats"]["rejected_quality"] == 1
    serialised = json.dumps(described)
    assert "0.1" not in serialised.replace("0.05", "").replace("0.002", "")
    assert "[0.1, 0.2]" not in serialised


# -- 注册侧的同一道质量门 -----------------------------------------------------
#
# 这一组钉死的是 2026-08-29 查出的一条不对称:verify() 查音频质量,enroll() 不查。
# 后果不是「注册失败」而是「注册成功、然后本人永远唤不醒」—— 三个噪声向量落进档案,
# describe() 报 3 个样本,每次唤醒被拒的原因写着 below threshold,看不出根因在注册那一侧。
#
# 这条路真实存在:控制台注册走浏览器 getUserMedia,取系统默认输入设备,而 Windows 上一个
# 被静音/被隐私设置拒绝的默认设备是**静默而不是报错**的。本机实测默认设备录 1 秒
# peak=0.00003。所以采集侧和注册侧会同时中招,而只有采集侧有检测。


def test_a_silent_enrollment_sample_is_refused_not_silently_accepted(tmp_path):
    """静音注册必须当场报错。静默接受它等于造一个永远拒绝本人的门。"""
    provider = _provider(tmp_path)
    provider._extractor = object()  # 越过模型加载,只测质量门
    with pytest.raises(Exception) as excinfo:
        provider.enroll("du", [np.zeros(16000, dtype=np.float32)])
    assert "too quiet" in str(excinfo.value)


def test_the_refusal_names_which_sample_was_bad(tmp_path):
    """三段里坏的是第二段时,消息要说「第 2 段」—— 让人知道重录哪一段。"""
    provider = _provider(tmp_path)
    provider._extractor = object()
    good = (np.sin(np.linspace(0, 400, 16000)) * 0.2).astype(np.float32)
    with pytest.raises(Exception) as excinfo:
        provider.enroll("du", [good, np.zeros(16000, dtype=np.float32), good])
    assert "sample 2" in str(excinfo.value)


def test_a_clipped_enrollment_sample_is_refused(tmp_path):
    """过载的注册音频同样拒:削平的波形已经丢了说话人特征。"""
    provider = _provider(tmp_path)
    provider._extractor = object()
    with pytest.raises(Exception) as excinfo:
        provider.enroll("du", [np.array([1.0, -1.0] * 8000, dtype=np.float32)])
    assert "clipped" in str(excinfo.value)


def test_serving_the_cooldown_clears_the_streak(tmp_path):
    """服刑期满即销账 —— 否则本人被压到「每 30 秒只有一次真实尝试」。

    这是 2026-08-29 从使用者实机日志里读出来的缺陷:`cooling down for 0.5s` →
    一次真实校验 0.484 → 紧接着 `cooling down for 25.2s`。原因是 _rejection_streak
    要等「距上次拒绝超过 60 秒」才归零,而冷却只有 30 秒,于是计数一直停在上限,
    冷却结束后再拒一次就立刻又是 30 秒。

    暴力防护要的是限速,不是累加惩罚。等满了就该重新给满额度。
    """
    clock = FakeClock()
    provider = _provider(tmp_path, max_consecutive_rejections=3, cooldown_s=30.0, clock=clock)

    # 三次静音拒绝 -> 进冷却
    for _ in range(3):
        provider.verify([0.0] * 16000)
    assert provider.gate_stats["consecutive_rejections"] == 3
    assert "cooling down" in provider.verify([0.0] * 16000).reason

    # 等满 30 秒:下一次不该再是冷却,而且计数已经归零
    clock.now += 31.0
    result = provider.verify([0.0] * 16000)
    assert "cooling down" not in result.reason
    assert provider.gate_stats["consecutive_rejections"] == 1, "销账后这是新一轮的第 1 次"

    # 而且要再攒满 3 次才会重新进冷却 —— 不是「一次就又锁 30 秒」
    provider.verify([0.0] * 16000)
    assert "cooling down" not in provider.verify([0.0] * 16000).reason
    assert "cooling down" in provider.verify([0.0] * 16000).reason


def test_the_cooldown_still_throttles_within_one_window(tmp_path):
    """反向断言:销账不能把限速取消掉。窗口内连续尝试仍然会被锁。"""
    clock = FakeClock()
    provider = _provider(tmp_path, max_consecutive_rejections=3, cooldown_s=30.0, clock=clock)
    for _ in range(3):
        provider.verify([0.0] * 16000)
    armed_at = clock.now
    for offset in (0.0, 5.0, 10.0, 29.0):  # 绝对偏移,不是累加
        clock.now = armed_at + offset
        assert "cooling down" in provider.verify([0.0] * 16000).reason


def test_a_below_threshold_rejection_names_the_clipping(tmp_path):
    """拒绝原因里必须带**测出来的输入质量**,不能只说「below threshold」。

    这是 2026-08-29 实机日志的教训:每一次唤醒的峰值都是 1.000(削波),而拒绝原因只写
    「below threshold 0.5」。那句话把人引向「调阈值」或「换模型」,而真正的毛病是麦克风
    增益太高 —— 削波把说话人特征削掉一部分,相似度就稳定落在 0.34–0.48。

    削波不到 max_clip_ratio(5%)时质量门是**放行**的,所以这件事此前完全不可见。
    用 ScriptedSpeaker 走真实的门流程、假的 embedding,所以不需要模型。
    """
    speaker = ScriptedSpeaker([[0.4]], tmp_path=tmp_path)

    # 贴轨但削波比例 1.9%:过得了 5% 的质量门,却该在原因里被点出来。
    samples = (np.sin(np.linspace(0, 400.0, 16000)) * 0.6).astype(np.float32)
    samples[:300] = 1.0
    result = speaker.verify(samples)

    assert result.accepted is False
    # 差多少要说出来：0.448 和 0.05 是完全不同的两件事（条件不够好 vs 不是这个人）。
    assert "相似度 0.400" in result.reason
    assert "差 0.100" in result.reason
    assert "削波" in result.reason, "削波必须出现在原因里,否则这件事仍然不可见"
    assert "1.9%" in result.reason or "2.0%" in result.reason


def test_a_clean_but_unmatched_input_does_not_cry_clipping(tmp_path):
    """反向断言:没有削波时不许提削波。一个总在报同一句提示的诊断等于没有诊断。"""
    speaker = ScriptedSpeaker([[0.4]], tmp_path=tmp_path)
    clean = (np.sin(np.linspace(0, 400.0, 16000)) * 0.3).astype(np.float32)

    result = speaker.verify(clean)

    assert result.accepted is False
    assert "相似度 0.400" in result.reason
    assert "削波" not in result.reason


def test_input_quality_reports_peak_rms_and_clip_without_judging():
    """`input_quality` 只测不判 —— 判决仍然只在质量门与阈值那两处。"""
    import numpy as np

    from core.audio.speaker import SpeakerVerificationProvider

    provider = SpeakerVerificationProvider(model_path="nope.onnx", store_path="nope.json")
    quiet = provider.input_quality(np.full(1600, 0.001, dtype="float32"))
    assert quiet["clip"] == 0.0
    assert quiet["peak"] == pytest.approx(0.001, abs=1e-6)
    railed = provider.input_quality(np.ones(1600, dtype="float32"))
    assert railed["clip"] == 1.0
    assert railed["peak"] == 1.0
    assert provider.input_quality(np.zeros(0, dtype="float32")) == {
        "rms": 0.0,
        "clip": 0.0,
        "peak": 0.0,
        "seconds": 0.0,
        "active": 0.0,
    }


# ------------------- 控制台诊断不许消耗本人的暴力防护额度（2026-08-31 实机）


def test_a_console_diagnostic_does_not_build_a_cooldown(tmp_path):
    """**这是「说了唤醒词但根本没检测到」的根因。**

    控制台「试一句」走的是同一个 `verify()`，于是它每一次失败都算一次「连续拒绝」。
    使用者连点几下（而那时它因为另一个 bug 固定返回 0 分），第 5 次就把真实唤醒门推进了
    30 秒冷却 —— 实机日志：`声纹拒绝「你好小沃」：verification cooling down for 25.4s`，
    而唤醒漏斗里只记了 1 次拒绝（另外几次来自试一句，不在漏斗里）。

    一次本机、已鉴权、由人点出来的诊断不是暴力尝试。
    """
    clock = FakeClock()
    speaker = ScriptedSpeaker([[0.1]] * 20, tmp_path=tmp_path, clock=clock)
    speech = _speech_like(16000)

    for _ in range(8):
        speaker.verify(speech, throttle=False)

    assert speaker._rejection_streak == 0
    assert speaker._cooldown_until == 0.0
    assert speaker.gate_stats["rejected_below_threshold"] == 0, "诊断不进唤醒漏斗的统计"
    # 门本身照常工作：真实路径仍然会累计并冷却。
    for _ in range(4):
        speaker.verify(speech)
    assert speaker._rejection_streak == 4
    assert speaker._cooldown_until == 0.0
    assert speaker.verify(speech).accepted is False
    assert speaker._cooldown_until > 0.0


def test_a_console_diagnostic_still_answers_during_a_cooldown(tmp_path):
    """冷却期内诊断必须照样出分 —— 那正是最需要它的时候。

    反过来的话，人被冷却挡住、想用试一句查为什么，而试一句也只回一句「冷却中」，
    于是唯一的读数在唯一需要它的时刻消失了。诊断不放宽任何边界：它不改状态、不给准入。
    """
    clock = FakeClock()
    speaker = ScriptedSpeaker([[0.9]], tmp_path=tmp_path, clock=clock)
    speaker._cooldown_until = clock.now + 25.0

    result = speaker.verify(_speech_like(16000), throttle=False)

    assert result.accepted is True
    assert result.score == pytest.approx(0.9)
    assert speaker._cooldown_until == clock.now + 25.0, "诊断不许把冷却清掉"
    assert speaker.gate_stats["accepted"] == 0, "也不进漏斗的命中计数"


def test_a_short_utterance_rejection_says_what_to_do_about_it(tmp_path):
    """**这一条是 2026-09-02 真机验收那个症状的出口。**

    那天的相似度是 0.506 / 0.548 / 0.556 / 0.568（阈值 0.5），两次被拒 0.448 —— 余量只有
    百分之几。而使用者的观察给出了机制：

        仅使用唤醒词「你好小沃」会没有反应，且声纹过不了，
        但是「你好小沃，现在几点了」能过声纹。

    校验窗 1.5 秒里「你好小沃」只占约 0.8 秒，另一半是静音；说话人嵌入在语音不足一两秒时
    明显退化。这件事此前在拒绝原因里**一个字都没有**，于是它看起来和「不是这个人」一样。

    断言的是那句话给出了**能照着做的动作**，不是一个数字。
    """
    import numpy as np

    speaker = ScriptedSpeaker([[0.4]], tmp_path=tmp_path)
    # 1.5 秒的窗，只有开头 0.6 秒有声 —— 「只说唤醒词」的形状。
    samples = np.zeros(24000, dtype="float32")
    samples[:9600] = (np.sin(np.linspace(0, 300.0, 9600)) * 0.4).astype("float32")

    result = speaker.verify(samples)

    assert result.accepted is False
    assert "秒语音" in result.reason
    assert "连着说" in result.reason, "要给动作，不是只报一个数"


def test_a_window_that_is_mostly_speech_does_not_get_the_short_hint(tmp_path):
    """反向断言：语音够长时不许提这一条。一个总在报同一句提示的诊断等于没有诊断。"""
    import numpy as np

    speaker = ScriptedSpeaker([[0.4]], tmp_path=tmp_path)
    full = (np.sin(np.linspace(0, 900.0, 32000)) * 0.4).astype("float32")

    result = speaker.verify(full)

    assert result.accepted is False
    assert "秒语音" not in result.reason


def test_input_quality_measures_how_much_of_the_window_is_speech():
    """``active`` 只按能量比，不判「是不是人声」—— 那条判据必须是模型（core/audio/vad.py）。

    它只进拒绝原因、不参与判决，所以一个便宜的能量比在这里够用，而且不会骗人。
    """
    import numpy as np

    from core.audio.speaker import SpeakerVerificationProvider

    provider = SpeakerVerificationProvider(model_path="nope.onnx", store_path="nope.json")
    half = np.zeros(16000, dtype="float32")
    half[:8000] = (np.sin(np.linspace(0, 300.0, 8000)) * 0.5).astype("float32")

    quality = provider.input_quality(half)

    assert quality["seconds"] == pytest.approx(1.0)
    assert 0.4 <= quality["active"] <= 0.6, quality["active"]
    # 全静音时占比是 0，而不是除零。
    assert provider.input_quality(np.zeros(1600, dtype="float32"))["active"] == 0.0
