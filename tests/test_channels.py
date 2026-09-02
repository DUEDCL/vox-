"""微信通道：协议、语音进出、以及那几条不许越过的边界。

传输层是注入的，所以整条链路在**离线**下可断言 —— 那是 SIM。真机发一条微信消息是
REAL-WEIXIN，这个文件里没有任何一处声称它已经通过。

三条边界各有一条测试钉着：
1. **凭据只从环境变量读** —— 配置文件里写 token 会被「未知键」拒掉；
2. **默认关** —— 出厂配置下 `open_weixin` 返回 None，一个字节都不出网；
3. **媒体只从微信 CDN 下载** —— 一个能指向任意主机的字段就是一次 SSRF。
"""

from __future__ import annotations

import numpy as np
import pytest

from core.channels.audio import to_16k_mono, to_wav_bytes
from core.channels.config import ChannelConfigError, load_channels_config, open_weixin
from core.channels.contract import IncomingMessage, OutgoingMessage
from core.channels.crypto import aes128_ecb_encrypt
from core.channels.runner import PROVIDER_STT_NOTE, ChannelRunner
from core.channels.weixin import WeixinChannel


class FakeTransport:
    """记下每一次请求，按端点给预设答复。**不打网络。**"""

    def __init__(self, updates=None, media: bytes = b"") -> None:
        self.calls: list[tuple[str, object]] = []
        self.updates = updates if updates is not None else []
        self.media = media
        self.uploaded: list[int] = []

    def post_json(self, url, payload, headers, timeout_s):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, payload))
        if "getupdates" in url:
            return {"ret": 0, "get_updates_buf": "buf-2", "msgs": self.updates}
        if "getuploadurl" in url:
            return {"ret": 0, "upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?x=1"}
        return {"ret": 0}

    def post_bytes(self, url, data, headers, timeout_s):
        self.uploaded.append(len(data))
        self.calls.append(("upload", len(data)))
        return b'{"encrypt_query_param":"qp-1"}'

    def get_bytes(self, url, timeout_s):
        self.calls.append(("download", url))
        return self.media


def voice_message(*, text: str = "腾讯的转写", media_url: str = "", aes: str = "") -> dict:
    voice: dict = {"text": text}
    if media_url:
        voice["media"] = {"full_url": media_url, "aes_key": aes, "file_name": "v.wav"}
    return {
        "msg_id": "m1",
        "from_user_id": "peer-1",
        "context_token": "ctx-9",
        "item_list": [{"type": 3, "voice_item": voice}],
    }


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("VOX_WEIXIN_TOKEN", "offline-token")


# ----------------------------------------------------------------- 三条边界


def test_the_shipped_config_keeps_everything_off():
    """**默认关**。打开它意味着长轮询腾讯的端点、收发媒体 —— 出网必须是一次显式选择。"""
    config = load_channels_config()

    assert config["weixin.enabled"] is False
    assert open_weixin(config) is None


def test_a_token_in_the_file_is_refused(tmp_path):
    """凭据永不进配置文件。schema 层面就没有这个键，所以它以「未知键」被拒。"""
    path = tmp_path / "channels.toml"
    path.write_text('[weixin]\ntoken = "sk-real-secret"\n', encoding="utf-8")

    with pytest.raises(ChannelConfigError) as caught:
        load_channels_config(path)

    assert "weixin.token" in str(caught.value)


def test_a_misspelled_key_is_refused(tmp_path):
    """拼错的 enabled 会让「我开了微信但它没反应」变成一次无从下手的排查。"""
    path = tmp_path / "channels.toml"
    path.write_text("[weixin]\nenabld = true\n", encoding="utf-8")

    with pytest.raises(ChannelConfigError):
        load_channels_config(path)


def test_media_outside_the_weixin_cdn_is_not_downloaded(token):
    """一个能指向任意主机的字段就是一次 SSRF。拒绝下载而不是「试试看」。"""
    transport = FakeTransport(
        updates=[voice_message(media_url="https://evil.example.com/a.wav")], media=b"RIFF"
    )
    channel = WeixinChannel(transport=transport)

    message = channel.poll(1.0)[0]

    assert message.media == b""
    assert "微信 CDN" in channel.last_error
    assert not any(call[0] == "download" for call in transport.calls)


# ----------------------------------------------------------------- 协议


def test_check_does_not_touch_the_network(monkeypatch):
    """和 agent 适配器的 `check()` 同一条规矩：它答的是「能不能用」，不是「通不通」。"""
    monkeypatch.delenv("VOX_WEIXIN_TOKEN", raising=False)

    class Boom:
        def post_json(self, *a, **k):
            raise AssertionError("check() 不许打网络")

    status = WeixinChannel(transport=Boom()).check()

    assert status["available"] is False
    assert "VOX_WEIXIN_TOKEN" in status["reason"]


def test_the_sync_cursor_advances_and_the_context_token_is_kept(token):
    """**每条出站回复必须回带该 peer 最新的 `context_token`**，漏了就发不出去。"""
    transport = FakeTransport(updates=[voice_message()])
    channel = WeixinChannel(transport=transport)

    message = channel.poll(1.0)[0]

    assert channel.sync_buf == "buf-2"
    assert message.reply_context["context_token"] == "ctx-9"
    assert channel.context_tokens["peer-1"] == "ctx-9"


def test_the_same_message_is_not_handled_twice(token):
    """长轮询会重投同一条。不去重的后果是同一句话被回答两遍。"""
    transport = FakeTransport(updates=[voice_message()])
    channel = WeixinChannel(transport=transport)

    assert len(channel.poll(1.0)) == 1
    assert channel.poll(1.0) == ()


def test_a_stale_session_is_told_apart_from_rate_limiting(token):
    """iLink 用两个码表达同一件事：-14，以及 errmsg 恰好是 "unknown error" 的 -2。
    分不开的后果是「被限流」和「要重新扫码」用同一句话报出来。"""

    class Stale(FakeTransport):
        def post_json(self, url, payload, headers, timeout_s):
            return {"ret": -2, "errmsg": "unknown error"}

    channel = WeixinChannel(transport=Stale())

    assert channel.poll(1.0) == ()
    assert "扫码" in channel.last_error


def test_a_network_error_returns_empty_rather_than_raising(token):
    """`poll` 在一个 while 循环里被反复调用。「这一轮网线松了」不该结束那个循环。"""

    class Broken(FakeTransport):
        def post_json(self, *a, **k):
            raise OSError("connection reset")

    channel = WeixinChannel(transport=Broken())

    assert channel.poll(1.0) == ()
    assert channel.last_error


# ----------------------------------------------------------------- 语音进出


def test_an_encrypted_voice_note_is_downloaded_and_decrypted(token):
    """入站语音的完整一条：下载 → AES 解密 → 按 rawsize 裁 → 认出格式。"""
    wav = to_wav_bytes(np.zeros(1600, dtype=np.float32), 16000)
    key = bytes(range(16))
    transport = FakeTransport(
        updates=[
            {
                "msg_id": "m2",
                "from_user_id": "peer-1",
                "item_list": [
                    {
                        "type": 3,
                        "voice_item": {
                            "text": "腾讯的",
                            "media": {
                                "full_url": "https://novac2c.cdn.weixin.qq.com/c2c/x",
                                "aes_key": key.hex(),
                                "rawsize": len(wav),
                            },
                        },
                    }
                ],
            }
        ],
        media=aes128_ecb_encrypt(wav, key),
    )

    message = WeixinChannel(transport=transport).poll(1.0)[0]

    assert message.is_voice
    assert message.media == wav
    assert message.media_format == "wav"
    assert message.provider_text == "腾讯的"


def test_outbound_audio_is_encrypted_before_it_leaves(token):
    """上传的必须是密文。明文上 CDN 等于把那段语音放在一个公网可读的地方。"""
    audio = b"RIFF0000WAVEdata-and-more"
    transport = FakeTransport()
    channel = WeixinChannel(transport=transport)

    result = channel.send(OutgoingMessage(chat_id="peer-1", text="好", audio=audio))

    assert result["audio"] == "file"
    # 密文长度是补齐后的长度，而且不等于明文。
    assert transport.uploaded == [len(audio) + (16 - len(audio) % 16)]
    endpoints = [call[0] for call in transport.calls]
    assert endpoints == ["sendmessage", "getuploadurl", "upload", "sendmessage"]


def test_a_native_voice_bubble_is_opt_in(token):
    """原生语音气泡在上游没跑通（Hermes 自己的 `send_voice` 注释），所以**默认走文件附件**
    —— 一条发不出去的语音比一条能播的附件差。"""
    default_channel = WeixinChannel(transport=FakeTransport())
    native_channel = WeixinChannel(transport=FakeTransport(), voice_native=True)

    assert default_channel.voice_native is False
    assert default_channel.send(OutgoingMessage(chat_id="p", audio=b"x" * 32))["audio"] == "file"
    assert native_channel.send(OutgoingMessage(chat_id="p", audio=b"x" * 32))["audio"] == "voice"


def test_a_message_with_neither_text_nor_audio_is_refused(token):
    from core.channels.contract import ChannelError

    with pytest.raises(ChannelError):
        WeixinChannel(transport=FakeTransport()).send(OutgoingMessage(chat_id="p"))


# ----------------------------------------------------------------- 音频这一层


def test_silk_is_honestly_undecodable():
    """微信的语音多是 SILK，而没有纯 Python 的 SILK 解码器。**不假装能解** ——
    把 SILK 字节硬喂给识别器会得到一段噪声的转写，那比「我解不了」难查得多。"""
    assert to_16k_mono(b"\x02#!SILK_V3" + bytes(64), "silk") is None


def test_a_44k_wav_is_resampled_to_the_model_rate():
    """16 kHz 是三个模型共同的约定。本机 VITS 出 44.1 kHz，微信那边什么都可能。"""
    source = np.sin(np.arange(44100) * 0.01).astype(np.float32)

    samples = to_16k_mono(to_wav_bytes(source, 44100), "wav")

    assert samples is not None
    assert abs(samples.size - 16000) <= 2


def test_stereo_is_averaged_not_halved():
    """取平均而不是取第一条：微信那边的双声道是同一路声音，平均能少 3 dB 噪声。"""
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes((np.ones(3200, dtype="<i2") * 1000).tobytes())

    samples = to_16k_mono(buffer.getvalue(), "wav")

    assert samples is not None
    assert samples.size == 1600


def test_an_oversized_blob_is_refused():
    """一条微信语音最长 60 秒。8 MB 的上限挡住「一个 200 MB 的文件」。"""
    from core.channels.audio import MAX_INBOUND_BYTES

    assert to_16k_mono(b"RIFF" + bytes(MAX_INBOUND_BYTES), "wav") is None


# ------------------------------------------------- 一条消息走完一整轮（runner）


class FakeAsr:
    def create_stream(self):
        return []

    def feed(self, stream, block, rate):
        stream.append(len(block))

    def finalize(self, stream):
        return "本机转写的结果"


class FakeTts:
    def synthesize(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(samples=np.zeros(8000, dtype=np.float32), sample_rate=16000)


class FakeChannel:
    name = "weixin"
    last_error = ""

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    def send(self, message):
        self.sent.append(message)
        return {"text": bool(message.text), "audio": bool(message.audio)}


class FakeRuntime:
    def __init__(self, reply: str = "知道了") -> None:
        self.said: list[tuple[str, bool]] = []
        self.reply = reply

    def say(self, text, *, speak=True):
        from types import SimpleNamespace

        self.said.append((text, speak))
        return SimpleNamespace(text=self.reply, route="agent", ok=True)

    def log(self, *args, **kwargs):
        return None


def _runner(**kwargs):
    channel, runtime = FakeChannel(), FakeRuntime()
    defaults = {"asr": FakeAsr(), "tts": FakeTts()}
    defaults.update(kwargs)
    return ChannelRunner(channel=channel, runtime=runtime, **defaults), channel, runtime


def test_local_asr_wins_when_the_audio_can_be_decoded():
    """腾讯自带的 STT 对非中文是错的（Hermes issue #27300），而本机那个是我们调过的。
    所以顺序是「能解开就自己转」，不是反过来。"""
    runner, _channel, runtime = _runner()
    wav = to_wav_bytes(np.zeros(1600, dtype=np.float32), 16000)

    result = runner.handle(
        IncomingMessage(chat_id="c1", kind="voice", provider_text="腾讯听到的", media=wav, media_format="wav")
    )

    assert result["source"] == "local"
    assert runtime.said[-1][0] == "本机转写的结果"


def test_the_provider_transcript_is_the_fallback_and_says_so():
    """SILK 解不了时退回腾讯的文本，**并且标注来源** —— 一句转错的话如果不说来源，
    使用者会以为是我们的识别器听错了，然后去调一个没问题的模型。"""
    runner, _channel, runtime = _runner()

    result = runner.handle(
        IncomingMessage(chat_id="c1", kind="voice", provider_text="腾讯听到的", media=b"\x02#!SILK_V3", media_format="silk")
    )

    assert result["source"] == "provider"
    assert PROVIDER_STT_NOTE in runtime.said[-1][0]


def test_a_voice_note_with_no_usable_text_is_skipped_not_answered():
    """既没原件也没转写时不要「回答」—— 那会变成对着空气生成一段话。"""
    runner, channel, runtime = _runner()

    assert runner.handle(IncomingMessage(chat_id="c1", kind="voice", media_format="silk")) is None
    assert runtime.said == []
    assert channel.sent == []


def test_the_reply_carries_both_text_and_voice():
    """**文字永远都带。** 一条只有语音的回复在电脑上看不了，也搜不到。"""
    runner, channel, _runtime = _runner()

    runner.handle(IncomingMessage(chat_id="c1", text="今天几号"))

    sent = channel.sent[-1]
    assert sent.text == "知道了"
    assert sent.audio and sent.audio_format == "wav"


def test_the_local_speakers_stay_quiet():
    """微信来的这一轮不该同时从本机音箱放出来 —— `speak=False`。"""
    runner, _channel, runtime = _runner()

    runner.handle(IncomingMessage(chat_id="c1", text="你好"))

    assert runtime.said[-1][1] is False


def test_a_synthesis_failure_still_sends_the_text():
    """语音是增强。合不出来不该让这条消息没有回复。"""

    class BrokenTts:
        def synthesize(self, text):
            raise RuntimeError("云端 401")

    runner, channel, _runtime = _runner(tts=BrokenTts())

    runner.handle(IncomingMessage(chat_id="c1", text="你好"))

    assert channel.sent[-1].text == "知道了"
    assert channel.sent[-1].audio == b""


def test_voice_out_can_be_turned_off():
    runner, channel, _runtime = _runner(reply_with_voice=False)

    runner.handle(IncomingMessage(chat_id="c1", text="你好"))

    assert channel.sent[-1].audio == b""


def test_a_failing_turn_still_answers_something():
    """一条坏消息不能结束这条通道，也不该让对面等一个永远不来的回复。"""

    class Angry(FakeRuntime):
        def say(self, text, *, speak=True):
            raise RuntimeError("dispatch exploded")

    channel = FakeChannel()
    runner = ChannelRunner(channel=channel, runtime=Angry(), asr=FakeAsr(), tts=FakeTts())

    assert runner.handle(IncomingMessage(chat_id="c1", text="你好")) is None
    assert runner.failures == 1
    assert channel.sent and "出错" in channel.sent[-1].text


def test_the_reply_context_is_carried_back():
    """iLink 要求出站回带 `context_token`。runner 只是原样传，**不读它** ——
    那是那一个平台的私事（契约里写着）。"""
    runner, channel, _runtime = _runner()

    runner.handle(IncomingMessage(chat_id="c1", text="你好", reply_context={"context_token": "ctx-7"}))

    assert channel.sent[-1].reply_context == {"context_token": "ctx-7"}
