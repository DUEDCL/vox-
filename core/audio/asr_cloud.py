"""云端语音识别：把整段话交给百炼的 `qwen-audio-3.0-asr-flash`（ADR 009）。

摆的是和 ``SherpaStreamingAsrProvider`` **完全同一个形状**（`load` / `create_stream` /
`feed` / `finalize` / `reset` / `close`），所以 ``capture.py`` 一个字都不用改 —— 这正是
红线 2 说的「组件可替换」。

## 为什么要这一层：本机模型听错的那些字

2026-09-03 同一条真录音，同一只麦克风，两条路各跑一次：

| 路 | 转写 |
|---|---|
| 本机 `zipformer-zh-14M` | 你好小**吴**检查目前运行状态是否正常 |
| **云端 `qwen-audio-3.0-asr-flash`** | **你好，小沃，检查目前运行状态是否正常。** |

云端全对，还带标点。差别不在阈值上：本机那个 14M 模型的字表只有 1426 个汉字，「沃」不在
里面 —— 它**写不出**这个字，所以任何热词、任何束宽都救不回来。使用者两次报「语音转文字
还是不够精准」，根因就是这个。

## 三件必须说清楚的事

**1. 音频会出网。** 这条改了红线 1 的措辞：从「ASR 全部本机执行」变成「唤醒与声纹本机，
识别与合成上云」。**唤醒词与声纹仍然一步都不出网** —— 只有「被接受的唤醒之后说的那句话」
会以 base64 内联进一次 HTTPS POST。不写盘、不留缓存、不带 URL（见下一条）。

**2. 必须内联 base64，不能给 URL。** 实测三种形状：

| 形状 | 结果 |
|---|---|
| `data:audio/wav;base64,…` + `parameters.format` | **200，转写正确** |
| 裸 base64（不带 `data:` 前缀） | 500 `Cannot run program "/usr/bin/wget": Argument list too long` |
| 缺 `parameters.format` | 400 `UNSUPPORTED_FORMAT: format is empty` |

第二行说明服务端把不带前缀的字符串**当 URL 拿 wget 去下载** —— 那条路要求先把使用者的
录音传到一个公网可取的地方，本项目不做。第三行是使用者报的那个 400 的真正原因：他配的是
`compatible-mode/v1`（OpenAI 协议），而那条路**没有地方放 `parameters.format`**，所以
兼容端点上这个模型恒回 `format is empty`。**只能走原生端点。**

**3. 它不流式，所以端点检测必须本机做。** 这个接口是一次 POST 整段音频（实测 7.15 s 的
录音往返 4.3–7.8 s），没有增量端点。于是这一层自己拿 Silero VAD 判「人说完了没有」，说完
才发一次请求。首字延迟 = 整段延迟，这是端点的属性，不给它套「看起来增量」的外壳。

## 为什么 HTTP 不在音频回调里发

``capture._recognize()`` 跑在 sounddevice 的音频回调线程上。在那里阻塞 4–8 秒会丢帧，
而丢帧的表现是**「识别器听错」而不是「卡住」** —— 一个最难查的症状。所以：

* ``feed()`` 只攒样本 + 跑 VAD。判到句末就把这段音频**交给工作线程**，然后返回
  ``is_endpoint=False``（还没有文本，聆听继续）；
* 之后每一块 ``feed()`` 顺手看一眼工作线程好了没。好了就返回
  ``AsrResult(text, is_endpoint=True)``，``finalize()`` 把攒好的文本交出去。

于是音频回调永不碰网络，而 ``capture.py`` 的两段模式逻辑一行都不用动。
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.outbound import API_USER_AGENT

from .asr import AsrResult
from .base import ProviderStatus, ProviderUnavailable
from .vad import SileroSpeechGate

#: 原生多模态端点。**不是** `compatible-mode/v1` —— 见模块头第 2 条。
DEFAULT_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)

#: 出厂模型。flash 档：实测 7 秒音频 4.3–7.8 s 往返。
DEFAULT_MODEL = "qwen-audio-3.0-asr-flash"

#: 密钥的**变量名**。与 TTS 分开是有教训的：`VOX_DASHSCOPE_KEY` 里装过中转站的 key，
#: 一个变量服务两个角色时，把一边修好就等于把另一边弄坏。
DEFAULT_KEY_ENV = "VOX_ASR_KEY"

#: 判「说完了」的尾部静音。0.8 s 比本机流式的 rule2（1.2 s）短：云端往返本身就要几秒，
#: 端点上省下来的每 100 ms 都直接落在使用者的等待里。再短会把句中的换气切成两句。
DEFAULT_SILENCE_S = 0.8

#: 一段最长多久。到这个长度不等静音也发 —— 一个没有句末的长句（念清单、读地址）不该
#: 把整轮卡死。也是费用与请求体的上限：30 s 的 16 kHz 单声道 wav 约 960 KB，base64 后 1.3 MB。
DEFAULT_MAX_UTTERANCE_S = 30.0

#: 一段最短多久才值得发。低于它当没说话 —— 一次咳嗽、一声桌子响都会过 VAD 的 0.25 s 门，
#: 而每一次发出去都是一次计费和一次 4 秒等待。
DEFAULT_MIN_UTTERANCE_S = 0.35

#: HTTP 超时。实测 7 秒音频最慢 7.8 s；30 秒的一段按同比例约 30 s，留一倍余量。
DEFAULT_TIMEOUT_S = 60.0

#: 一轮里最多把几段拼起来。见 ``_poll`` 的续说逻辑 —— 这个上限挡的是「一直说不停」，
#: 到了就把手上的交出去，而不是无限攒下去让使用者等不到回答。
MAX_PARTS = 4

#: 拼接时要从**非最后一段**尾巴上去掉的标点。一段被停顿切开时，那个句号是模型猜的
#: 而不是说话人给的 —— 留着它会让「帮我打开。网易云音乐」这种文本进意图匹配，而
#: `app.open` 的动词锚定会当场对不上。
_TAIL_PUNCT = "。，、；：！？.,;:!?…~ "

#: 一段音频最多重发几次。**只对暂时性失败重发**（超时、连不上、429、5xx），
#: 配置类失败（400/401/403/404）一次都不重发 —— 那只是把真正的原因推迟两秒说出来。
#:
#: 为什么值得重发（而 `tts_cloud` 刻意不重发）：合成失败有退路（降级到本机 VITS，见
#: `tts_fallback.py`），而识别**在一句话中途没有退路** —— 一次 500 的后果是使用者说了一句、
#: 得到一片安静、只能再说一遍。音频已经在手上，重发一次只多花一个往返（实测 3–4 s）。
MAX_ATTEMPTS = 2

#: 两次之间等多久。0.6 s 不是随手取的：这个端点上实测过一次 500（裸 base64 那次）是**立刻**
#: 回的，所以等待的意义不是「让服务器缓一缓」而是「不要在同一毫秒里再撞一次」。
#: 更长的等待直接加在使用者的沉默里，而这一层已经在 3–5 s 的往返上了。
RETRY_WAIT_S = 0.6

#: 值得重发的 HTTP 状态码。429 是限流（免费额度上真实存在），5xx 是服务端。
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

#: 采样率。三个模型共同的输入约定，改它等于换模型。
SAMPLE_RATE = 16000


class DashScopeAsrError(RuntimeError):
    """一次转写没成。带端点主机名与状态码分类，**绝不带 key、绝不带音频**。

    ``retryable`` 让上层分得清「重发一次可能就好了」和「配置错了，重发一百次也一样」。
    没有这个区分的重试会把一个 401 变成两次 401 加两秒沉默。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


#: 状态码 → 这台机器上该动哪里。和 tts_cloud 同一个立场：一句正确而不可操作的
#: `回 HTTP 400: {...}` 会让人往错的方向查好几轮。这张表的每一条都是实测出来的。
_STATUS_HINTS: dict[int, str] = {
    400: "请求形状不对。实测两种：缺 parameters.format 回 UNSUPPORTED_FORMAT "
    "`format is empty`；走 compatible-mode/v1 也回同一条（那条路没地方放 format）—— "
    "端点必须是 api/v1/services/aigc/multimodal-generation/generation",
    401: "密钥不对：${key_env} 里的值不是百炼认的 key。一个变量只服务一个角色 —— "
    "中转站的 key 放在这个变量里，症状就是这一行",
    403: "密钥对，但这个账号不许调这个模型。换模型或去百炼控制台开通，不是换 key",
    404: "路径不对 —— 主机在，但这个端点不是多模态生成接口",
    429: "被限流或额度用尽。这一层在途请求最多一个，所以这是账号侧的额度",
}


def _classify(code: int, key_env: str) -> str:
    hint = _STATUS_HINTS.get(code)
    if hint is None:
        hint = (
            "服务端出错。实测过一条：`input_audio.data` 不带 `data:` 前缀时服务端会把它"
            "当 URL 拿 wget 去下载，回 500 `Argument list too long`"
            if code >= 500
            else "端点拒绝了这次请求"
        )
    return f"HTTP {code} —— {hint.format(key_env=key_env)}"


def to_wav_bytes(samples: Any, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """float32 序列 → 16-bit 单声道 wav 字节。**在内存里，不落盘。**

    落盘会给这套东西加一个「音频文件曾经存在于磁盘上」的窗口，而红线 1 的整个重点就是
    音频不留痕。``io.BytesIO`` 让 ``wave`` 照常写它的头。
    """
    import numpy as np

    values = np.asarray(samples, dtype="float32").reshape(-1)
    # 先钳后转：超过 ±1.0 的样本在 int16 里会绕成反相的噪声（一段削波变成一段撕裂声），
    # 而自适应增益的输出理论上不越界、实际上偶尔擦边。
    clipped = np.clip(values, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


@dataclass
class _CloudStream:
    """一次聆听的状态。``capture`` 拿它当不透明句柄，只在 ``feed`` 里被读写。

    ``pending`` 是「已经交给工作线程、还没拿回文本」的那一段。**它存在的期间 feed 照常
    收音频、照常跑 VAD**，只是不再发第二个请求 —— 云端往返要 3–5 秒，而使用者不知道自己
    被切断了，那几秒里说的话必须还在。丢掉它的表现是「它总听半句」。

    ``parts`` 是被停顿切开的前几段。见 ``_poll``：文本回来那一刻如果人又在说了，这一段
    就不是句末而是句中，于是攒起来等下一段。
    """

    samples: list[Any] = field(default_factory=list)
    frames: int = 0
    speech_frames: int = 0
    silence_frames: int = 0
    started_at: float = field(default_factory=time.monotonic)
    pending: _Transcribe | None = None
    parts: list[str] = field(default_factory=list)
    text: str = ""
    gate: SileroSpeechGate | None = None


class _Transcribe:
    """在别的线程上跑一次 HTTP 转写。

    和 ``tts_cloud._Ahead`` 同一个形状与同一个理由：音频回调线程绝不能阻塞。daemon —— 被
    打断之后没人来取这段结果，而非 daemon 线程会让进程退出时干等那 60 秒超时走完。
    """

    __slots__ = ("_text", "_error", "_done", "started_at", "seconds")

    def __init__(self, transcribe: Any, audio: Any, seconds: float) -> None:
        self._text = ""
        self._error = ""
        self._done = threading.Event()
        self.started_at = time.monotonic()
        self.seconds = float(seconds)
        threading.Thread(
            target=self._run, args=(transcribe, audio), daemon=True, name="vox-asr-cloud"
        ).start()

    def _run(self, transcribe: Any, audio: Any) -> None:
        try:
            self._text = transcribe(audio)
        except Exception as exc:  # noqa: BLE001 - 失败要变成读数，不是崩在工作线程里
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._done.set()

    @property
    def ready(self) -> bool:
        return self._done.is_set()

    @property
    def text(self) -> str:
        return self._text

    @property
    def error(self) -> str:
        return self._error


class DashScopeAsrProvider:
    """整段上云的识别器，本机 VAD 判端点，HTTP 在工作线程上跑。"""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        key_env: str = DEFAULT_KEY_ENV,
        endpoint: str = DEFAULT_ENDPOINT,
        silence_s: float = DEFAULT_SILENCE_S,
        max_utterance_s: float = DEFAULT_MAX_UTTERANCE_S,
        min_utterance_s: float = DEFAULT_MIN_UTTERANCE_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        language: str = "",
        vad_model: str | None = None,
        transport: Any = None,
    ) -> None:
        self.model = str(model).strip() or DEFAULT_MODEL
        self.key_env = str(key_env).strip() or DEFAULT_KEY_ENV
        self.endpoint = str(endpoint).strip() or DEFAULT_ENDPOINT
        self.silence_s = float(silence_s)
        self.max_utterance_s = float(max_utterance_s)
        self.min_utterance_s = float(min_utterance_s)
        self.timeout_s = float(timeout_s)
        #: 语言提示。空 = 让模型自己判。中英混说的场景下**不要**填死 zh：填了之后
        #: 英文词会被硬转成音近的汉字。
        self.language = str(language).strip()
        self.vad_model = vad_model
        #: 注入点，给测试用。有它就不打网络。
        self.transport = transport
        #: 读数。**每一项都要能在就绪清单上看到** —— 云端那条路的失败只在真的转写时才
        #: 暴露，没有这些计数的话「为什么这句没转出来」查不下去。
        self.requests = 0
        self.failures = 0
        self.empty_results = 0
        #: 被停顿切开又拼回去的次数。**这是判断 `silence_s` 调对了没有的唯一读数** ——
        #: 它一直涨说明那个阈值对这个人说话的节奏太短了。
        self.continuations = 0
        #: 暂时性失败之后重发的次数，以及最后一次重发的原因。**必须分开计** ——
        #: `failures` 是「最终没成」，`retries` 是「第一次没成但救回来了」，把两者混在一个
        #: 数字里会让「网络在抖」和「配置是错的」看起来一样。
        self.retries = 0
        self.last_retry = ""
        self.last_error = ""
        self.last_latency_ms = 0
        self.last_seconds = 0.0
        self.total_seconds = 0.0

    # -- 与本机 provider 同形的那几个 ---------------------------------------

    @property
    def available(self) -> bool:
        """有 key 就算可用。**不预热网络** —— 建一个栈只为看一眼它（控制台的就绪清单
        每次刷新都在做这件事）不该产生一次计费请求。
        """
        if self.transport is not None:
            return True
        return bool(os.getenv(self.key_env, "").strip())

    def _safe_endpoint(self) -> str:
        """报主机名，不报完整 URL —— query 里可能被人塞过东西。"""
        from urllib.parse import urlsplit

        parts = urlsplit(self.endpoint)
        return f"{parts.scheme}://{parts.netloc}"

    def load(self) -> ProviderStatus:
        details: dict[str, Any] = {
            "engine": "dashscope",
            "model": self.model,
            "endpoint": self._safe_endpoint(),
            "key_env": self.key_env,
        }
        if not self.available:
            return ProviderStatus(
                False,
                self._safe_endpoint(),
                {
                    **details,
                    "reason": f"{self.key_env} 没有值 —— 云端识别要一个百炼 key，"
                    "在控制台「密钥」那一栏存进去，或者写进 .env",
                },
            )
        return ProviderStatus(True, self._safe_endpoint(), details)

    def create_stream(self) -> Any:
        """一次聆听一条流。VAD 也一条 —— 它是有状态的，跨轮复用会让上一句的尾巴影响这一句。"""
        gate = SileroSpeechGate(
            self.vad_model,
            sample_rate=SAMPLE_RATE,
            # 句中换气 0.2–0.5 s 很常见，min_silence 必须比它长否则一句被切成两句。
            min_silence_duration=max(0.30, self.silence_s * 0.5),
        )
        return _CloudStream(gate=gate)

    def feed(self, stream: Any, samples: Any, sample_rate: int = SAMPLE_RATE) -> AsrResult:
        """收一块音频。**永不阻塞**，返回的 ``is_endpoint`` 只在文本到手时才为真。

        ``sample_rate`` 收下但不重采样：capture 的流是 16 kHz 开的，一个不是 16 k 的
        输入是配置错误而不是这一层该修补的事。
        """
        import numpy as np

        block = np.asarray(samples, dtype="float32").reshape(-1)

        # **无论在不在等云端都先收下。** 收音频这件事不能因为「上一段还在路上」而停：
        # 使用者不知道自己被切断了，那 3–5 秒里说的话必须还在缓冲里。
        stream.samples.append(block.copy())
        stream.frames += int(block.size)

        speaking = self._speaking(stream, block)
        if speaking:
            stream.speech_frames += int(block.size)
            stream.silence_frames = 0
        else:
            stream.silence_frames += int(block.size)

        # 已经有一段在路上：不发第二个请求（在途最多一个，免费额度的并发限制是真实的）。
        if stream.pending is not None:
            return self._poll(stream)

        seconds = stream.frames / float(SAMPLE_RATE)
        silence = stream.silence_frames / float(SAMPLE_RATE)
        speech = stream.speech_frames / float(SAMPLE_RATE)

        long_enough = speech >= self.min_utterance_s
        ended = long_enough and silence >= self.silence_s
        overflowed = seconds >= self.max_utterance_s

        if not (ended or overflowed):
            return AsrResult("", False)

        if not long_enough:
            # 到了长度上限但一句话都没有：把缓冲清空重新开始，别把 30 秒的底噪发上去。
            self._rewind(stream)
            return AsrResult("", False)

        self._dispatch(stream)
        return self._poll(stream)

    def _speaking(self, stream: Any, block: Any) -> bool:
        """这一块里有人说话吗。VAD 起不来时**恒真** —— 那时端点只由静音长度决定，
        而静音长度在没有 VAD 的情况下永远不增长，于是退化成「说满 max_utterance_s 才发」。
        比「一块都不发」好，而且 ``describe()`` 里 `vad` 那一项会说 VAD 没起来。
        """
        gate = stream.gate
        if gate is None:
            return True
        try:
            return bool(gate(block))
        except Exception:  # noqa: BLE001 - 和 SileroSpeechGate 自己的姿态一致：坏了就放行
            return True

    def _rewind(self, stream: Any) -> None:
        stream.samples.clear()
        stream.frames = 0
        stream.speech_frames = 0
        stream.silence_frames = 0
        stream.started_at = time.monotonic()
        if stream.gate is not None:
            stream.gate.reset()

    def _dispatch(self, stream: Any) -> None:
        """把攒好的一段交给工作线程，缓冲当场清空。"""
        import numpy as np

        audio = np.concatenate(stream.samples) if stream.samples else np.zeros(0, dtype="float32")
        seconds = audio.size / float(SAMPLE_RATE)
        self._rewind(stream)
        stream.pending = _Transcribe(self._transcribe, audio, seconds)

    def _poll(self, stream: Any) -> AsrResult:
        """看一眼工作线程。没好就继续听，好了就决定「这是句末」还是「他还在说」。

        **续说判定是这一层最要紧的一件事。** 一次停顿会被当成句末（0.8 秒静音就发），
        而人说「帮我打开……网易云音乐」中间那一下停顿是句中不是句末。判据是**文本回来
        那一刻人有没有又在说了**：那时距离发出去已经过了 3–5 秒，所以这个判断拿到的是
        真实的后续语音，不是猜的。
        """
        pending = stream.pending
        if pending is None or not pending.ready:
            return AsrResult("", False)
        stream.pending = None
        self.requests += 1
        self.last_latency_ms = int((time.monotonic() - pending.started_at) * 1000)
        self.last_seconds = pending.seconds
        self.total_seconds += pending.seconds
        if pending.error:
            self.failures += 1
            self.last_error = pending.error
            # 空文本 + 端点 = capture 的 `_restart_or_expire`：宽限期内继续听。
            # 一次网络失败不该把整轮结束掉，而失败原因已经进了 last_error 与日志。
            # 手上攒着的前几段一起丢：半句话派给 agent 比不派更糟。
            stream.parts.clear()
            return AsrResult("", True)
        text = pending.text.strip()
        if not text:
            self.empty_results += 1
            if not stream.parts:
                return AsrResult("", True)
            # 前面有段、这一段空：把前面的交出去，别因为一段静音把已经听到的话丢了。
            stream.text = _join(stream.parts)
            stream.parts.clear()
            return AsrResult(stream.text, True)

        resumed = (stream.speech_frames / float(SAMPLE_RATE)) >= self.min_utterance_s
        if resumed and len(stream.parts) + 1 < MAX_PARTS:
            stream.parts.append(text)
            self.continuations += 1
            return AsrResult("", False)

        stream.text = _join([*stream.parts, text])
        stream.parts.clear()
        return AsrResult(stream.text, True)

    def finalize(self, stream: Any) -> str:
        """把最后一次 ``feed`` 攒下的文本交出去。**不再发请求。**

        capture 在看到 ``is_endpoint=True`` 之后立刻调它，而那时文本已经在 ``stream.text``
        里了。在这里发请求会让它跑在音频回调线程上 —— 正是这一层要避免的那件事。
        """
        text, stream.text = stream.text, ""
        return text

    def reset(self, stream: Any) -> None:
        self._rewind(stream)
        stream.pending = None
        stream.parts.clear()
        stream.text = ""

    def close(self) -> None:
        return None

    # -- HTTP ----------------------------------------------------------------

    def _transcribe(self, audio: Any) -> str:
        """一次 POST。**跑在工作线程上**，所以这里可以阻塞。"""
        wav = to_wav_bytes(audio)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    # `data:` 前缀是必需的：不带它服务端会把整串 base64
                                    # 当 URL 拿 wget 去下载，回 500。见模块头那张表。
                                    "data": "data:audio/wav;base64,"
                                    + base64.b64encode(wav).decode("ascii")
                                },
                            }
                        ],
                    }
                ]
            },
            # 两项都是必需的。缺 format 回 400 `format is empty` —— 使用者报的那个 400。
            "parameters": {"format": "wav", "sample_rate": str(SAMPLE_RATE)},
        }
        if self.language:
            payload["parameters"]["language"] = self.language
        return _text_of(self._post_with_retry(payload))

    def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发一次，暂时性失败再发一次。**跑在工作线程上**，所以这里可以睡。

        只重发暂时性的那几类（见 ``_RETRYABLE``）。配置类失败一次都不重发 —— 重发一个 401
        只是把「你的变量装错了」这句话推迟两秒说出来，而这两秒落在使用者的沉默里。
        """
        last: DashScopeAsrError | None = None
        for attempt in range(1, max(1, MAX_ATTEMPTS) + 1):
            try:
                return self._post(payload)
            except DashScopeAsrError as exc:
                if not exc.retryable or attempt >= MAX_ATTEMPTS:
                    raise
                last = exc
                self.retries += 1
                self.last_retry = str(exc)
                time.sleep(RETRY_WAIT_S)
        # 只有 MAX_ATTEMPTS <= 0 时才可能走到这里（配置写错），把最后一条原样抛出去。
        raise last or DashScopeAsrError(f"{self._safe_endpoint()} 一次都没发出去")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            return dict(self.transport.post(self.endpoint, payload))
        key = os.getenv(self.key_env, "").strip()
        if not key:
            raise ProviderUnavailable(f"{self.key_env} 没有值")
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                # 显式关掉 SSE。默认行为在这个端点上不稳定，而流式对一次整段转写没有意义。
                "X-DashScope-SSE": "disable",
                "User-Agent": API_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                return dict(json.loads(response.read().decode("utf-8", "replace")))
        except HTTPError as exc:
            # **不回显请求体**：它带 key，也带整段音频的 base64。只报状态码分类。
            code = int(exc.code)
            raise DashScopeAsrError(
                f"{self._safe_endpoint()} {_classify(code, self.key_env)}",
                retryable=code in _RETRYABLE,
            ) from exc
        except TimeoutError as exc:
            raise DashScopeAsrError(
                f"{self._safe_endpoint()} 超时（{self.timeout_s:.0f}s）—— "
                "整段音频一次传完才开始识别，长句子上调大 asr.timeout_s",
                retryable=True,
            ) from exc
        except URLError as exc:
            raise DashScopeAsrError(
                f"{self._safe_endpoint()} 连不上：{exc.reason} —— 请求还没发出，"
                "检查网络或代理，不是密钥问题",
                retryable=True,
            ) from exc
        except json.JSONDecodeError as exc:
            # 半个 JSON 通常是连接被截断，重发一次值得试 —— 但只试一次。
            raise DashScopeAsrError(
                f"{self._safe_endpoint()} 回的不是 JSON：{exc}", retryable=True
            ) from exc

    def take_error(self) -> str:
        """把「上一次失败的原因」取走（取完清空）。

        取走而不是只读的理由：``last_error`` 会一直留着，而调用方（``runtime`` 的
        ``_listen_expired``）每次聆听超时都会问一遍。只读的话**一次 401 会让之后每一次
        正常的「没人说话」超时都跟着报同一条错**，而那种日志比没有日志更误导人。
        """
        error, self.last_error = self.last_error, ""
        return error

    def describe(self) -> dict[str, Any]:
        return {
            "engine": "dashscope",
            "model": self.model,
            "endpoint": self._safe_endpoint(),
            "key_env": self.key_env,
            "available": self.available,
            "requests": int(self.requests),
            "failures": int(self.failures),
            "empty": int(self.empty_results),
            "continuations": int(self.continuations),
            "retries": int(self.retries),
            "last_retry": self.last_retry,
            "last_latency_ms": int(self.last_latency_ms),
            "last_seconds": round(float(self.last_seconds), 2),
            "total_seconds": round(float(self.total_seconds), 1),
            "silence_s": self.silence_s,
            "error": self.last_error,
        }


def _join(parts: Any) -> str:
    """把被停顿切开的几段拼成一句。**非最后一段的句末标点去掉。**

    那个句号是模型给一个「被截断的片段」猜的，不是说话人给的。留着它的话
    「帮我打开。网易云音乐」会进意图匹配，而 `app.open` 的动词锚定要的是
    「帮我打开网易云音乐」—— 一个多出来的句号足以让整条快路径失配、落到 agent 上。
    """
    texts = [str(item).strip() for item in parts]
    texts = [text for text in texts if text]
    if not texts:
        return ""
    head = [text.rstrip(_TAIL_PUNCT) for text in texts[:-1]]
    return "".join([*head, texts[-1]])


def _text_of(payload: Any) -> str:
    """从回包里挖出转写文本。

    实测这个端点的回包**嵌了两层 ``output``**，而里面既可能是
    ``output.output.sentence.text``（ASR 专用形状）也可能是
    ``output.choices[].message.content[].text``（多模态通用形状）。两条都认：一个只认
    一种形状的解析器会在服务端换形状那天变成「转写恒为空」，而那是静默失败。
    """
    if not isinstance(payload, dict):
        return ""
    output = payload.get("output")
    if isinstance(output, dict):
        inner = output.get("output")
        if isinstance(inner, dict):
            text = _sentence_text(inner)
            if text:
                return text
        text = _choice_text(output.get("choices"))
        if text:
            return text
        text = _sentence_text(output)
        if text:
            return text
    return _choice_text(payload.get("choices"))


def _sentence_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    sentence = node.get("sentence")
    if isinstance(sentence, dict):
        return str(sentence.get("text", "")).strip()
    if isinstance(sentence, list):
        parts = [
            str(item.get("text", "")) for item in sentence if isinstance(item, dict)
        ]
        return "".join(parts).strip()
    return str(node.get("text", "")).strip()


def _choice_text(choices: Any) -> str:
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
        return "".join(parts).strip()
    return ""


__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_KEY_ENV",
    "DEFAULT_MODEL",
    "DashScopeAsrError",
    "DashScopeAsrProvider",
    "to_wav_bytes",
]
