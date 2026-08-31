"""云端 TTS：阿里云百炼（DashScope）的 CosyVoice / Qwen-Audio-TTS。

## 为什么会有这个文件

`core/audio/tts.py` 里只有 `SherpaTtsProvider`，而它读的是一个**本地模型目录**
（`tts_dir`）。所以在 2026-08-29 之前，「在控制台把 TTS 换成 cosyvoice-v1、音色 longyuan」
这件事**在任何一层都做不到**：

1. 代码里没有云端 TTS 实现，`voice_stack.py` 硬写 `SherpaTtsProvider(resolved["tts_dir"])`；
2. `config/models.toml` 的字段白名单是 `provider/model/base/proto/key_env`，**没有 `voice`**，
   而写白名单外的键会直接报错（那是刻意的）；
3. `models.toml` 根本没有读侧（`docs/backlog.md` B7）—— 语音栈由 `config/voice.toml` +
   四个 `VOX_*_MODEL_DIR` 决定，改 models.toml 不改变任何行为。

这个文件补第 1 条。

## 走 HTTP 非实时接口，不走 WebSocket

百炼的实时合成是 WebSocket（`dashscope.audio.tts_v2`），非实时合成是普通 HTTP POST。
选后者的理由是**不引新依赖**：HTTP 用标准库 `urllib` 就够，WebSocket 要么装
`dashscope` SDK（它自己还拖一串依赖）要么装 `websockets`。红线 2 的代价评估里，为了
「首字延迟从 1.5 s 降到 0.5 s」引入一个 SDK 不值得 —— 这一层本来是可替换的，
将来真需要流式再加一个 provider。

代价说清楚：非实时接口是**整句合成完才返回一个下载链接**，所以首字延迟 = 整句合成时间 +
一次下载。长句子上这比本机 VITS 慢。

**第一句的延迟消不掉，句与句之间的可以。** 一次合成实测 0.7–1.5 s；`speak_segments` 原来是
「合成一段 → 播一段 → 合成下一段」，于是那 0.7–1.5 s 完整地落在两句话之间 —— 这就是
2026-08-30 报的「句子之间的间隔太长，感觉不连贯」。两个改动治它：播放期间预取下一段
（`_Ahead`），以及从第二段起把短句合并成更长的请求（`merge_segments`，顺带让韵律由模型
在一段之内安排，而不是每句各自起调收尾）。

## 端点与形状（2026-08-29 核实，两个来源交叉验证）

```
POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
Authorization: Bearer <key>
Content-Type: application/json

{"model": "cosyvoice-v2",
 "input": {"text": "...", "voice": "longyuan", "format": "wav", "sample_rate": 24000}}
```

回包里音频**不在 body 里**：`output.audio.url` 是一个 24 小时有效的下载链接，
`output.audio.data` 只在流式（`X-DashScope-SSE: enable`）时才有内容。所以一次合成是
**两个** HTTP 往返。

来源：`help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api` 与
`platform.qianwenai.com/docs/api-reference/speech-synthesis/cosyvoice-nrt/http-api`。

## 密钥只从环境变量读

和 `config/agents.toml` 同一条规矩：这个类不接受把 key 当参数传进来的写法之外的任何
来源，默认变量名 `VOX_DASHSCOPE_KEY`。配置文件里只写变量名。
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.outbound import API_USER_AGENT

from .base import ProviderStatus, ProviderUnavailable
from .tts import TtsAudio

#: 非实时合成端点。华北2（北京）；文档同时给出一个 workspace 专属域名，那个需要
#: WorkspaceId，而这个通用域名文档明说仍然可用，所以默认用通用的。
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"

#: 默认变量名。值永远只从环境变量读。
DEFAULT_KEY_ENV = "VOX_DASHSCOPE_KEY"

#: 请求 wav 而不是默认的 mp3：下游是 `soundfile` 解码后直接喂 sounddevice，
#: 而 wav 是无损且解码零配置的那一种。24 kHz 是这批模型的推荐值。
DEFAULT_FORMAT = "wav"
DEFAULT_SAMPLE_RATE = 24000

#: 流式模式请求的格式。**pcm 而不是 wav** —— SSE 是一帧一帧下发的裸样本，每帧再带一个
#: wav 头就没有意义了。16 位小端，采样率就是请求里那个。
STREAM_FORMAT = "pcm"

#: 流式模式的开关头。文档：`X-DashScope-SSE: enable`。
SSE_HEADER = "X-DashScope-SSE"

#: 从第二段起，短句攒到这么多字才发一次请求。
#:
#: 两个理由，都不是省钱：
#:
#: 1. **一次请求 0.7–1.5 s（两个 HTTP 往返）**，而一句「好的。」只有半秒的音频。段越短，
#:    合成时间越盖不住播放时间，段间的空白就越明显。
#: 2. **韵律是按请求算的。** 每段单独合成 = 每段各自从头起调、各自收尾，拼起来听得出
#:    接缝；一次合成一整段则由模型自己安排句间停顿。使用者 2026-08-30 报的「感觉不是
#:    很连贯」两个成因都在这里。
#:
#: **第一段永远单独发**，不参与合并：它决定「多久才出第一个字」，而那是最能被感知的
#: 一个延迟。40 字按本机实测这把音色约 4.3 字/秒算是 9 秒左右的音频，足够盖住下一段的
#: 合成；打断仍然是即时的（`sd.stop()` 从中间切断），所以块长不影响响应。
SEGMENT_MERGE_CHARS = 40


class DashScopeTtsError(RuntimeError):
    """一次合成没成。带上端点主机名，但**绝不带 key**。"""


#: HTTP 状态码 → 这台机器上该动哪里。**分类是必需的，不是修饰。**
#:
#: 2026-09-01 的故障里，这一层报的原话是
#: `https://dashscope.aliyuncs.com 回 HTTP 401: {"code":"InvalidApiKey",...}`。
#: 那句话技术上完全正确，而它没有回答唯一要紧的问题：**哪个变量装错了。** 于是「回答
#: 不出声」被当成了合成模型的问题查了好几轮，真正的原因是 `config/voice.toml` 的
#: `key_env` 指向 `VOX_DASHSCOPE_KEY`，而那个变量里装的是中转站的 key。
#:
#: 每一条都带「该动哪里」，因为 401 和 403 在这个服务上要动的地方完全不同：前者是变量装错
#: 了（换变量），后者是这个账号没有这个模型的调用权（换模型或去开通）。
_STATUS_HINTS: Mapping[int, str] = {
    400: "请求形状不对 —— 通常是 model 与 voice 不配对（每个模型只支持一组特定音色），"
    "或者给了一个这个模型不支持的字段（instruction 只有 qwen-audio-3.0-tts-* 支持）",
    401: "密钥不对：${key_env} 里的值不是这个服务认的 key。"
    "一个变量只服务一个角色 —— 中转站的 key 放在 TTS 的变量里，症状就是这一行",
    403: "密钥对，但这个账号不许调这个模型（实测 cosyvoice-v2 回的是 "
    "403 AllocationQuota.FreeTierOnly = 不在免费额度内）。换模型或去控制台开通，不是换 key",
    404: "路径不对 —— 主机在，但这个端点不是合成接口",
    411: "音色名无效 —— 实测 20 个候选里只有配对的那个回 200，其余全部 411",
    429: "被限流或额度用尽。免费额度上的并发限制是真实的（这一层已经把在途请求压到最多一个）",
}


def _classify(code: int, detail: str, key_env: str) -> str:
    """一句能照着做的失败原因。**永不带 key**，只带变量名。"""
    hint = _STATUS_HINTS.get(code)
    if hint is None:
        hint = (
            "服务端出错，重试通常有用（这一层不自动重试：一次合成计费按字符算）"
            if code >= 500
            else "端点拒绝了这次请求"
        )
    return f"HTTP {code} —— {hint.format(key_env=key_env)}"


def merge_segments(segments: Any, *, threshold: int = SEGMENT_MERGE_CHARS) -> list[str]:
    """把一串句子攒成更少、更长的请求。**第一句单独一段。**

    纯函数，不打网络，理由见 ``SEGMENT_MERGE_CHARS``。空白句子被丢掉（它们会变成一次
    什么都不说的请求）。不插分隔符：``split_speech`` 切出来的句子自带句末标点。
    """
    texts = [text for text in (str(item).strip() for item in segments) if text]
    if not texts:
        return []
    limit = max(1, int(threshold))
    merged = [texts[0]]
    buffer = ""
    for text in texts[1:]:
        buffer += text
        if len(buffer) >= limit:
            merged.append(buffer)
            buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


class _Ahead:
    """在别的线程上把下一段合成好。

    存在的理由是**段间的空白**：非实时接口一次合成是两个 HTTP 往返（实测 0.7–1.5 s），
    而这里原来是「合成一段 → 播一段 → 合成下一段」，那 0.7–1.5 s 完整地落在两句话之间。
    播放是阻塞的（``sd.wait()``），那段时间本来就闲着 —— 把下一段的合成挪进去，段间的
    空白就被上一段的播放盖住了。

    **只预取一段，而且要等当前这段的音频到手之后才起。** 于是在途的请求始终最多一个：
    免费额度上的并发限制是真实的，为省半秒换一个 429 不值得。

    线程是 daemon：被打断之后没人会来取这段结果，而非 daemon 线程会让进程退出时干等
    它那 60 秒超时走完。
    """

    __slots__ = ("text", "_audio", "_error", "_done")

    def __init__(self, synthesize: Any, text: str) -> None:
        self.text = text
        self._audio: Any = None
        self._error: BaseException | None = None
        self._done = threading.Event()
        threading.Thread(
            target=self._run, args=(synthesize,), daemon=True, name="vox-tts-ahead"
        ).start()

    def _run(self, synthesize: Any) -> None:
        try:
            self._audio = synthesize(self.text)
        except BaseException as exc:  # noqa: BLE001 - 原样留给等待方重抛
            self._error = exc
        finally:
            self._done.set()

    def result(self, timeout: float) -> Any:
        """等这一段。超时抛而不是返回 ``None`` —— 一段静音会被读成「合成成功但没声音」。"""
        if not self._done.wait(timeout):
            raise DashScopeTtsError(f"合成超时：{timeout:.0f}s 内没有结果")
        if self._error is not None:
            raise self._error
        return self._audio


@dataclass
class DashScopeTtsProvider:
    """百炼非实时合成，摆成和 ``SherpaTtsProvider`` 一样的形状。

    形状一致是红线 2 的要求：`VoicePlugin.attach_tts` 与 `complete_turn` 不该知道声音
    是本机算的还是云端算的。所以这里有 `available` / `load` / `synthesize` / `speak` /
    `speak_segments` / `stop` / `is_stopped` / `close`，签名与本机那个一致。
    """

    model: str = "cosyvoice-v2"
    voice: str = "longyuan"
    key_env: str = DEFAULT_KEY_ENV
    endpoint: str = DEFAULT_ENDPOINT
    sample_rate: int = DEFAULT_SAMPLE_RATE
    speed: float = 1.0
    volume: int = 50
    #: 自由指令，控制情绪与语气。**只有 ``qwen-audio-3.0-tts-*`` 支持**（文档明写「仅
    #: qwen-audio-3.0-tts-plus 和 qwen-audio-3.0-tts-flash」）。这是把一把声音调成「温柔」
    #: 的正确杠杆 —— 音色决定是谁在说，instruction 决定她怎么说。空字符串 = 不发这个字段。
    instruction: str = ""
    timeout_s: float = 60.0
    #: 走 SSE（音频在响应体里按帧下发）还是两个 HTTP 往返（POST 拿链接 + GET 下载）。
    #:
    #: **默认流式，因为它就是更快，而且少一个失败点。** 2026-09-01 实测（同一台机器、
    #: 同一把音色、各跑三次取代表值）：
    #:
    #: | 字数 | 两个往返：音频到手 | SSE：首块到达 | SSE：音频到手 |
    #: |---|---|---|---|
    #: | 3 | 3353 ms | **2405 ms** | **2735 ms** |
    #: | 11 | 4243 ms | **2267 ms** | **3117 ms** |
    #: | 38 | 5786 ms | **2301–2569 ms** | **3700–3961 ms** |
    #:
    #: 两件事同时被这张表钉住：①非流式那条路有约 3.3 秒的固定开销（3 个字也要 3353 ms），
    #: 因为它要等整句合成完再下载一次；②**SSE 的首块到达时间和句子长度基本无关**
    #: （2.3–2.6 s），所以「把第一段切短一点让它早出声」这个策略在流式下不再必要。
    #:
    #: 注入了 ``transport`` 时走非流式那条：测试替掉的是 ``post``/``get`` 两个方法。
    stream: bool = True
    playback: Any = None
    #: 注入点，给测试用。默认是真的 HTTP。
    transport: Any = None
    _stopped: bool = False

    @property
    def available(self) -> bool:
        """有 key 就算可用。**不在这里打网络** —— `available` 被 `describe()` 这类
        只读路径调用，让它发一次请求等于让开一个状态页就花掉配额。"""
        return bool(os.getenv(self.key_env, "").strip()) and bool(self.model and self.voice)

    def load(self) -> ProviderStatus:
        if not os.getenv(self.key_env, "").strip():
            return ProviderStatus(
                False,
                self._safe_endpoint(),
                {"reason": f"{self.key_env} 没有值（云端 TTS 的密钥只从环境变量读）"},
            )
        if not self.model or not self.voice:
            return ProviderStatus(
                False, self._safe_endpoint(), {"reason": "model 或 voice 是空的"}
            )
        return ProviderStatus(
            True,
            self._safe_endpoint(),
            {
                "engine": "dashscope",
                "model": self.model,
                "voice": self.voice,
                "sample_rate": str(self.sample_rate),
                # 报变量名而不是值。日志与事件里出现的必须是名字。
                "key_env": self.key_env,
            },
        )

    def _safe_endpoint(self) -> str:
        """只报 scheme + host，不报路径也不报凭据。"""
        from urllib.parse import urlparse

        parsed = urlparse(self.endpoint)
        return f"{parsed.scheme}://{parsed.hostname}"

    def synthesize(self, text: str, **_ignored: Any) -> TtsAudio:
        """一句话 -> float32 采样。

        两条路，形状一样，都在这里选完：

        - **流式（默认）**：一个 HTTP 请求，音频按帧从 `output.audio.data` 下发。
        - **两个往返**：POST 等整句合成完拿到 `output.audio.url`，再 GET 下载 wav。
          非流式模式下 `data` 是空的，所以那条路必须走两趟 —— 那是接口的形状不是实现的
          选择，代价是约 3.3 秒固定开销（见 ``stream`` 的实测表）。
        """
        import numpy as np

        started = time.monotonic()
        streaming = self.stream and self.transport is None
        payload = {
            "model": self.model,
            "input": {
                "text": str(text),
                "voice": self.voice,
                "format": STREAM_FORMAT if streaming else DEFAULT_FORMAT,
                "sample_rate": int(self.sample_rate),
                "volume": int(self.volume),
                "rate": float(self.speed),
            },
        }
        # 只在有值时带上：不支持 instruction 的模型收到这个字段会 400，而空字符串
        # 和「不发」在服务端不是一回事。
        if self.instruction.strip():
            payload["input"]["instruction"] = self.instruction.strip()
        if streaming:
            raw = self._post_sse(payload)
            if not raw:
                raise DashScopeTtsError("合成失败：流式响应里一帧音频都没有")
            # 16 位小端裸样本。采样率就是请求里那个 —— 裸 PCM 不自带它。
            audio = (np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0)
            return TtsAudio(
                samples=audio,
                sample_rate=int(self.sample_rate),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        import soundfile as sf

        body = self._post(payload)
        url = (((body.get("output") or {}).get("audio") or {}).get("url") or "").strip()
        if not url:
            reason = (body.get("output") or {}).get("finish_reason") or body.get("message") or "回包里没有 output.audio.url"
            raise DashScopeTtsError(f"合成失败：{reason}")
        raw = self._get(url)
        samples, rate = sf.read(io.BytesIO(raw), dtype="float32")
        audio = np.asarray(samples, dtype="float32")
        if audio.ndim > 1:  # 单声道化：下游播放与本机 provider 一致地按一维处理
            audio = audio[:, 0]
        return TtsAudio(
            samples=audio,
            sample_rate=int(rate),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # -- HTTP ---------------------------------------------------------------

    def _post_sse(self, payload: dict[str, Any]) -> bytes:
        """一个请求，边读边收帧。返回拼好的裸 PCM。

        **``stop()`` 在帧之间生效**：打断不必等整句合成完 —— 这条是流式相对两个往返的第二
        个好处（第一个是快）。已经收到的帧不播（那由调用方决定），但连接立刻不再读。
        """
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
                "User-Agent": API_USER_AGENT,
                SSE_HEADER: "enable",
            },
        )
        frames: list[bytes] = []
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                for line in response:
                    if self._stopped:
                        break
                    text = line.decode("utf-8", "replace").rstrip("\r\n")
                    if not text.startswith("data:"):
                        continue
                    body = text[5:].strip()
                    if not body:
                        continue
                    try:
                        event = json.loads(body)
                    except json.JSONDecodeError:
                        # 一帧读坏了不该让整句失败：后面的帧仍然有用。
                        continue
                    data = ((event.get("output") or {}).get("audio") or {}).get("data") or ""
                    if data:
                        try:
                            frames.append(base64.b64decode(data))
                        except (ValueError, TypeError):
                            continue
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} {_classify(int(exc.code), detail, self.key_env)}"
                + (f"\n服务端原话：{detail}" if detail else "")
            ) from exc
        except TimeoutError as exc:
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} 流式合成超时（{self.timeout_s:.0f}s）"
                f" —— 已收到 {len(frames)} 帧"
            ) from exc
        except URLError as exc:
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} 连不上：{exc.reason}"
            ) from exc
        return b"".join(frames)

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
                # urllib 默认的 Python-urllib/3.x 已经被一个中转站 403 过一次
                # （见 core/outbound.py），统一用项目自己的 UA。
                "User-Agent": API_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                return dict(json.loads(response.read().decode("utf-8", "replace")))
        except HTTPError as exc:
            # 状态码要分类报出来，但**不把请求体回显**：它带 key。分类的理由见 `_classify`
            # —— 一句正确而不可操作的 `回 HTTP 401: {...}` 让这次故障多查了好几轮。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - 读不到就算了
                pass
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} {_classify(int(exc.code), detail, self.key_env)}"
                + (f"\n服务端原话：{detail}" if detail else "")
            ) from exc
        except TimeoutError as exc:
            # 超时和「被拒绝」要分开：前者动 timeout_s 或网络，后者动配置。合成一句长文本
            # 本身就要几秒（非实时接口是整句合成完才返回），所以超时不等于坏了。
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} 超时（{self.timeout_s:.0f}s 内没有响应）"
                " —— 非实时接口整句合成完才返回，长句子上调大 timeout_s"
            ) from exc
        except URLError as exc:
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} 连不上：{exc.reason} —— 这一层还没发出请求，"
                "检查网络或代理，不是密钥问题"
            ) from exc
        except json.JSONDecodeError as exc:
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} 回的不是 JSON：{exc}"
            ) from exc

    def _get(self, url: str) -> bytes:
        if self.transport is not None:
            return bytes(self.transport.get(url))
        # 下载链接自带签名，**不能**再附 Authorization —— 带上会被 OSS 当成两套凭据拒掉。
        request = Request(url, method="GET", headers={"User-Agent": API_USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                return bytes(response.read())
        except (HTTPError, URLError, TimeoutError) as exc:
            raise DashScopeTtsError(f"音频下载失败: {exc}") from exc

    # -- 播放（与本机 provider 同签名）---------------------------------------

    def _player(self) -> Any:
        if self.playback is not None:
            return self.playback
        from core.audio.playback import SounddevicePlayback

        self.playback = SounddevicePlayback()
        return self.playback

    def speak(self, text: str, **_ignored: Any) -> dict[str, Any]:
        self._stopped = False
        audio = self.synthesize(text)
        if self._stopped:
            return {"played": False, "reason": "stopped"}
        self._player().play(audio.samples, audio.sample_rate)
        return {
            "played": True,
            "elapsed_ms": audio.elapsed_ms,
            "sample_rate": audio.sample_rate,
            "samples": int(len(audio.samples)),
        }

    def speak_segments(self, segments: Any, **_ignored: Any) -> dict[str, Any]:
        """逐段说，但**下一段在上一段播放期间就合成好**（见 ``_Ahead``）。

        ``stop()`` 在段之间生效 —— 打断不该等整段说完。已经预取出来的那一段在打断之后
        **不播**：它花掉的额度收不回来，但让它出声等于「打断之后又说了一句」。
        """
        self._stopped = False
        planned = merge_segments(segments)
        # 一次合成是两个往返，各自受 timeout_s 约束；再留一点解码与排队的余量。
        deadline = self.timeout_s * 2 + 5.0
        spoken = 0
        ahead: _Ahead | None = None
        for index, text in enumerate(planned):
            if self._stopped:
                break
            current = ahead if ahead is not None else _Ahead(self.synthesize, text)
            ahead = None
            audio = current.result(deadline)
            if self._stopped:
                break
            # 先把下一段排上，**再**开始播这一段：两件事重叠正是 `_Ahead` 存在的目的。
            if index + 1 < len(planned):
                ahead = _Ahead(self.synthesize, planned[index + 1])
            self._player().play(audio.samples, audio.sample_rate)
            spoken += 1
        return {"played": spoken > 0, "segments": spoken, "stopped": self._stopped}

    def stop(self) -> None:
        self._stopped = True
        if self.playback is not None:
            try:
                self.playback.stop()
            except Exception:  # noqa: BLE001 - 停不掉也不该抛给调用方
                pass

    def is_stopped(self) -> bool:
        return self._stopped

    def close(self) -> None:
        self.stop()
        self.playback = None


__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_KEY_ENV",
    "SEGMENT_MERGE_CHARS",
    "DashScopeTtsError",
    "DashScopeTtsProvider",
    "merge_segments",
]
