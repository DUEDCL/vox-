"""消息平台的契约。**平台的类型不许越过这条线。**

和 `core/agents/contract.py` 同一个立场，同一个理由：一个把 iLink 的 `context_token`
或者 `item_list` 形状泄进公开结构的契约，会让「换一个消息平台」变成改一整条链路，而不是
加一个文件。所以这里的字段只有 `str` / `float` / `bytes` / `tuple`。

## 一条消息的三种形状

微信那一侧一条语音消息同时带**两样东西**：腾讯云自己的 STT 文本，和原始音频。它们的可信度
不一样（腾讯那份对非中文是错的，见 Hermes 的 issue #27300），所以契约里它们是两个字段而
不是一个 —— 让上层决定信谁，而不是在适配器里替它决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

#: 语音消息的 kind。用一个集合而不是一个布尔：将来会有视频、图片，而
#: `kind == "voice"` 比 `is_voice and not is_video` 读得懂。
VOICE_KINDS: frozenset[str] = frozenset({"voice"})


class ChannelError(RuntimeError):
    """这条通道不能被信任去做它声称的事。"""


@dataclass(frozen=True)
class IncomingMessage:
    """平台送进来的一条消息。

    ``text`` 与 ``provider_text`` 分开是刻意的：前者是**我们自己**转写出来的（或者对方
    打的字），后者是平台附带的 STT 结果。腾讯云那份对非中文是错的，所以哪一个可信要由
    上层按语言与场景决定 —— 适配器只负责把两样都带上来。
    """

    #: 平台内的会话标识。回复必须发回这里。
    chat_id: str
    #: 消息本体的文本。语音消息在这一层可能是空的（还没转写）。
    text: str = ""
    #: ``text`` / ``voice`` / ``image`` / ``file`` / ``other``。
    kind: str = "text"
    #: 平台自己的转写结果，仅语音消息有。**不与 ``text`` 合并。**
    provider_text: str = ""
    #: 原始音频/文件的字节。语音消息在能下载到原件时带它。
    media: bytes = b""
    #: 原件的格式后缀（``wav`` / ``mp3`` / ``silk`` …），小写，不带点。
    media_format: str = ""
    #: 平台的消息 id，用于去重与日志。
    message_id: str = ""
    #: 发件人在平台内的标识。**不是**已验证说话人 —— 消息平台没有声纹。
    sender: str = ""
    #: 适配器需要在回复时回带的东西（iLink 的 `context_token` 就在这里）。
    #: 形状由适配器自己定，**上层不许读它** —— 它是那一个平台的私事。
    reply_context: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_voice(self) -> bool:
        return self.kind in VOICE_KINDS

    @property
    def best_text(self) -> str:
        """能拿去派发的那一句。自己转写的优先，平台的兜底。"""
        return self.text.strip() or self.provider_text.strip()


@dataclass(frozen=True)
class OutgoingMessage:
    """要发出去的一条回复。

    ``audio`` 只可能是 **TTS 合成的产物**。麦克风录到的东西永不出现在这里 —— 这个包
    不 import `core/audio` 的采集侧，所以那条路在结构上就不通（见 `__init__.py`）。
    """

    chat_id: str
    text: str = ""
    #: 合成好的音频字节。给了它就同时发一条音频。
    audio: bytes = b""
    #: 音频格式后缀，小写不带点。
    audio_format: str = "wav"
    #: 从哪条消息来的（拿 `reply_context` 回带平台要的东西）。
    reply_context: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class MessageChannel(Protocol):
    """一个消息平台。**四个动作，全部同步** —— 异步留给调用方自己起线程。

    同步是刻意的：Vox 的运行时（`VoiceRuntime.pump`）本来就是「在调用方线程上跑一整轮」
    的形状，而一条微信消息和一句话在派发那一层是同一个东西。给这一层套 asyncio 会让
    两个事件循环出现在同一个进程里，而那是 Hermes 用 aiohttp 换来的代价（我们连
    aiohttp 都没有装）。
    """

    name: str

    def check(self) -> Mapping[str, Any]:
        """能不能用，不能的话为什么。**不打网络。**"""
        ...

    def poll(self, timeout_s: float) -> tuple[IncomingMessage, ...]:
        """取一批新消息。超时返回空元组，不抛。"""
        ...

    def send(self, message: OutgoingMessage) -> Mapping[str, Any]:
        """发一条出去。返回给日志看的东西（不含正文）。"""
        ...

    def close(self) -> None:
        ...


__all__ = [
    "ChannelError",
    "IncomingMessage",
    "MessageChannel",
    "OutgoingMessage",
    "VOICE_KINDS",
]
