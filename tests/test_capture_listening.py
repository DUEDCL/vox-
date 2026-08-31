"""The listening phase: wake -> ASR -> recognised text (ADR 001).

The capture has two modes on one stream. Before a wake it feeds KWS; after an
**accepted** wake it feeds the streaming recognizer instead, and on an endpoint
it hands the final text to ``on_recognized`` and returns to KWS. A rejected wake
must not open the recognizer -- that would transcribe an unauthorised voice.

Evidence level: AUTO (stub KWS/verifier/ASR, no device, no model).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import AsrResult, ProviderStatus, ProviderUnavailable, SounddeviceWakeCapture
from core.audio.speaker import VerificationResult


class StubKws:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])
        self.closed = False
        #: 被喂了几次。用来断言「VAD 不闸 KWS」—— 判成非语音的块照样要喂进来。
        self.feeds = 0

    def load(self):
        return ProviderStatus(True, "stub", {"engine": "stub"})

    def create_stream(self):
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, samples, sample_rate
        self.feeds += 1
        hits, self.hits = self.hits, []
        return [(keyword, None) for keyword in hits]

    def close(self):
        self.closed = True


class StubAsr:
    """Endpoints after ``endpoint_after`` feeds, then returns ``final_text``."""

    def __init__(self, final_text="读一下 README", endpoint_after=1) -> None:
        self.final_text = final_text
        self.endpoint_after = endpoint_after
        self.fed = 0
        self.streams = 0
        self.resets = 0
        self.closed = False

    def load(self):
        return ProviderStatus(True, "stub", {"engine": "stub"})

    def create_stream(self):
        self.streams += 1
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, samples, sample_rate
        self.fed += 1
        return AsrResult(text="部分", is_endpoint=self.fed >= self.endpoint_after)

    def finalize(self, stream):
        del stream
        return self.final_text

    def reset(self, stream):
        del stream
        self.resets += 1

    def close(self):
        self.closed = True


class StubVerifier:
    def __init__(self, *, accepted=True) -> None:
        self.result = VerificationResult(
            accepted, "owner" if accepted else None, 0.91, "match" if accepted else "below"
        )
        self.speakers = ["owner"]

    def load(self):
        return ProviderStatus(True, "stub", {"dim": 192})

    def verify(self, samples, *, sample_rate=16000):
        del samples, sample_rate
        return self.result


def block(samples: int = 160, value: float = 0.2) -> np.ndarray:
    return np.full((samples, 1), value, dtype="float32")


def build(*, asr=None, recognized=None, verifier=None, require_verification=False):
    kws = StubKws(["你好问问"])
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        blocksize=160,
        require_verification=require_verification,
        verifier=verifier,
        asr_provider=asr,
        on_recognized=(recognized.append if recognized is not None else None),
    )
    capture._inference_stream = kws.create_stream()
    return capture, kws


def test_wake_only_mode_is_unchanged_without_an_asr_provider():
    woke = []
    capture, _kws = build()
    capture.on_wake = lambda keyword, score: woke.append(keyword)

    capture._callback(block(), 160, None, None)

    assert woke == ["你好问问"]
    assert capture._listening is False


def test_an_accepted_wake_switches_to_asr_and_delivers_the_text():
    asr = StubAsr("读一下 README")
    recognized: list[str] = []
    capture, _kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)
    assert capture._listening is True, "an accepted wake must open the recognizer"
    assert asr.streams == 1

    capture._callback(block(), 160, None, None)

    assert recognized == ["读一下 README"]
    assert capture._listening is False, "an endpoint returns the capture to KWS"
    assert asr.resets == 1


def test_audio_during_listening_never_reaches_kws():
    asr = StubAsr(endpoint_after=99)
    recognized: list[str] = []
    capture, kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)
    kws.hits = ["你好问问"]
    capture._callback(block(), 160, None, None)

    # Still queued for KWS, because the block went to the recognizer instead.
    assert kws.hits == ["你好问问"]
    # One feed, not two: the block the wake fired on opens the recognizer but is
    # not itself transcribed, so the wake word does not land in the request.
    assert asr.fed == 1
    assert recognized == []


def test_a_rejected_wake_never_opens_the_recognizer():
    asr = StubAsr()
    recognized: list[str] = []
    capture, _kws = build(
        asr=asr,
        recognized=recognized,
        verifier=StubVerifier(accepted=False),
        require_verification=True,
    )
    capture.on_reject = lambda *a: None

    capture._callback(block(), 160, None, None)

    assert capture._listening is False
    assert asr.streams == 0, "transcribing an unauthorised voice is the failure"
    assert recognized == []


def test_an_empty_transcription_is_not_delivered():
    """空转写不开启回合。

    **2026-08-30 改了它的后半段。** 此前这里还断言 `_listening is False`，也就是「一次空
    转写立刻结束聆听」。那个行为是使用者报的缺陷本身：唤醒之后停顿两秒（端点检测在一个字
    都没解出来时 2.4 秒就报一次），聆听就结束了，而状态机还停在 LISTENING —— 球显示
    「在听」，采集却已经回到 KWS 模式，于是后面说的话不再被转写。

    现在空转写在宽限期内换一条新的识别流继续听。**「不开启回合」这一条不变**，
    变的是「还听不听」。
    """
    asr = StubAsr(final_text="   ")
    recognized: list[str] = []
    capture, _kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)
    capture._callback(block(), 160, None, None)

    assert recognized == [], "silence must not start a turn"
    assert capture._listening is True, "宽限期内还要继续听 —— 人想两秒再开口是正常的"
    assert capture.listen_restarts == 1


def test_listening_expires_after_the_grace_period_and_says_so():
    """宽限期用完了就结束聆听，而且**必须通知出去**。

    不通知的那个版本是缺陷的核心：状态机停在 LISTENING、球一直显示「在听」，而采集早就
    回到唤醒模式了。一个说谎的状态比一个「已经不听了」的状态糟得多。
    """
    asr = StubAsr(final_text="")
    recognized: list[str] = []
    expired: list[float] = []
    capture, _kws = build(asr=asr, recognized=recognized)
    capture.on_listen_expired = expired.append

    capture._callback(block(), 160, None, None)  # 唤醒 -> 开识别器
    capture.listen_grace_s = 0.0                 # 宽限期已经用完
    capture._callback(block(), 160, None, None)  # 端点 + 空转写

    assert capture._listening is False
    assert capture.listen_expiries == 1
    assert expired and expired[0] >= 0.0
    assert recognized == []


def test_a_raising_expiry_sink_does_not_take_the_audio_thread_down():
    asr = StubAsr(final_text="")
    capture, _kws = build(asr=asr, recognized=[])

    def boom(_seconds):
        raise RuntimeError("sink 坏了")

    capture.on_listen_expired = boom
    capture._callback(block(), 160, None, None)
    capture.listen_grace_s = 0.0
    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture._listening is False


class TrackingKws(StubKws):
    def __init__(
        self, hits=None, *, fail_load=False, fail_create=False, fail_feed=False, fail_close=False
    ) -> None:
        super().__init__(hits)
        self.fail_load = fail_load
        self.fail_create = fail_create
        self.fail_feed = fail_feed
        self.fail_close = fail_close
        self.loads = 0
        self.streams = 0
        self.closes = 0

    def load(self):
        self.loads += 1
        if self.fail_load:
            return ProviderStatus(False, "stub", {"reason": "kws unavailable"})
        return super().load()

    def create_stream(self):
        self.streams += 1
        if self.fail_create:
            raise RuntimeError("kws stream failed")
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        if self.fail_feed:
            self.fail_feed = False
            raise RuntimeError("private kws detail")
        return super().feed(stream, samples, sample_rate)

    def close(self):
        self.closes += 1
        super().close()
        if self.fail_close:
            raise RuntimeError("kws close failed")


class TrackingAsr(StubAsr):
    def __init__(
        self,
        *args,
        fail_load=False,
        fail_feed=False,
        fail_finalize=False,
        fail_reset=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fail_load = fail_load
        self.fail_feed = fail_feed
        self.fail_finalize = fail_finalize
        self.fail_reset = fail_reset
        self.loads = 0
        self.closes = 0

    def load(self):
        self.loads += 1
        if self.fail_load:
            return ProviderStatus(False, "stub", {"reason": "asr unavailable"})
        return super().load()

    def feed(self, stream, samples, sample_rate=16000):
        if self.fail_feed:
            raise RuntimeError("private asr feed detail")
        return super().feed(stream, samples, sample_rate)

    def finalize(self, stream):
        if self.fail_finalize:
            raise ValueError("private transcript detail")
        return super().finalize(stream)

    def reset(self, stream):
        super().reset(stream)
        if self.fail_reset:
            raise RuntimeError("private asr reset detail")

    def close(self):
        self.closes += 1
        super().close()


class FakeInputStream:
    def __init__(self, *, fail_start=False, fail_stop=False, fail_close=False, **kwargs) -> None:
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.kwargs = kwargs
        self.starts = 0
        self.stops = 0
        self.closes = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("device start failed")

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("device stop failed")

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("device close failed")


def install_stream_factory(monkeypatch, streams):
    pending = list(streams)

    def factory(**kwargs):
        stream = pending.pop(0)
        stream.kwargs = kwargs
        return stream

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=factory))


def test_a_failed_device_start_rolls_back_and_can_retry(monkeypatch):
    first = FakeInputStream(fail_start=True)
    second = FakeInputStream()
    install_stream_factory(monkeypatch, [first, second])
    kws = TrackingKws()
    asr = TrackingAsr()
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        require_verification=False,
        asr_provider=asr,
        on_recognized=lambda _text: None,
    )

    with pytest.raises(RuntimeError, match="device start failed"):
        capture.start()

    assert capture._stream is None
    assert capture._inference_stream is None
    assert capture._asr_stream is None
    assert capture._listening is False
    assert first.stops == 1
    assert first.closes == 1
    assert kws.closes == 1
    assert asr.closes == 1

    capture.start()
    assert capture._stream is second
    assert capture._inference_stream is not None
    assert second.starts == 1
    assert kws.loads == 2
    assert asr.loads == 2


@pytest.mark.parametrize("failure", ["kws-load", "kws-stream", "asr-load"])
def test_provider_start_failures_leave_no_partial_state(monkeypatch, failure):
    stream = FakeInputStream()
    install_stream_factory(monkeypatch, [stream])
    kws = TrackingKws(fail_load=failure == "kws-load", fail_create=failure == "kws-stream")
    asr = TrackingAsr(fail_load=failure == "asr-load")
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        require_verification=False,
        asr_provider=asr,
        on_recognized=lambda _text: None,
    )

    with pytest.raises((ProviderUnavailable, RuntimeError)):
        capture.start()

    assert capture._stream is None
    assert capture._inference_stream is None
    assert capture._asr_stream is None
    assert capture._listening is False
    assert kws.closes == 1
    assert asr.closes == (1 if failure == "asr-load" else 0)
    assert stream.starts == 0


def test_kws_callback_failure_is_isolated_and_future_audio_can_recover():
    woke = []
    kws = TrackingKws(["你好问问"], fail_feed=True)
    capture = SounddeviceWakeCapture(kws, lambda *args: woke.append(args), require_verification=False)
    capture._keyword_provider_loaded = True
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == "RuntimeError"
    assert "private kws detail" not in capture.last_callback_error
    assert capture._inference_stream is not None

    capture._callback(block(), 160, None, None)
    assert woke == [("你好问问", None)]


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [("feed", "RuntimeError"), ("finalize", "ValueError"), ("reset", "RuntimeError")],
)
def test_asr_callback_failures_return_to_kws_without_leaking(failure, error_type):
    asr = TrackingAsr(
        fail_feed=failure == "feed",
        fail_finalize=failure == "finalize",
        fail_reset=failure == "reset",
    )
    recognized = []
    capture, kws = build(asr=asr, recognized=recognized)
    capture._keyword_provider_loaded = True

    capture._callback(block(), 160, None, None)
    assert capture._listening is True

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == error_type
    assert capture._listening is False
    assert capture._asr_stream is None
    assert asr.resets == 1
    assert recognized == []
    kws.hits = ["你好问问"]
    capture._callback(block(), 160, None, None)
    assert capture._listening is True



def test_on_wake_failure_is_isolated_from_the_audio_thread():
    kws = TrackingKws(["你好问问"])

    def fail_callback(_keyword, _score):
        raise OSError("wake consumer leaked a path")

    capture = SounddeviceWakeCapture(kws, fail_callback, require_verification=False)
    capture._keyword_provider_loaded = True
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == "OSError"
    assert capture._listening is False
    assert capture._inference_stream is not None


def test_on_reject_failure_is_isolated_and_never_becomes_a_wake():
    woke = []
    kws = TrackingKws(["你好问问"])

    def fail_callback(*_args):
        raise PermissionError("rejection consumer detail")

    capture = SounddeviceWakeCapture(
        kws,
        lambda *args: woke.append(args),
        verifier=StubVerifier(accepted=False),
        on_reject=fail_callback,
    )
    capture._keyword_provider_loaded = True
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert woke == []
    assert capture.callback_errors == 1
    assert capture.last_callback_error == "PermissionError"
    assert capture._listening is False


def test_on_recognized_failure_is_isolated_after_asr_state_is_cleared():
    asr = TrackingAsr("敏感识别文本")

    def fail_callback(_text):
        raise LookupError("consumer included sensitive content")

    capture, _kws = build(asr=asr, recognized=[])
    capture.on_recognized = fail_callback
    capture._keyword_provider_loaded = True
    capture._callback(block(), 160, None, None)

    capture._callback(block(), 160, None, None)

    assert capture.callback_errors == 1
    assert capture.last_callback_error == "LookupError"
    assert capture._listening is False
    assert capture._asr_stream is None
    assert asr.resets == 1


def test_stop_is_best_effort_and_second_stop_has_no_side_effects():
    kws = TrackingKws(fail_close=True)
    asr = TrackingAsr()
    stream = FakeInputStream(fail_stop=True, fail_close=True)
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        require_verification=False,
        asr_provider=asr,
        on_recognized=lambda _text: None,
    )
    capture._stream = stream
    capture._keyword_provider_loaded = True
    capture._asr_provider_loaded = True
    capture._inference_stream = kws.create_stream()
    capture._asr_stream = asr.create_stream()
    capture._listening = True
    capture._ring.write(np.ones(10, dtype="float32"))

    capture.stop()

    assert capture._stream is None
    assert capture._inference_stream is None
    assert capture._asr_stream is None
    assert capture._listening is False
    assert len(capture._ring) == 0
    assert stream.stops == 1
    assert stream.closes == 1
    assert asr.resets == 1
    assert kws.closes == 1
    assert asr.closes == 1

    capture.stop()
    assert stream.stops == 1
    assert stream.closes == 1
    assert asr.resets == 1
    assert kws.closes == 1
    assert asr.closes == 1


# ---------------------------------------------------------------- 死麦克风检测
#
# 这一组钉死的是本项目查了好几轮才找到的那个缺陷:Windows 上一个被静音/被隐私设置
# 拒绝/根本不在用的输入设备**不报错** —— 流照常打开、回调照常以正确速率触发、
# 每块样本全是零。于是 KWS 永远不命中,而每一层都报告自己健康。
#
# 实测(2026-08-29 本机):默认设备 `麦克风阵列 (Realtek(R) Audio)` 1.2 秒采集
# peak=0.00003,同一时刻耳机设备 peak=0.027。前者是数值噪声不是房间。当时的表现是
# 「自定义唤醒词唤不醒」,于是词表、音素、KWS 阈值、声纹阈值被逐个怀疑 —— 没有一层坏。


def test_a_silent_device_is_reported_once_after_the_grace_period():
    """全零输入必须变成一次明确报告,而不是无限沉默。

    只报一次:一个死设备每 100 ms 喊一遍毫无信息量,而回调线程上的重复调用是真实成本。
    """
    reports = []
    kws = StubKws([])
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        blocksize=1600,
        require_verification=False,
        on_input_silent=reports.append,
        silent_grace_s=1.0,
    )
    capture._inference_stream = kws.create_stream()
    # 1600 样本 @16 kHz = 100 ms/块,所以 10 块正好到 1 秒的宽限线。
    for _ in range(12):
        capture._callback(np.zeros((1600, 1), dtype="float32"), 1600, None, None)
    assert len(reports) == 1, "应当恰好报一次"
    assert reports[0]["peak"] == 0.0
    assert reports[0]["device"] == "(系统默认)"
    assert capture.input_silent is True


def test_a_live_device_is_never_reported_even_at_a_low_noise_floor():
    """一个正常安静房间的噪声底不该被当成死设备。

    判据用 peak 而不是 RMS 正是为了这条:安静房间的 RMS 很低,但 peak 有噪声底。
    这里的 0.02 取自实测的活设备(耳机 peak=0.027)。
    """
    reports = []
    kws = StubKws([])
    capture = SounddeviceWakeCapture(
        kws, lambda *a: None, blocksize=1600, require_verification=False,
        on_input_silent=reports.append, silent_grace_s=1.0,
    )
    capture._inference_stream = kws.create_stream()
    for _ in range(30):
        capture._callback(np.full((1600, 1), 0.02, dtype="float32"), 1600, None, None)
    assert reports == []
    assert capture.input_silent is False
    assert capture.input_peak == pytest.approx(0.02)


def test_nothing_is_reported_before_the_grace_period_elapses():
    """启动瞬间的几块静音不算死设备 —— 设备起来要时间,报早了是假警报。"""
    reports = []
    kws = StubKws([])
    capture = SounddeviceWakeCapture(
        kws, lambda *a: None, blocksize=1600, require_verification=False,
        on_input_silent=reports.append, silent_grace_s=4.0,
    )
    capture._inference_stream = kws.create_stream()
    for _ in range(20):  # 2 秒,不到 4 秒宽限
        capture._callback(np.zeros((1600, 1), dtype="float32"), 1600, None, None)
    assert reports == []
    assert capture.input_silent is False


def test_a_raising_silence_callback_is_counted_not_propagated():
    """报告用的回调抛异常不能把音频线程带走 —— 它和其他 sink 同一个姿态。"""
    kws = StubKws([])

    def boom(_details):
        raise RuntimeError("sink 坏了")

    capture = SounddeviceWakeCapture(
        kws, lambda *a: None, blocksize=1600, require_verification=False,
        on_input_silent=boom, silent_grace_s=1.0,
    )
    capture._inference_stream = kws.create_stream()
    for _ in range(12):
        capture._callback(np.zeros((1600, 1), dtype="float32"), 1600, None, None)
    assert capture.callback_errors == 1
    assert capture.input_silent is True


# ------------------------------------------------------- 输出静音窗（确认音期间）


def test_the_ack_window_keeps_the_confirmation_out_of_the_transcript():
    """使用者 2026-08-30 报的「**有几率**在唤醒后不能进行后续的对话」。

    根因：唤醒命中之后识别器立刻开着，而确认音从扬声器出来、被同一支麦克风采回去。
    那 0.8–1.6 秒放完就是静音，端点正好在那时触发 —— 于是这一轮的「请求」是确认音自己
    （出厂那四句是**按能被 ASR 识别回原文**挑的，所以特别容易劫持），或者是一段空转写
    然后回 KWS。两种都表现为「唤醒了但没有后文」。

    静音窗必须让那几块**根本不到识别器**。喂静音不行：识别器内部的时间照样在走，
    端点照样会触发。
    """
    asr = StubAsr(endpoint_after=1)
    recognized: list[str] = []
    capture, _kws = build(asr=asr, recognized=recognized)

    capture._callback(block(), 160, None, None)  # 唤醒 -> 开识别器
    assert capture._listening is True

    capture.mute_for(5.0)  # 确认音开始播
    for _ in range(8):
        capture._callback(block(), 160, None, None)

    assert asr.fed == 0, "确认音那几块不许进识别器"
    assert recognized == [], "端点不许在确认音上触发"
    assert capture.muted_blocks == 8
    assert capture._listening is True, "静音窗只是丢音频,不该退出聆听"

    capture.unmute()  # 播完收窗,真正的人声开始
    capture._callback(block(), 160, None, None)

    assert recognized == ["读一下 README"]


def test_a_shorter_window_replaces_a_longer_one():
    """``mute_for`` 是**赋值**不是取大值。

    调用方的用法是「播放前压一个够长的上限，阻塞播放返回后再压一个短尾巴」。如果第二次
    调用不能把窗口收回来，每次唤醒都会白聋掉上限那么久 —— 那就把一个偶发的缺陷换成了
    一个必然的缺陷。
    """
    capture, _kws = build()
    capture.mute_for(60.0)
    assert capture.muted is True
    capture.mute_for(0.0)
    assert capture.muted is False


def test_muted_blocks_do_not_count_as_evidence_that_the_microphone_is_alive():
    """静音窗里听到的是**我们自己的扬声器**。拿它去证明麦克风活着是假证据，
    所以死麦克风检测在这几块上完全不跑。"""
    kws = StubKws([])
    capture = SounddeviceWakeCapture(
        kws, lambda *a: None, blocksize=1600, require_verification=False,
    )
    capture._inference_stream = kws.create_stream()
    capture.mute_for(60.0)
    for _ in range(10):
        capture._callback(np.full((1600, 1), 0.9, dtype="float32"), 1600, None, None)

    assert capture.input_blocks == 0
    assert capture.input_peak == 0.0
    assert capture.muted_blocks == 10


# ------------------- VAD：让增益只在语音上适应（2026-08-31 的 fail-open 正解）


class StubVad:
    """按脚本回答「是不是语音」。真 VAD 的形状见 core/audio/vad.SileroSpeechGate。"""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.asked = 0
        self.segments: list[bool] = []

    def __call__(self, samples):
        del samples
        self.asked += 1
        return self.answers[min(self.asked - 1, len(self.answers) - 1)]

    def has_speech(self, samples):
        del samples
        return self.segments.pop(0) if self.segments else True


class StubGain:
    def __init__(self) -> None:
        self.calls: list[bool | None] = []

    def apply(self, block, *, is_speech=None):
        self.calls.append(is_speech)
        return block


def test_the_vad_verdict_reaches_the_gain_but_never_gates_kws():
    """两件事一起钉住：

    1. VAD 的答案要**传给增益**（否则底噪会把增益抬上去 —— 那就是那次 fail-open）；
    2. VAD **不闸 KWS**。KWS 是流式解码器，喂一条被切碎的流可能反而降低命中率，而命中率
       正是要保住的东西。所以判成「不是语音」的块照样喂给 KWS。
    """
    vad = StubVad([False, False])
    gain = StubGain()
    kws = StubKws([])
    capture = SounddeviceWakeCapture(
        kws, lambda *a: None, blocksize=160, require_verification=False,
        speech_gate=vad, auto_gain=gain,
    )
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)
    capture._callback(block(), 160, None, None)

    assert gain.calls == [False, False], "VAD 的答案必须传下去"
    assert vad.asked == 2
    assert capture.speech_blocks == 0
    # KWS 仍然被喂了两次 —— 断言的是「没有被 VAD 挡掉」。
    assert kws.feeds == 2


def test_speech_blocks_counts_what_the_vad_accepted():
    vad = StubVad([True, False, True])
    kws = StubKws([])
    capture = SounddeviceWakeCapture(
        kws, lambda *a: None, blocksize=160, require_verification=False, speech_gate=vad,
    )
    capture._inference_stream = kws.create_stream()

    for _ in range(3):
        capture._callback(block(), 160, None, None)

    assert capture.speech_blocks == 2


def test_has_speech_falls_through_to_true_without_a_vad():
    """没接 VAD 时放行。这一层是鲁棒性增强，不是安全边界 —— 安全边界是声纹门，
    而一个读不到模型的 VAD 不该让注册和试一句整体不可用。"""
    kws = StubKws([])
    capture = SounddeviceWakeCapture(kws, lambda *a: None, blocksize=160, require_verification=False)

    assert capture.has_speech(np.zeros(16000, dtype="float32")) is True
