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

import io
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
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

        **两个 HTTP 往返**：第一次拿到 `output.audio.url`，第二次把 wav 下载回来。
        这是接口的形状不是实现的选择 —— 非流式模式下 `output.audio.data` 是空的。
        """
        import numpy as np
        import soundfile as sf

        started = time.monotonic()
        payload = {
            "model": self.model,
            "input": {
                "text": str(text),
                "voice": self.voice,
                "format": DEFAULT_FORMAT,
                "sample_rate": int(self.sample_rate),
                "volume": int(self.volume),
                "rate": float(self.speed),
            },
        }
        # 只在有值时带上：不支持 instruction 的模型收到这个字段会 400，而空字符串
        # 和「不发」在服务端不是一回事。
        if self.instruction.strip():
            payload["input"]["instruction"] = self.instruction.strip()
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
            # 状态码要报出来（401 = key 不对，429 = 配额用尽，400 = 音色/模型不匹配），
            # 但**不把请求体回显**：它带 key。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - 读不到就算了
                pass
            raise DashScopeTtsError(
                f"{self._safe_endpoint()} 回 HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DashScopeTtsError(f"{self._safe_endpoint()} 请求失败: {exc}") from exc

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
