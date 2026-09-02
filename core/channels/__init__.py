"""消息平台接入：一条契约，一个实现（微信 iLink）。

## 为什么在 `contract.py` 之后才有实现

红线 2：任何平台的类型都不许出现在公开结构里。`IncomingMessage` / `OutgoingMessage`
的字段只有 `str` / `float` / `bytes` / `tuple` —— 换一个平台（飞书、QQ）时改的是一个
新文件，不是一整条链路。

## 出网这件事说清楚

这一层**是出网的**：它长轮询腾讯的 iLink 端点、从微信 CDN 下载媒体、往那里上传。红线 1
在 ADR 008 之后是「音频本地，算力上云」，而「把消息发到微信」是使用者的显式选择。三条
边界写在代码里而不是文档里：

1. **默认关**（`config/channels.toml` 的 `enabled = false`），不配就一个字节都不出网；
2. **凭据只从环境变量读**，配置文件里只有变量名 —— 和 agents / tts 同一条规矩；
3. **Vox 自己麦克风录下的音频永不出网**。出站语音只可能是 TTS 合成的产物，那是我们
   自己造的声音；声纹环形缓冲与识别输入在这一层根本拿不到（它们在 `core/audio` 里，
   这个包不 import 它们）。
"""

from __future__ import annotations

from .contract import (
    ChannelError,
    IncomingMessage,
    MessageChannel,
    OutgoingMessage,
    VOICE_KINDS,
)

__all__ = [
    "ChannelError",
    "IncomingMessage",
    "MessageChannel",
    "OutgoingMessage",
    "VOICE_KINDS",
]
