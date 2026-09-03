"""云端识别（`core/audio/asr_cloud.py`）。

**一律用注入的 transport，一次网络都不打。** 真机验收在 `.vox-ref/asr_cloud_probe.py` 与
`asr_cloud_stream_probe.py`，读数记在 `docs/research/prototype-results.md`。

这里钉死的四条不变式，每一条都对应一个已经踩过或必然会踩的坑：

1. **请求形状**：`data:` 前缀 + `parameters.format` 都在 —— 少哪一个都实测过（500 / 400）；
2. **音频不落盘、请求里不带 key 之外的任何凭据**；
3. **端点由本机 VAD 判，HTTP 不在 feed 里阻塞** —— feed 返回后请求可能还在路上；
4. **停顿切开的两段会被拼回去**，且非最后一段的句末标点被去掉。
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
import wave
from urllib.error import HTTPError, URLError

import numpy as np
import pytest

from core.audio.asr_cloud import (
    DEFAULT_ENDPOINT,
    MAX_ATTEMPTS,
    MAX_PARTS,
    DashScopeAsrError,
    DashScopeAsrProvider,
    _join,
    _text_of,
    to_wav_bytes,
)

SAMPLE_RATE = 16000
BLOCK = 1600


class Recorder:
    """记下每一次 POST 并按脚本回。``block`` 让「请求还在路上」变成可控状态。"""

    def __init__(self, *texts: str, block: bool = False) -> None:
        self.texts = list(texts) or ["好的"]
        self.calls: list[dict] = []
        self.gate = threading.Event()
        if not block:
            self.gate.set()

    def post(self, url: str, payload: dict) -> dict:
        self.gate.wait(5.0)
        self.calls.append({"url": url, "payload": payload})
        index = min(len(self.calls) - 1, len(self.texts) - 1)
        text = self.texts[index]
        return {"output": {"output": {"sentence": {"text": text}}}}

    def release(self) -> None:
        self.gate.set()


class Boom:
    def post(self, url: str, payload: dict) -> dict:
        raise DashScopeAsrError("https://example.invalid HTTP 401 —— 密钥不对")


class Deferred:
    """每一次 POST 都停在一个自己的闸门上，由测试逐个放行。

    存在的理由：**续说判定看的是「文本回来那一刻人有没有又在说了」**，而真机上那一刻在
    3–5 秒之后。一个立刻返回的 transport 会让第一段在使用者还没来得及接着说的时候就落定，
    于是这条逻辑测不到 —— 那正是这个测试第一版给出假阳性的原因。
    """

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts) or ["好的"]
        self.gates: list[threading.Event] = []
        self.calls: list[dict] = []
        self.released = 0

    def post(self, url: str, payload: dict) -> dict:
        gate = threading.Event()
        self.gates.append(gate)
        gate.wait(5.0)
        index = len(self.calls)
        self.calls.append(payload)
        return {
            "output": {
                "output": {"sentence": {"text": self.texts[min(index, len(self.texts) - 1)]}}
            }
        }

    def release_next(self, timeout: float = 2.0) -> bool:
        """放行下一个在等的请求。返回 False = 根本没有请求发出去。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.gates) > self.released:
                self.gates[self.released].set()
                self.released += 1
                return True
            time.sleep(0.005)
        return False


def speech(seconds: float, *, level: float = 0.4) -> np.ndarray:
    """一段确定性的「像语音」的信号。正弦 + 抖动 —— 纯正弦过不了 Silero。"""
    count = int(seconds * SAMPLE_RATE)
    time_axis = np.arange(count, dtype="float32") / SAMPLE_RATE
    tone = np.sin(2 * np.pi * 180 * time_axis) * np.sin(2 * np.pi * 3.5 * time_axis)
    rng = np.random.default_rng(7)
    return (tone * level + rng.normal(0, 0.02, count).astype("float32")).astype("float32")


class FakeGate:
    """按调用序列回「有没有语音」。**测试里不用真 VAD** —— 一个 onnx 模型的判决不是这一层
    的被测对象，而且它在 CI 上可能根本不在盘上。
    """

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.index = 0
        self.resets = 0

    def __call__(self, block) -> bool:  # noqa: ANN001
        mark = self.pattern[min(self.index, len(self.pattern) - 1)]
        self.index += 1
        return mark == "S"

    def reset(self) -> None:
        self.resets += 1


class Switch:
    """一个能被测试直接翻的 VAD 替身。

    ``FakeGate`` 那种固定序列在「喂多少块由被测逻辑自己决定」的测试里会和意图错位 ——
    ``until_pending`` 要喂几块取决于阈值，而一条写死的序列不知道这件事。
    """

    def __init__(self, *, speaking: bool = False) -> None:
        self.speaking = speaking
        self.resets = 0

    def __call__(self, block) -> bool:  # noqa: ANN001
        return self.speaking

    def reset(self) -> None:
        self.resets += 1


def drive(provider: DashScopeAsrProvider, stream, pattern: str):
    """按 pattern 喂块，返回每一块的 AsrResult。一个字符 = 一块 = 100 ms。"""
    results = []
    for _ in pattern:
        results.append(provider.feed(stream, speech(0.1), SAMPLE_RATE))
    return results


def pump(provider: DashScopeAsrProvider, stream, *, blocks: int = 60):
    """一直喂静音直到端点到达。返回最终文本（没有端点就返回 None）。"""
    for _ in range(blocks):
        result = provider.feed(stream, speech(0.1), SAMPLE_RATE)
        if result.is_endpoint:
            return provider.finalize(stream) if result.text else ""
    return None


def until_pending(provider: DashScopeAsrProvider, stream, *, blocks: int = 30) -> bool:
    """喂静音直到「有一段发出去了」。续说的第二段要先攒够静音才会被发。"""
    for _ in range(blocks):
        provider.feed(stream, speech(0.1), SAMPLE_RATE)
        if stream.pending is not None:
            return True
    return False


# -- wav 编码 ---------------------------------------------------------------


def test_to_wav_bytes_is_16k_mono_16bit():
    data = to_wav_bytes(speech(0.5))
    with wave.open(io.BytesIO(data)) as handle:
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == int(0.5 * SAMPLE_RATE)


def test_to_wav_bytes_clips_instead_of_wrapping():
    """越界样本被钳到 ±1.0。不钳的话 int16 会绕成反相噪声 —— 削波变撕裂声。"""
    data = to_wav_bytes(np.array([2.0, -2.0, 0.0], dtype="float32"))
    with wave.open(io.BytesIO(data)) as handle:
        pcm = np.frombuffer(handle.readframes(3), dtype="<i2")
    assert pcm[0] == 32767
    assert pcm[1] == -32767


def test_wav_is_never_written_to_disk(tmp_path, monkeypatch):
    """整条路上一次 ``open(..., 'wb')`` 都没有。红线 1 的「音频不留痕」是这一条。"""
    monkeypatch.chdir(tmp_path)
    provider = DashScopeAsrProvider(transport=Recorder("在的"))
    provider._transcribe(speech(0.4))
    assert list(tmp_path.iterdir()) == []


# -- 请求形状（每一条都是实测出来的失败）------------------------------------


def test_request_carries_data_uri_prefix():
    """裸 base64 实测回 500 `Cannot run program "/usr/bin/wget"` —— 服务端当 URL 下载。"""
    recorder = Recorder("你好")
    provider = DashScopeAsrProvider(transport=recorder)
    provider._transcribe(speech(0.4))
    content = recorder.calls[0]["payload"]["input"]["messages"][0]["content"][0]
    assert content["type"] == "input_audio"
    assert content["input_audio"]["data"].startswith("data:audio/wav;base64,")


def test_request_carries_format_and_sample_rate():
    """缺 format 实测回 400 `UNSUPPORTED_FORMAT: format is empty` —— 使用者报的那个 400。"""
    recorder = Recorder("你好")
    DashScopeAsrProvider(transport=recorder)._transcribe(speech(0.4))
    parameters = recorder.calls[0]["payload"]["parameters"]
    assert parameters["format"] == "wav"
    assert parameters["sample_rate"] == "16000"


def test_default_endpoint_is_native_not_compatible_mode():
    """`compatible-mode/v1` 没有地方放 `parameters.format`，所以那条路恒 400。"""
    assert "multimodal-generation/generation" in DEFAULT_ENDPOINT
    assert "compatible-mode" not in DEFAULT_ENDPOINT


def test_language_is_omitted_unless_asked():
    """默认不填 language：填死 zh 会把中英混说里的英文词硬转成音近的汉字。"""
    recorder = Recorder("hello")
    DashScopeAsrProvider(transport=recorder)._transcribe(speech(0.4))
    assert "language" not in recorder.calls[0]["payload"]["parameters"]
    recorder2 = Recorder("hello")
    DashScopeAsrProvider(transport=recorder2, language="zh")._transcribe(speech(0.4))
    assert recorder2.calls[0]["payload"]["parameters"]["language"] == "zh"


def test_audio_round_trips_through_base64():
    """请求体里那段 base64 解回来就是原始 wav —— 没有第二次编码、没有截断。"""
    recorder = Recorder("在")
    provider = DashScopeAsrProvider(transport=recorder)
    samples = speech(0.4)
    provider._transcribe(samples)
    data = recorder.calls[0]["payload"]["input"]["messages"][0]["content"][0]["input_audio"]["data"]
    raw = base64.b64decode(data.split(",", 1)[1])
    assert raw == to_wav_bytes(samples)


# -- 可用性与失败姿态 --------------------------------------------------------


def test_available_needs_the_key_and_does_not_touch_the_network(monkeypatch):
    monkeypatch.delenv("VOX_ASR_KEY", raising=False)
    provider = DashScopeAsrProvider()
    assert provider.available is False
    status = provider.load()
    assert status.available is False
    assert "VOX_ASR_KEY" in status.details["reason"]
    monkeypatch.setenv("VOX_ASR_KEY", "sk-not-a-real-key")
    assert DashScopeAsrProvider().available is True


def test_load_reports_endpoint_host_only():
    """报主机名而不是完整 URL：query 里可能被人塞过东西。"""
    provider = DashScopeAsrProvider(transport=Recorder())
    status = provider.load()
    assert status.details["endpoint"] == "https://dashscope.aliyuncs.com"
    assert "/api/v1" not in status.details["endpoint"]


def test_post_failure_never_echoes_the_key(monkeypatch):
    monkeypatch.setenv("VOX_ASR_KEY", "sk-secret-value-do-not-leak")
    provider = DashScopeAsrProvider()

    def explode(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr("core.audio.asr_cloud.urlopen", explode)
    with pytest.raises(Exception) as caught:  # noqa: PT011 - 只关心文本里有没有 key
        provider._transcribe(speech(0.4))
    assert "sk-secret-value-do-not-leak" not in str(caught.value)


def test_transcribe_failure_becomes_an_empty_endpoint_not_an_exception():
    """一次网络失败要变成读数，不是从音频回调里抛出去。"""
    provider = DashScopeAsrProvider(transport=Boom(), silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS......")
    results = drive(provider, stream, "SSSS......")
    for _ in range(40):
        results.append(provider.feed(stream, speech(0.1), SAMPLE_RATE))
    ended = [item for item in results if item.is_endpoint]
    assert ended and ended[0].text == ""
    assert provider.failures == 1
    assert "401" in provider.last_error


# -- 端点判定与不阻塞 --------------------------------------------------------


def test_feed_does_not_block_on_the_request():
    """``feed`` 返回之后请求还在路上。**这是这一层存在的全部理由** —— 它跑在音频回调上。"""
    recorder = Recorder("你好", block=True)
    provider = DashScopeAsrProvider(transport=recorder, silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS......")
    results = drive(provider, stream, "SSSS....")
    assert all(not item.is_endpoint for item in results)
    assert stream.pending is not None  # 已经发了，还没回
    recorder.release()
    for _ in range(30):
        result = provider.feed(stream, speech(0.1), SAMPLE_RATE)
        if result.is_endpoint:
            assert result.text == "你好"
            assert provider.finalize(stream) == "你好"
            return
    pytest.fail("请求回来之后 feed 应该报一次端点")


def test_audio_during_the_wait_is_kept_not_dropped():
    """等云端那 3–5 秒里说的话必须还在缓冲里。丢掉它的表现是「它总听半句」。"""
    recorder = Recorder("前半句", block=True)
    provider = DashScopeAsrProvider(transport=recorder, silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS...SSSSSS")
    drive(provider, stream, "SSSS...")
    assert stream.pending is not None
    before = stream.frames
    drive(provider, stream, "SSSSSS")
    assert stream.frames > before
    assert stream.speech_frames > 0


def test_short_noise_is_not_sent():
    """一次咳嗽过得了 VAD 的 0.25 秒门，但不值得一次计费和一次 4 秒等待。"""
    recorder = Recorder("不该被调用")
    provider = DashScopeAsrProvider(
        transport=recorder, silence_s=0.3, min_utterance_s=1.0, max_utterance_s=2.0
    )
    stream = provider.create_stream()
    stream.gate = FakeGate("SS" + "." * 40)
    drive(provider, stream, "SS" + "." * 40)
    assert recorder.calls == []
    assert provider.requests == 0


def test_overflow_sends_without_waiting_for_silence():
    """念清单、读地址那种没有句末的长句不该卡死整轮。"""
    recorder = Recorder("一二三四五")
    provider = DashScopeAsrProvider(
        transport=recorder, silence_s=5.0, min_utterance_s=0.2, max_utterance_s=1.0
    )
    stream = provider.create_stream()
    stream.gate = FakeGate("S" * 40)
    drive(provider, stream, "S" * 20)
    assert recorder.calls, "到了 max_utterance_s 就该发，不等静音"


def test_vad_failure_falls_back_to_treating_everything_as_speech():
    """VAD 起不来时恒真：退化成「说满 max_utterance_s 才发」，比一块都不发好。"""
    recorder = Recorder("还在")
    provider = DashScopeAsrProvider(
        transport=recorder, silence_s=0.3, min_utterance_s=0.2, max_utterance_s=1.0
    )
    stream = provider.create_stream()

    class Angry:
        def __call__(self, block):  # noqa: ANN001
            raise RuntimeError("onnx 没了")

        def reset(self) -> None:
            return None

    stream.gate = Angry()
    drive(provider, stream, "S" * 20)
    assert recorder.calls


# -- 续说拼接 ---------------------------------------------------------------


def test_pause_split_segments_are_joined():
    """「帮我打开……网易云音乐」中间那一下停顿是句中，不是句末。

    时序是刻意排的：发出第一段 → **在它回来之前**把后半句喂进去 → 才放行第一段。
    真机上第一段要 3–5 秒才回来，那几秒里人已经在说后半句了。
    """
    recorder = Deferred("帮我打开。", "网易云音乐。")
    provider = DashScopeAsrProvider(transport=recorder, silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS...SSSSSS" + "." * 80)
    drive(provider, stream, "SSSS...")
    assert stream.pending is not None, "0.3 秒静音之后该把第一段发出去"
    drive(provider, stream, "SSSSSS")  # 人接着说了 —— 这一段必须让第一段变成「句中」
    assert recorder.release_next()
    drive(provider, stream, "..")  # 让 _poll 看到第一段回来了
    assert stream.parts == ["帮我打开。"], "第一段该被攒起来而不是当句末交出去"
    assert provider.continuations == 1
    assert until_pending(provider, stream), "后半句攒够静音之后该被发出去"
    assert recorder.release_next()
    assert pump(provider, stream) == "帮我打开网易云音乐。"


def test_join_strips_only_the_non_final_punctuation():
    assert _join(["帮我打开。", "网易云音乐。"]) == "帮我打开网易云音乐。"
    assert _join(["一", "二", "三？"]) == "一二三？"
    assert _join(["", "  ", "只有这一段"]) == "只有这一段"
    assert _join([]) == ""


def test_continuations_are_capped():
    """一直说不停的话到 MAX_PARTS 就交出去，不无限攒着让人等不到回答。"""
    recorder = Deferred(*[f"第{index}段" for index in range(1, 8)])
    provider = DashScopeAsrProvider(transport=recorder, silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    gate = Switch()
    stream.gate = gate
    delivered = ""
    for _ in range(MAX_PARTS + 3):
        gate.speaking = True
        drive(provider, stream, "SSSS")
        gate.speaking = False
        if not until_pending(provider, stream):
            break
        gate.speaking = True
        drive(provider, stream, "SSSS")  # 每一段都有后续语音 → 每一段都该算「句中」
        assert recorder.release_next()
        result = None
        for _ in range(20):
            result = provider.feed(stream, speech(0.1), SAMPLE_RATE)
            if result.is_endpoint:
                break
        if result is not None and result.is_endpoint and result.text:
            delivered = provider.finalize(stream)
            break
    assert delivered, "到了上限就该把手上的交出去"
    assert provider.continuations == MAX_PARTS - 1
    assert stream.parts == []


def test_empty_result_after_a_part_still_delivers_what_was_heard():
    """前面有段、这一段空：把前面的交出去，别因为一段静音把已经听到的话丢了。"""
    recorder = Deferred("说了一半", "")
    provider = DashScopeAsrProvider(transport=recorder, silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS...SSSS" + "." * 80)
    drive(provider, stream, "SSSS...")
    drive(provider, stream, "SSSS")
    assert recorder.release_next()
    drive(provider, stream, "..")
    assert stream.parts == ["说了一半"]
    assert until_pending(provider, stream)
    assert recorder.release_next()
    assert pump(provider, stream) == "说了一半"


def test_failure_drops_pending_parts():
    """半句话派给 agent 比不派更糟。"""
    provider = DashScopeAsrProvider(transport=Boom(), silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS......")
    stream.parts.append("前面听到的")
    for _ in range(50):
        result = provider.feed(stream, speech(0.1), SAMPLE_RATE)
        if result.is_endpoint:
            break
    assert stream.parts == []


# -- 暂时性失败的重发 --------------------------------------------------------


class Flaky:
    """前 ``fail`` 次抛给定的异常，之后成功。"""

    def __init__(self, error: Exception, *, fail: int = 1, text: str = "救回来了") -> None:
        self.error = error
        self.fail = fail
        self.text = text
        self.calls = 0

    def post(self, url: str, payload: dict) -> dict:
        self.calls += 1
        if self.calls <= self.fail:
            raise self.error
        return {"output": {"output": {"sentence": {"text": self.text}}}}


def test_a_transient_failure_is_retried_once(monkeypatch):
    """一次 500 的后果本来是「说了一句，得到一片安静，只能再说一遍」。

    音频已经在手上，重发一次只多花一个往返。合成那一层刻意不重发是因为它有退路
    （降级到本机 VITS），而识别在一句话中途没有退路。
    """
    monkeypatch.setattr("core.audio.asr_cloud.RETRY_WAIT_S", 0.0)
    transport = Flaky(DashScopeAsrError("500 服务端出错", retryable=True))
    provider = DashScopeAsrProvider(transport=transport)
    assert provider._transcribe(speech(0.4)) == "救回来了"
    assert transport.calls == 2
    assert provider.retries == 1
    assert "500" in provider.last_retry


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_a_configuration_failure_is_not_retried(code, monkeypatch):
    """重发一个 401 只是把「你的变量装错了」推迟两秒说出来，而那两秒落在使用者的沉默里。"""
    monkeypatch.setattr("core.audio.asr_cloud.RETRY_WAIT_S", 0.0)
    transport = Flaky(DashScopeAsrError(f"{code} 配置错了", retryable=False), fail=99)
    provider = DashScopeAsrProvider(transport=transport)
    with pytest.raises(DashScopeAsrError):
        provider._transcribe(speech(0.4))
    assert transport.calls == 1
    assert provider.retries == 0


def test_retries_are_capped(monkeypatch):
    monkeypatch.setattr("core.audio.asr_cloud.RETRY_WAIT_S", 0.0)
    transport = Flaky(DashScopeAsrError("503", retryable=True), fail=99)
    provider = DashScopeAsrProvider(transport=transport)
    with pytest.raises(DashScopeAsrError):
        provider._transcribe(speech(0.4))
    assert transport.calls == MAX_ATTEMPTS


def test_retryable_is_set_from_the_status_code(monkeypatch):
    """哪些码值得重发是这一层的判断，不是调用方的。"""
    monkeypatch.setenv("VOX_ASR_KEY", "sk-fake")
    provider = DashScopeAsrProvider()

    def raise_http(code):
        def opener(*_args, **_kwargs):
            raise HTTPError("https://example.invalid", code, "no", {}, None)  # type: ignore[arg-type]
        return opener

    for code, expected in ((429, True), (500, True), (503, True), (401, False), (400, False)):
        monkeypatch.setattr("core.audio.asr_cloud.urlopen", raise_http(code))
        with pytest.raises(DashScopeAsrError) as caught:
            provider._post({"model": "m"})
        assert caught.value.retryable is expected, code


def test_a_timeout_and_a_dead_network_are_retryable(monkeypatch):
    monkeypatch.setenv("VOX_ASR_KEY", "sk-fake")
    provider = DashScopeAsrProvider()
    for error in (TimeoutError("slow"), URLError("no route")):
        def opener(*_args, _error=error, **_kwargs):
            raise _error
        monkeypatch.setattr("core.audio.asr_cloud.urlopen", opener)
        with pytest.raises(DashScopeAsrError) as caught:
            provider._post({"model": "m"})
        assert caught.value.retryable is True


def test_retries_and_failures_are_counted_separately(monkeypatch):
    """`failures` 是「最终没成」，`retries` 是「第一次没成但救回来了」。

    混在一个数字里会让「网络在抖」和「配置是错的」看起来一样。
    """
    monkeypatch.setattr("core.audio.asr_cloud.RETRY_WAIT_S", 0.0)
    transport = Flaky(DashScopeAsrError("502", retryable=True))
    provider = DashScopeAsrProvider(transport=transport, silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS......")
    drive(provider, stream, "SSSS...")
    for _ in range(40):
        result = provider.feed(stream, speech(0.1), SAMPLE_RATE)
        if result.is_endpoint:
            assert result.text == "救回来了"
            break
    assert provider.retries == 1
    assert provider.failures == 0
    assert provider.take_error() == ""


# -- 生命周期 ---------------------------------------------------------------


def test_reset_clears_everything_including_parts():
    provider = DashScopeAsrProvider(transport=Recorder("在"))
    stream = provider.create_stream()
    stream.parts.append("残留")
    stream.text = "残留"
    stream.frames = 999
    provider.reset(stream)
    assert stream.parts == []
    assert stream.text == ""
    assert stream.frames == 0


def test_finalize_hands_the_text_over_once():
    provider = DashScopeAsrProvider(transport=Recorder("在"))
    stream = provider.create_stream()
    stream.text = "拿走"
    assert provider.finalize(stream) == "拿走"
    assert provider.finalize(stream) == ""


def test_finalize_never_posts():
    """capture 在音频回调线程上调 finalize —— 在那里发请求正是这一层要避免的事。"""
    recorder = Recorder("在")
    provider = DashScopeAsrProvider(transport=recorder)
    stream = provider.create_stream()
    stream.text = "已经有了"
    provider.finalize(stream)
    assert recorder.calls == []


def test_each_stream_gets_its_own_vad():
    """VAD 有状态，跨轮复用会让上一句的尾巴影响这一句。"""
    provider = DashScopeAsrProvider(transport=Recorder("在"))
    first = provider.create_stream()
    second = provider.create_stream()
    assert first.gate is not second.gate


def test_take_error_clears_so_a_401_is_not_reported_forever():
    """只读的话一次 401 会让之后每一次正常的「没人说话」超时都跟着报同一条错。"""
    provider = DashScopeAsrProvider(transport=Boom(), silence_s=0.3, min_utterance_s=0.2)
    stream = provider.create_stream()
    stream.gate = FakeGate("SSSS......")
    drive(provider, stream, "SSSS...")
    for _ in range(50):
        if provider.feed(stream, speech(0.1), SAMPLE_RATE).is_endpoint:
            break
    assert "401" in provider.take_error()
    assert provider.take_error() == ""


def test_describe_reports_the_counters_and_no_key(monkeypatch):
    monkeypatch.setenv("VOX_ASR_KEY", "sk-secret")
    provider = DashScopeAsrProvider()
    described = provider.describe()
    assert described["key_env"] == "VOX_ASR_KEY"
    assert "sk-secret" not in json.dumps(described)
    for field_name in ("requests", "failures", "empty", "continuations", "last_latency_ms"):
        assert field_name in described


# -- 回包解析（两种形状都认）------------------------------------------------


def test_text_of_reads_the_asr_sentence_shape():
    payload = {"output": {"output": {"sentence": {"text": "你好，小沃。"}}}}
    assert _text_of(payload) == "你好，小沃。"


def test_text_of_reads_the_multimodal_choice_shape():
    payload = {"output": {"choices": [{"message": {"content": [{"text": "在的"}]}}]}}
    assert _text_of(payload) == "在的"


def test_text_of_reads_a_sentence_list():
    payload = {"output": {"output": {"sentence": [{"text": "前"}, {"text": "后"}]}}}
    assert _text_of(payload) == "前后"


def test_text_of_survives_an_unknown_shape():
    """服务端换形状那天要返回空串，不是抛异常 —— 抛出去会跑到音频线程上。"""
    assert _text_of({"output": {}}) == ""
    assert _text_of({}) == ""
    assert _text_of(None) == ""
    assert _text_of("字符串") == ""


def test_provider_shape_matches_the_local_one():
    """红线 2：两个 provider 摆同一个形状，所以 capture 一个字都不用改。"""
    from core.audio.asr import SherpaStreamingAsrProvider

    for name in ("load", "create_stream", "feed", "finalize", "reset", "close"):
        assert callable(getattr(DashScopeAsrProvider, name))
        assert callable(getattr(SherpaStreamingAsrProvider, name))
