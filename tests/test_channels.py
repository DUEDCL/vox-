"""微信通道：协议、语音进出、以及那几条不许越过的边界。

传输层是注入的，所以整条链路在**离线**下可断言 —— 那是 SIM。真机发一条微信消息是
REAL-WEIXIN，这个文件里没有任何一处声称它已经通过。

三条边界各有一条测试钉着：
1. **凭据永不进配置文件** —— 写 token 会被「未知键」拒掉；扫码换来的那份落在
   `.vox/channels/weixin.json`（gitignored），环境变量优先于它；
2. **默认关** —— 出厂默认下 `open_weixin` 返回 None，一个字节都不出网；
3. **媒体只从微信 CDN 下载** —— 一个能指向任意主机的字段就是一次 SSRF。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from core.channels.audio import to_16k_mono, to_wav_bytes
from core.channels.config import (
    ChannelConfigError,
    defaults,
    load_channels_config,
    open_weixin,
)
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


@pytest.fixture
def unbound(monkeypatch, tmp_path):
    """两个凭据来源都空。

    **必须两个都动。** `check()` 与 `_token()` 都是「环境变量优先，然后是扫码存下来的
    那份」，所以只 `delenv` 一个变量的测试在一台真的绑过微信的机器上会读到 `.vox/
    channels/weixin.json` 然后判定「能用」—— 一个结果取决于开发机私有状态的测试等于
    没有这条断言。
    """
    monkeypatch.delenv("VOX_WEIXIN_TOKEN", raising=False)
    monkeypatch.setenv("VOX_WEIXIN_CREDENTIALS", str(tmp_path / "nobody.json"))


# ----------------------------------------------------------------- 三条边界


def test_the_factory_default_keeps_everything_off(tmp_path):
    """**默认关**。打开它意味着长轮询腾讯的端点、收发媒体 —— 出网必须是一次显式选择。

    判的是**代码的默认值**与一份没写 `enabled` 的文件，不是仓库里那份 `config/
    channels.toml`。2026-09-04 之前这一条读的是后者，而从那天起控制台自己会往那个文件
    写 `enabled = true`（扫码成功之后）—— 于是这条断言会在**任何一台真的绑过微信的
    机器上失败**，把产品正常工作报成回归。
    """
    assert defaults()["weixin.enabled"] is False

    empty = tmp_path / "channels.toml"
    empty.write_text("[weixin]\n", encoding="utf-8")
    assert load_channels_config(empty)["weixin.enabled"] is False
    assert open_weixin(load_channels_config(empty)) is None

    # 文件根本不在也是全关 —— 「不配就一个字节都不出网」。
    missing = load_channels_config(tmp_path / "nope.toml")
    assert missing["weixin.enabled"] is False
    assert open_weixin(missing) is None


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


def test_check_does_not_touch_the_network(unbound):
    """和 agent 适配器的 `check()` 同一条规矩：它答的是「能不能用」，不是「通不通」。"""

    class Boom:
        def post_json(self, *a, **k):
            raise AssertionError("check() 不许打网络")

    status = WeixinChannel(transport=Boom()).check()

    assert status["available"] is False
    assert status["source"] == ""
    # 说清**该往哪走**。上一版这里只说「没有 $VOX_WEIXIN_TOKEN」，而那个 token 本来就是
    # 扫码换来的 —— 一个正常使用者读完这句话仍然无处可取。
    assert "扫码登录" in status["reason"]


def test_check_accepts_the_scanned_credentials(monkeypatch, tmp_path):
    """**扫码存下来的凭据要算「能用」。**

    这是使用者报的那件事的根因：`check()` 只问环境变量，而 `_token()` 早就先环境变量、
    后扫码凭据。于是扫完码、把 `enabled` 打开、重启之后，`start_channels` 读到
    `available=False`，打印一行「配了但用不了 —— 没有 $VOX_WEIXIN_TOKEN」就什么都不起：
    绑定成功了，微信里说话没有任何反应，而唯一的线索是一行启动日志。
    """
    monkeypatch.delenv("VOX_WEIXIN_TOKEN", raising=False)
    path = tmp_path / "weixin.json"
    path.write_text(
        json.dumps({"account_id": "acc-1", "token": "scanned-token", "base_url": "https://b.example"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("VOX_WEIXIN_CREDENTIALS", str(path))

    status = WeixinChannel().check()

    assert status["available"] is True
    assert status["source"] == "scan"
    assert status["reason"] == ""
    # 报的是**将要用的**那个 base_url：`scaned_but_redirect` 会把账号分到另一个域名上。
    assert status["base"] == "https://b.example"
    # **永不回显 token。** 这一页会被截图。
    assert "scanned-token" not in json.dumps(status, ensure_ascii=False)


def test_check_does_not_mutate_the_channel(monkeypatch, tmp_path):
    """`check()` 不带副作用 —— 「看一眼状态」不该顺手改掉 base_url。"""
    monkeypatch.delenv("VOX_WEIXIN_TOKEN", raising=False)
    path = tmp_path / "weixin.json"
    path.write_text(
        json.dumps({"account_id": "a", "token": "t", "base_url": "https://moved.example"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("VOX_WEIXIN_CREDENTIALS", str(path))
    channel = WeixinChannel()
    before = channel.base_url

    channel.check()

    assert channel.base_url == before


def test_the_env_var_still_wins(monkeypatch, tmp_path):
    """环境变量优先。反过来的话，一个想临时换账号的人会发现改了环境变量没有用。"""
    path = tmp_path / "weixin.json"
    path.write_text(json.dumps({"account_id": "a", "token": "scanned"}), encoding="utf-8")
    monkeypatch.setenv("VOX_WEIXIN_CREDENTIALS", str(path))
    monkeypatch.setenv("VOX_WEIXIN_TOKEN", "from-env")

    assert WeixinChannel().check()["source"] == "env"
    assert WeixinChannel()._token() == "from-env"


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


# ------------------------------------------- 控制台那一栏：实时收发（2026-09-03）


def test_the_transcript_records_both_directions_with_a_usable_cursor():
    """控制台的「微信」那一栏要能看实时收发，所以两个方向都要进会话记录。

    游标用**序号**不用时间戳：同一秒里可能有两条，而一个会漏条目的游标比没有游标更糟 ——
    它会让「刚才那条去哪了」变成一个偶发问题。
    """
    channel = FakeChannel()
    runner = ChannelRunner(channel=channel, runtime=FakeRuntime("三点半"), reply_with_voice=False)

    runner.handle(IncomingMessage(chat_id="c1", text="现在几点"))

    read = runner.read_transcript()
    directions = [row["direction"] for row in read["entries"]]
    assert directions == ["in", "out"]
    assert read["entries"][0]["text"] == "现在几点"
    assert read["entries"][1]["text"] == "三点半"
    assert read["entries"][1]["by"] == "agent"
    assert read["next"] == 2

    # 从游标之后再读一次就该是空的 —— 页面每一两秒问一次，重复给会让消息看起来发了两遍。
    assert runner.read_transcript(since=read["next"])["entries"] == []


def test_the_transcript_is_capped_and_the_cursor_stays_monotonic():
    """环形缓冲、不落盘。挤掉旧条目时序号**必须继续往上走** —— 重用序号会让页面把新条目
    当成旧的、直接不显示。"""
    from core.channels.runner import TRANSCRIPT_MAX

    runner = ChannelRunner(channel=FakeChannel(), runtime=FakeRuntime("x"))
    for index in range(TRANSCRIPT_MAX + 25):
        runner._record("in", chat_id="c1", text=f"第 {index} 条")

    assert len(runner.transcript) == TRANSCRIPT_MAX
    seqs = [row["seq"] for row in runner.transcript]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs[-1] == TRANSCRIPT_MAX + 25
    assert runner.read_transcript()["dropped"] == 25


def test_sending_from_the_console_does_not_run_a_turn():
    """那一栏是「我自己说」，不是「让它答」。走 agent 的话，控制台上手打一句会被当成
    使用者的请求发给模型，然后模型的回答被发给微信 —— 那是另一个功能。"""
    channel = FakeChannel()
    runtime = FakeRuntime("不该被调用")
    runner = ChannelRunner(channel=channel, runtime=runtime)

    runner.send_text("chat-1", "我一会儿回你")

    assert runtime.said == [], "控制台手打的消息跑了一轮派发"
    assert channel.sent[-1].text == "我一会儿回你"
    entry = runner.read_transcript()["entries"][-1]
    assert entry["direction"] == "out" and entry["by"] == "console"


def test_sending_an_empty_message_is_refused():
    from core.channels.contract import ChannelError

    runner = ChannelRunner(channel=FakeChannel(), runtime=FakeRuntime("x"))

    with pytest.raises(ChannelError, match="空的"):
        runner.send_text("chat-1", "   ")


def test_the_chat_list_groups_by_peer_and_counts():
    runner = ChannelRunner(channel=FakeChannel(), runtime=FakeRuntime("x"))
    runner._record("in", chat_id="c1", text="嗨", sender="wx-aaaa")
    runner._record("out", chat_id="c1", text="嗨你好")
    runner._record("in", chat_id="c2", text="在吗", sender="wx-bbbb")

    chats = {row["chat_id"]: row for row in runner.chats()}

    assert chats["c1"]["messages"] == 2
    assert chats["c2"]["last"] == "在吗"
    assert chats["c2"]["sender"] == "wx-bbbb"
