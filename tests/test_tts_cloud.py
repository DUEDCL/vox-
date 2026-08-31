"""云端 TTS（阿里云百炼 CosyVoice）：请求形状、密钥来源、失败姿态、装配选路。

证据等级：**AUTO**。HTTP 传输是注入的假的，所以这一组不打网络、不花额度。
真的把 longyuan 那把声音放出来是 REAL —— 需要密钥和扬声器在场，走
`scripts/probe_dashscope_tts.py`。

这一组存在的理由是使用者 2026-08-29 的报告：「使用阿里云的最佳模型作为 tts 模型，
音色用 longyuan，我在控制台并不能直接配置」。当时不能配置有五个各自独立致命的原因，
每一条现在都有对应的断言：

1. 代码里没有云端 TTS provider           -> test_a_synthesis_posts_model_voice_and_reads_the_url
2. `voice.toml` 的 schema 没有这几个键   -> tests/test_voice_config.py 那边
3. `EDITABLE` 白名单里没有它们           -> test_the_console_can_edit_provider_model_and_voice
4. `config/voice.toml` 文件里没有这几行  -> 同上（config_edit 只改已存在的键）
5. 密钥白名单里没有 VOX_DASHSCOPE_KEY    -> test_the_key_name_is_settable_from_the_console
"""

from __future__ import annotations

import io
import json
import time
from typing import Mapping

import numpy as np
import pytest
import soundfile as sf

from core.audio.tts_cloud import DashScopeTtsError, DashScopeTtsProvider


def _wav_bytes(seconds: float = 0.4, sample_rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    tone = (np.sin(np.linspace(0, 220 * 2 * np.pi * seconds, int(seconds * sample_rate))) * 0.3)
    sf.write(buffer, tone.astype("float32"), sample_rate, format="WAV")
    return buffer.getvalue()


class FakeTransport:
    """替掉两个 HTTP 往返。``posted`` 留着给断言看请求体。"""

    def __init__(self, body: dict | None = None, audio: bytes | None = None) -> None:
        self.posted: list[tuple[str, dict]] = []
        self.fetched: list[str] = []
        self.body = body if body is not None else {
            "request_id": "r-1",
            "output": {"finish_reason": "stop", "audio": {"data": "", "url": "https://oss/x.wav"}},
            "usage": {"characters": 4},
        }
        self.audio = audio if audio is not None else _wav_bytes()

    def post(self, url: str, payload: dict) -> dict:
        self.posted.append((url, payload))
        return self.body

    def get(self, url: str) -> bytes:
        self.fetched.append(url)
        return self.audio


def test_a_synthesis_posts_model_voice_and_reads_the_url(monkeypatch):
    """请求体的形状是钉死的:只有 model 与 input 两个顶层键,音色在 input 里。

    这个 API **没有** parameters 这一层(和 OpenAI 那套不同),写错层级会 400。
    音频也不在回包里:非流式模式下 output.audio.data 是空的,只有 output.audio.url。
    """
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")
    transport = FakeTransport()
    provider = DashScopeTtsProvider(
        model="cosyvoice-v1", voice="longyuan", transport=transport
    )
    audio = provider.synthesize("你好小沃")

    _url, payload = transport.posted[0]
    assert set(payload) == {"model", "input"}, "顶层只许 model 与 input"
    assert payload["model"] == "cosyvoice-v1"
    assert payload["input"]["voice"] == "longyuan"
    assert payload["input"]["text"] == "你好小沃"
    assert payload["input"]["format"] == "wav"
    assert transport.fetched == ["https://oss/x.wav"], "音频必须从 output.audio.url 下载"
    assert audio.sample_rate == 24000
    assert len(audio.samples) > 0
    assert audio.samples.ndim == 1, "下游按一维处理,和本机 provider 一致"


def test_a_reply_without_an_audio_url_is_an_error_not_silence(monkeypatch):
    """回包里没有 url 要抛,不能返回一段空音频 —— 静音会被读成「合成成功但没声音」。"""
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")
    transport = FakeTransport(body={"output": {"finish_reason": "length"}, "usage": {}})
    provider = DashScopeTtsProvider(transport=transport)
    with pytest.raises(DashScopeTtsError) as caught:
        provider.synthesize("你好")
    assert "length" in str(caught.value)


def test_no_key_reports_the_variable_name_and_never_a_value(monkeypatch):
    """缺密钥时报的是**变量名**。这是日志/事件里唯一允许出现的形式。"""
    monkeypatch.delenv("VOX_DASHSCOPE_KEY", raising=False)
    provider = DashScopeTtsProvider()
    assert provider.available is False
    status = provider.load()
    assert status.available is False
    assert "VOX_DASHSCOPE_KEY" in status.details["reason"]


def test_load_reports_the_key_name_not_the_key(monkeypatch):
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-secret-value-here")
    status = DashScopeTtsProvider(model="cosyvoice-v2", voice="longyuan").load()
    assert status.available is True
    flattened = repr(status.details) + status.source
    assert "sk-secret-value-here" not in flattened, "密钥不得出现在任何报告里"
    assert status.details["key_env"] == "VOX_DASHSCOPE_KEY"
    assert status.details["voice"] == "longyuan"
    # source 只到主机名,不带路径也不带凭据
    assert status.source == "https://dashscope.aliyuncs.com"


def test_available_does_not_hit_the_network(monkeypatch):
    """``available`` 被只读路径(describe / 就绪清单)调用。它发请求 = 开一次状态页花掉额度。"""
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")
    transport = FakeTransport()
    provider = DashScopeTtsProvider(transport=transport)
    assert provider.available is True
    assert transport.posted == []


def test_a_barge_in_during_synthesis_cancels_the_playback(monkeypatch):
    """打断要在「云端还在合成」这个窗口里生效 —— 而那个窗口在云端 TTS 上很宽。

    本机合成是几百毫秒,云端是两个 HTTP 往返。所以「说完唤醒词打断」很可能正好落在
    请求在途的时候:那一刻 stop() 必须让已经合成好的这一段**不要播**,否则用户会听到
    自己刚打断掉的那句话又开始说。

    注意 `speak()` 开头会把 `_stopped` 清成 False —— 那是对的,一次新的 speak 是一句新话。
    所以这里让打断发生在**合成过程中**,那才是真实的竞争。
    """
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")

    class Player:
        def __init__(self) -> None:
            self.played = 0

        def play(self, samples, sample_rate, **kwargs):
            del samples, sample_rate, kwargs
            self.played += 1

        def stop(self) -> None:
            pass

    class BargingTransport(FakeTransport):
        """请求在途时有人喊了唤醒词。"""

        def __init__(self, box) -> None:
            super().__init__()
            self.box = box

        def post(self, url: str, payload: dict) -> dict:
            self.box[0].stop()
            return super().post(url, payload)

    box: list = [None]
    player = Player()
    provider = DashScopeTtsProvider(transport=BargingTransport(box), playback=player)
    box[0] = provider
    result = provider.speak("你好")
    assert result["played"] is False
    assert result["reason"] == "stopped"
    assert player.played == 0, "打断之后不许再播"


def test_speak_segments_stops_between_segments(monkeypatch):
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")

    class Player:
        def __init__(self, provider_box) -> None:
            self.played = 0
            self.box = provider_box

        def play(self, samples, sample_rate, **kwargs):
            del samples, sample_rate, kwargs
            self.played += 1
            self.box[0].stop()  # 第一段播完就打断

        def stop(self) -> None:
            pass

    box: list = [None]
    player = Player(box)
    provider = DashScopeTtsProvider(transport=FakeTransport(), playback=player)
    box[0] = provider
    result = provider.speak_segments(["第一句", "第二句", "第三句"])
    assert player.played == 1
    assert result["stopped"] is True
    assert result["segments"] == 1


# --------------------------------------------------------- 段间空白（2026-08-30）


def test_the_next_segment_is_synthesised_while_the_current_one_plays(monkeypatch):
    """使用者 2026-08-30 报的「句子之间的间隔太长，感觉不是很连贯」。

    根因是形状而不是网络：一次合成是**两个 HTTP 往返**（实测 0.7–1.5 s），而原来的循环是
    「合成一段 → 播一段 → 合成下一段」，那段时间完整地落在两句话之间。播放是阻塞的，
    所以那段时间本来就闲着。

    断言写成「**播第一段的时候第二段的请求已经发出去了**」而不是比较耗时：一个计时断言
    在忙机器上会假红，而这一条正是要证明的那件事。
    """
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")

    class Player:
        def __init__(self, transport) -> None:
            self.transport = transport
            self.posted_while_playing: list[int] = []

        def play(self, samples, sample_rate, **kwargs):
            del samples, sample_rate, kwargs
            # 等预取那个线程把请求发出去。没有预取的话这里会干等满 2 秒然后断言失败。
            deadline = time.monotonic() + 2.0
            while len(self.transport.posted) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.posted_while_playing.append(len(self.transport.posted))

        def stop(self) -> None:
            pass

    transport = FakeTransport()
    player = Player(transport)
    provider = DashScopeTtsProvider(transport=transport, playback=player)

    result = provider.speak_segments(["第一句。", "第二句。"])

    assert result["segments"] == 2
    assert player.posted_while_playing[0] == 2, "播第一段时第二段必须已经在合成"


def test_a_synthesis_failure_in_a_prefetched_segment_is_raised_not_swallowed(monkeypatch):
    """预取是在另一个线程上跑的。那个线程里的异常必须在**取结果的地方**重抛 ——
    吞掉它会让「后半段没说」变成一件没有任何记录的事。"""
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")

    class SecondFails(FakeTransport):
        def post(self, url: str, payload: dict) -> dict:
            if len(self.posted) >= 1:
                self.posted.append((url, payload))
                raise DashScopeTtsError("第二段炸了")
            return super().post(url, payload)

    class Player:
        def __init__(self) -> None:
            self.played = 0

        def play(self, samples, sample_rate, **kwargs):
            del samples, sample_rate, kwargs
            self.played += 1

        def stop(self) -> None:
            pass

    player = Player()
    provider = DashScopeTtsProvider(transport=SecondFails(), playback=player)
    with pytest.raises(DashScopeTtsError):
        provider.speak_segments(["第一句。", "第二句。"])
    assert player.played == 1, "第一段照说，坏的是第二段"


def test_short_sentences_are_merged_but_the_first_one_never_is():
    """合并的两个理由都不是省钱：短段盖不住合成时间，而且**韵律是按请求算的** ——
    每句单独合成 = 每句各自起调收尾，拼起来听得出接缝。

    第一段例外：它决定「多久出第一个字」，那是最能被感知的延迟。
    """
    from core.audio.tts_cloud import SEGMENT_MERGE_CHARS, merge_segments

    assert merge_segments([]) == []
    assert merge_segments(["  ", ""]) == []
    assert merge_segments(["只有一句。"]) == ["只有一句。"]
    assert merge_segments(["一。", "二。", "三。"]) == ["一。", "二。三。"]

    long_tail = "字" * SEGMENT_MERGE_CHARS
    assert merge_segments(["头。", long_tail, "尾。"]) == ["头。", long_tail, "尾。"]


def test_the_merged_text_is_what_actually_goes_out(monkeypatch):
    """合并是在 provider 里做的，所以要验的是**请求体**，不是返回的计数。"""
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")

    class Player:
        def play(self, samples, sample_rate, **kwargs):
            del samples, sample_rate, kwargs

        def stop(self) -> None:
            pass

    transport = FakeTransport()
    provider = DashScopeTtsProvider(transport=transport, playback=Player())
    provider.speak_segments(["第一句。", "第二。", "第三。"])

    sent = [payload["input"]["text"] for _url, payload in transport.posted]
    assert sent == ["第一句。", "第二。第三。"]


# ------------------------------------------------- 控制台那一侧的四道闸(原因 3/4/5)


def test_the_console_can_edit_provider_model_and_voice():
    """使用者的原话是「我在控制台并不能直接配置」。这三个键必须在可编辑白名单里。"""
    from core.console.routes import EDITABLE

    keys = EDITABLE["voice.toml"]
    for key in ("tts.provider", "tts.model", "tts.voice"):
        assert key in keys, f"{key} 不在白名单里,页面上就改不了"


def test_the_key_env_name_is_not_editable_from_a_web_page():
    """反向断言,而且这一条比上面三条重要。

    `tts.key_env` 是「去读哪个环境变量」。让网页改它等于让网页决定把**哪个凭据**发给
    百炼 —— 指到 ANTHROPIC_AUTH_TOKEN 上就是一次凭据外发。密钥的**值**走 /api/secret
    (有白名单校验),变量**名**留在文件里。
    """
    from core.console.routes import EDITABLE

    assert "tts.key_env" not in EDITABLE["voice.toml"]


def test_the_key_name_is_settable_from_the_console():
    """值要能从页面存进去,否则使用者只能手改 .env。"""
    from core.console.routes import allowed_secret_names

    assert "VOX_DASHSCOPE_KEY" in allowed_secret_names()


def test_the_shipped_voice_config_carries_the_three_keys():
    """`core/config_edit.py` **只改已存在的键**(为了保住注释)。所以光把键加进 schema
    不够 —— 文件里没有那一行,控制台配置页就扫不到它,页面上也就没有那一栏。"""
    from core.audio.config import repo_root

    text = (repo_root() / "config" / "voice.toml").read_text(encoding="utf-8")
    for key in ("provider", "model", "voice", "key_env"):
        assert f"\n{key} = " in text, f"config/voice.toml 里缺 {key} 那一行"


def test_longyuan_is_in_the_voice_table_with_its_chinese_name():
    """使用者点名的音色。表的出处与核实日期也一起钉住 —— 一张没有出处的表下一个人没法核。"""
    from core.audio.voices import VOICE_LIST_CHECKED, VOICE_LIST_SOURCE, describe_voice

    found = describe_voice("longyuan")
    assert found is not None
    assert found.name == "龙媛"
    assert "help.aliyun.com" in VOICE_LIST_SOURCE
    assert VOICE_LIST_CHECKED == "2026-08-29"


def test_the_voice_list_says_out_loud_that_it_is_not_live():
    """百炼**没有**列举系统预置音色的 API。界面上一个下拉框会让人以为它是实时的,
    所以响应里必须带 live=False + 出处 —— 那是诚实性的一部分,不是装饰。

    音色还必须**按当前 model 过滤**:文档明写「每个 model 只支持一组特定的 voice,
    不能混用」,实测混用回 411。给一个「全部音色」的下拉是在邀请报错。
    """
    from unittest.mock import MagicMock

    from core.audio.voices import voices_for
    from core.console.routes import ConsoleApi

    view = ConsoleApi(MagicMock()).voices_view()
    assert view["live"] is False
    assert "help.aliyun.com" in view["source"]
    assert "cosyvoice-v1" in view["models"]
    expected = {item.voice for item in voices_for(view["model"])}
    assert {item["voice"] for item in view["voices"]} == expected
    assert view["voices"], "当前 model 至少要有一个可选音色"


def test_qwen_audio_tts_does_not_offer_the_cosyvoice_voices():
    """反向断言,而且它是这一组里最省时间的一条。

    使用者点名过 longyuan(cosyvoice-v1 的音色)。把它填到 qwen-audio-3.0-tts-plus 上
    实测回 411 —— 20 个候选名里只有 `longanhuan_v3.6` 回 200。所以这两组必须分开,
    否则界面会把一个必然失败的组合摆在人眼前。
    """
    from core.audio.voices import voices_for

    qwen = {item.voice for item in voices_for("qwen-audio-3.0-tts-plus")}
    cosy = {item.voice for item in voices_for("cosyvoice-v1")}
    assert "longanhuan_v3.6" in qwen
    assert "longyuan" in cosy
    assert not (qwen & cosy), "两组音色不许有交集"


def test_the_instruction_field_only_goes_out_when_it_has_a_value(monkeypatch):
    """`instruction` 只有 qwen-audio-3.0-tts-* 支持,不支持的模型收到它会 400。
    空字符串和「不发这个字段」在服务端不是一回事。"""
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")

    bare = FakeTransport()
    DashScopeTtsProvider(transport=bare).synthesize("你好")
    assert "instruction" not in bare.posted[0][1]["input"]

    told = FakeTransport()
    DashScopeTtsProvider(transport=told, instruction="  用温柔的语气  ").synthesize("你好")
    assert told.posted[0][1]["input"]["instruction"] == "用温柔的语气"


def test_a_cloud_tts_without_a_key_does_not_fall_back_to_the_local_voice():
    """缺 key 时**静音并报警告**,不退回本机 VITS。

    一个要求 longyuan 的人拿到 VITS 的默认女声会以为配置生效了 —— 那比不出声更糟,
    因为它把一个配置错误伪装成了一次成功。
    """
    from vox_plugin.voice_stack import _open_tts

    warnings: list[str] = []
    resolved = {
        "tts.provider": "dashscope",
        "tts.model": "cosyvoice-v2",
        "tts.voice": "longyuan",
        "tts.key_env": "VOX_DEFINITELY_NOT_SET_KEY",
        "tts.speed": 1.0,
        "tts.num_threads": 2,
        "tts.speaker_id": 0,
        "tts_dir": "models/vits-melo-tts-zh_en",
    }
    assert _open_tts(resolved, warnings) is None
    assert warnings and "VOX_DEFINITELY_NOT_SET_KEY" in warnings[0]


def test_the_configured_instruction_reaches_the_provider(monkeypatch):
    """2026-08-30 查出的断线：``_open_tts`` 建 provider 时**没传 instruction**。

    后果不是「少一个可选项」：`config/voice.toml` 里那句「用温柔、亲和、放松的语气说」、
    控制台上那一栏、以及为它写的整段注释全部对生产无效 —— 听到的一直是裸音色。而
    `EDITABLE` 里有 `tts.instruction`，所以页面上改完会显示保存成功。**一个能改、能存、
    不生效的配置项比没有这个配置项糟得多**，因为它让人以为试过了。
    """
    monkeypatch.setenv("VOX_DASHSCOPE_KEY", "sk-not-a-real-key")
    from vox_plugin.voice_stack import _open_tts

    warnings: list[str] = []
    tts = _open_tts(
        {
            "tts.provider": "dashscope",
            "tts.model": "qwen-audio-3.0-tts-plus",
            "tts.voice": "longanhuan_v3.6",
            "tts.instruction": "  用温柔、亲和、放松的语气说  ",
            "tts.key_env": "VOX_DASHSCOPE_KEY",
            "tts.speed": 1.0,
            "tts.num_threads": 2,
            "tts.speaker_id": 0,
            "tts_dir": "models/vits-melo-tts-zh_en",
        },
        warnings,
    )
    assert warnings == []
    assert tts is not None
    assert tts.instruction == "用温柔、亲和、放松的语气说"


# ------------------------------------------- 失败分类(2026-09-01 那次 401 的账)


@pytest.mark.parametrize(
    "code, must_contain",
    [
        (400, "model 与 voice"),
        (401, "$VOX_TTS_KEY"),
        (403, "不许调这个模型"),
        (404, "路径不对"),
        (411, "音色名无效"),
        (429, "限流"),
        (500, "服务端出错"),
        (502, "服务端出错"),
        (418, "端点拒绝"),
    ],
)
def test_every_status_says_which_thing_to_change(code, must_contain):
    """状态码要分类，而且每一条都得说**该动哪里**。

    这一组是 2026-09-01 那次故障的账。那天这一层报的原话是
    `https://dashscope.aliyuncs.com 回 HTTP 401: {"code":"InvalidApiKey",...}` ——
    技术上完全正确，而它没有回答唯一要紧的问题：**哪个变量装错了**。于是「回答不出声」
    被当成合成模型的问题查了好几轮，真因是 `config/voice.toml` 的 `key_env` 指向
    `VOX_DASHSCOPE_KEY`，而那个变量里装的是中转站的 key。

    401 与 403 分开尤其重要：前者换变量，后者换模型（或去开通）。把两者合成一句
    「密钥有问题」会把人推向错误的那一侧。
    """
    from core.audio.tts_cloud import _classify

    message = _classify(code, '{"code":"InvalidApiKey"}', "VOX_TTS_KEY")
    assert f"HTTP {code}" in message
    assert must_contain in message


def test_a_failure_never_carries_the_key_itself(monkeypatch):
    """整条链上只许出现变量**名**。这一条钉的是分类文本本身 —— 它是唯一带
    `key_env` 的地方，而一个把值格式化进去的手滑会让密钥进日志、进事件流。"""
    from core.audio.tts_cloud import _STATUS_HINTS, _classify

    for code in (*_STATUS_HINTS, 500, 418):
        message = _classify(code, "detail", "VOX_TTS_KEY")
        assert "sk-" not in message


def test_the_variable_the_runtime_reads_is_settable_from_the_console():
    """**这一条比「VOX_DASHSCOPE_KEY 在白名单里」重要。**

    运行时读的是 `config/voice.toml` 的 `tts.key_env`，而白名单此前把那个名字**写死**在
    `EXTRA_SECRET_NAMES` 里。于是把 `tts.key_env` 改到另一个变量之后，页面上存不进那个
    变量的值，而界面只会说「不允许设这个名字」—— 配置改了、白名单没跟上。
    """
    from core.audio.config import load_voice_config
    from core.console.routes import allowed_secret_names

    live = str(load_voice_config()["tts.key_env"])
    assert live in allowed_secret_names(), f"运行时读 {live}，而控制台存不进它"


def test_the_shipped_tts_key_is_not_shared_with_another_role():
    """出厂配置里 TTS 的变量名不许和别的角色共用。

    2026-09-01 的故障就是共用：`config/models.toml` 的 `[profiles.local.llm]`（端点是
    中转站）也点名 `VOX_DASHSCOPE_KEY`，而 `config/voice.toml` 的 TTS 读同一个变量。
    为了让聊天能用往里存中转站的 key，百炼就回 401 —— **把一边修好等于把另一边弄坏**，
    而两边都显示「已配置」。
    """
    from core.audio.config import load_voice_config
    from core.models_config import load_models_config, models_config_path

    tts_env = str(load_voice_config()["tts.key_env"])
    config = load_models_config(models_config_path())
    for name, profile in config["profiles"].items():
        llm = profile.get("llm", {})
        if isinstance(llm, Mapping):
            assert llm.get("key_env", "") != tts_env, (
                f"profiles.{name}.llm 和 TTS 共用 {tts_env}"
            )


# ------------------------------------- 流式合成（2026-09-01 的首声延迟）


class _FakeSse:
    """替掉 urlopen：按帧吐 SSE 行，记下请求头与请求体。"""

    def __init__(self, frames, *, finish: str = "stop") -> None:
        self.frames = frames
        self.finish = finish
        self.request = None
        self.closed = False

    def __call__(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False

    def __iter__(self):
        import base64 as b64
        import json as js

        for frame in self.frames:
            payload = {"output": {"audio": {"data": b64.b64encode(frame).decode("ascii")}}}
            yield b"data: " + js.dumps(payload).encode("utf-8") + b"\n"
            yield b"\n"
        yield b"data: " + js.dumps({"output": {"finish_reason": self.finish}}).encode("utf-8") + b"\n"


def _pcm(values) -> bytes:
    return np.asarray(values, dtype="<i2").tobytes()


def test_streaming_is_the_default_and_asks_for_raw_pcm(monkeypatch):
    """默认走 SSE，而且请求的是裸 pcm。

    非流式那条路有约 3.3 秒的**固定**开销（3 个字也要 3353 ms），因为它要等整句合成完
    再 GET 下载一次。实测 SSE 的首块 2.3–2.6 s 且**与句子长度基本无关**，音频到手
    2735/3117/3783 ms（3/11/38 字）—— 见 ``DashScopeTtsProvider.stream`` 的表。
    每帧再带一个 wav 头没有意义，所以流式请求 pcm。
    """
    monkeypatch.setenv("VOX_TTS_KEY", "sk-not-a-real-key")
    fake = _FakeSse([_pcm([0, 16384, -16384, 0])])
    monkeypatch.setattr("core.audio.tts_cloud.urlopen", fake)
    provider = DashScopeTtsProvider(key_env="VOX_TTS_KEY", sample_rate=24000)
    assert provider.stream is True

    audio = provider.synthesize("好的。")

    sent = json.loads(fake.request.data.decode("utf-8"))
    assert sent["input"]["format"] == "pcm"
    assert fake.request.headers["X-dashscope-sse"] == "enable"
    assert audio.sample_rate == 24000, "裸 PCM 不自带采样率，只能用请求里那个"
    assert audio.samples.dtype == np.float32
    assert audio.samples.tolist() == pytest.approx([0.0, 0.5, -0.5, 0.0], abs=1e-4)


def test_frames_are_concatenated_in_order(monkeypatch):
    monkeypatch.setenv("VOX_TTS_KEY", "sk-not-a-real-key")
    fake = _FakeSse([_pcm([1000, 2000]), _pcm([3000]), _pcm([4000, 5000, 6000])])
    monkeypatch.setattr("core.audio.tts_cloud.urlopen", fake)
    audio = DashScopeTtsProvider(key_env="VOX_TTS_KEY").synthesize("测试")
    assert len(audio.samples) == 6
    assert audio.samples[0] * 32768 == pytest.approx(1000, abs=1)
    assert audio.samples[-1] * 32768 == pytest.approx(6000, abs=1)


def test_a_stream_with_no_audio_frames_raises_rather_than_returning_silence(monkeypatch):
    """一段静音会被读成「合成成功但没声音」—— 和非流式那条路同一条判断。"""
    monkeypatch.setenv("VOX_TTS_KEY", "sk-not-a-real-key")
    monkeypatch.setattr("core.audio.tts_cloud.urlopen", _FakeSse([], finish="length"))
    with pytest.raises(DashScopeTtsError, match="一帧音频都没有"):
        DashScopeTtsProvider(key_env="VOX_TTS_KEY").synthesize("测试")


def test_a_corrupt_frame_does_not_lose_the_rest(monkeypatch):
    """一帧读坏了不该让整句失败：后面的帧仍然有用。"""
    monkeypatch.setenv("VOX_TTS_KEY", "sk-not-a-real-key")

    class Broken(_FakeSse):
        def __iter__(self):
            yield b"data: {not json\n"
            yield b"data: " + json.dumps(
                {"output": {"audio": {"data": "!!!not base64!!!"}}}
            ).encode("utf-8") + b"\n"
            yield from _FakeSse.__iter__(self)

    monkeypatch.setattr("core.audio.tts_cloud.urlopen", Broken([_pcm([8000])]))
    audio = DashScopeTtsProvider(key_env="VOX_TTS_KEY").synthesize("测试")
    assert len(audio.samples) == 1


def test_a_barge_in_stops_reading_the_stream(monkeypatch):
    """``stop()`` 在**帧之间**生效 —— 打断不必等整句合成完。

    这是流式相对两个往返的第二个好处：非流式那条路上 `stop()` 只能在两段之间起作用，
    因为那一段的音频要么整块到手要么没到。这里让打断发生在第 2 帧下发之前，断言收到的
    样本数停在第 1 帧 —— 也就是「连接立刻不再读」，而不是「读完再丢掉」。
    """
    monkeypatch.setenv("VOX_TTS_KEY", "sk-not-a-real-key")
    provider = DashScopeTtsProvider(key_env="VOX_TTS_KEY")

    class Barging(_FakeSse):
        def __init__(self, frames) -> None:
            super().__init__(frames)
            self.yielded = 0

        def __iter__(self):
            import base64 as b64

            for index, frame in enumerate(self.frames):
                payload = {"output": {"audio": {"data": b64.b64encode(frame).decode("ascii")}}}
                self.yielded += 1
                yield b"data: " + json.dumps(payload).encode("utf-8") + b"\n"
                if index == 0:
                    provider.stop()  # 第一帧到手之后有人喊了唤醒词

    fake = Barging([_pcm([100] * 10), _pcm([200] * 10), _pcm([300] * 10)])
    monkeypatch.setattr("core.audio.tts_cloud.urlopen", fake)
    audio = provider.synthesize("测试")
    assert len(audio.samples) == 10, "只该收到第一帧"
    assert fake.yielded < 3, "剩下的帧不该再被读"
    assert provider.is_stopped() is True


def test_an_injected_transport_still_uses_the_two_round_trip_path(monkeypatch):
    """注入 transport 时走非流式那条：测试替掉的是 post/get 两个方法。

    这一条保住的是**可测性**：流式那条路要替 urlopen，而已有的十几条测试都建立在
    transport 上。两条路共存不是过渡状态 —— 有些部署（代理、离线镜像）只能走 GET。
    """
    monkeypatch.setenv("VOX_TTS_KEY", "sk-not-a-real-key")
    transport = FakeTransport()
    audio = DashScopeTtsProvider(key_env="VOX_TTS_KEY", transport=transport).synthesize("你好")
    assert transport.fetched == ["https://oss/x.wav"]
    assert audio.sample_rate == 24000
