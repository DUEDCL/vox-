"""一条微信消息 -> 一轮对话 -> 一条回复。**语音进，语音出。**

## 它和麦克风那条路是同一个派发器

`VoiceRuntime.say()` 不在乎这句话是从麦克风来的还是从微信来的 —— 意图判定、工具快路径、
agent 路由、记忆写入全都复用。所以这一层薄得几乎只剩三个决定：

1. **入站语音信谁的转写。** 能下载到原件、而且格式解得开，就用**本机 ASR**；否则用腾讯
   自带的 STT 文本并标注来源。顺序是这样而不是反过来，因为腾讯那份对非中文是错的
   （Hermes issue #27300），而本机那个是我们自己调过的。
2. **出站要不要带语音。** 默认带（使用者点名要「在微信也能进行语音消息的处理和发送」），
   同时**永远带文字** —— 一条只有语音的回复在电脑上看不了，也搜不到。
3. **说话人是谁。** 微信没有声纹，所以这条路上的 `speaker` 永远是 ``None`` ——
   `shell.run` 因此进不来（它要 `require_verified_speaker`）。这不是限制，是这条链路
   唯一正确的答案：一个微信 id 证明不了对面是谁。

## 出网边界

这一层只发**它自己合成的音频**。Vox 麦克风录到的东西在这里拿不到 —— 这个模块不 import
`core/audio/capture`，声纹环形缓冲与识别输入都在它够不到的地方。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core.channels.audio import to_16k_mono, to_wav_bytes
from core.channels.contract import ChannelError, IncomingMessage, OutgoingMessage

#: 腾讯的 STT 被用上时加的前缀。**必须留痕** —— 一句转错的话如果不说来源，
#: 使用者会以为是我们的识别器听错了，然后去调一个没问题的模型。
PROVIDER_STT_NOTE = "（微信自带转写）"

#: 一轮的最长等待。派发本身有自己的超时，这一层的上限是为了让循环不被一个卡住的回合占死。
TURN_TIMEOUT_S = 180.0

#: 会话记录的条数上限。**环形缓冲、不落盘** —— 和运行日志同一条规矩：一个跑了一天的通道
#: 不该在磁盘上留下每一句微信消息。要长期留的那份由记忆层决定，不该被一个调试视图绕过去。
TRANSCRIPT_MAX = 400


@dataclass
class ChannelRunner:
    """把一个通道接到运行时上。``run_forever`` 之外每个动作都能单独测。"""

    channel: Any
    runtime: Any
    #: 出站带不带语音。默认带。
    reply_with_voice: bool = True
    #: ASR provider（`core/audio/asr.py` 的那个形状：create_stream / feed / finalize）。
    #: ``None`` = 不自己转写，一律用平台的 STT。
    asr: Any = None
    #: TTS provider（`synthesize(text) -> TtsAudio`）。``None`` = 只回文字。
    tts: Any = None
    #: 处理过几条、失败几条。给日志和控制台看。
    handled: int = 0
    failures: int = 0
    last_error: str = ""
    #: 会话记录的环形缓冲 —— 控制台那一栏的「实时收发」读的就是它。
    #:
    #: **不落盘、有上限**，和运行日志同一条规矩：一个跑了一天的通道不该在磁盘上留下每一句
    #: 微信消息。要长期留的那份由记忆层决定（使用者自己选），不该被一个调试视图绕过去。
    transcript: list[dict] = field(default_factory=list)
    #: 被环形缓冲挤掉的条数。序号要连续跨过它们，否则页面会把新条目当成旧的。
    _dropped: int = 0
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    # ------------------------------------------------------------------ 会话记录

    def _record(self, direction: str, **fields: Any) -> dict:
        """往会话记录里写一条。返回写进去的那条，好让调用方拿到它的序号。

        序号（``seq``）是页面拉增量的游标。用序号而不是时间戳：同一毫秒里可能有两条，
        而一个会漏条目的游标比没有游标更糟 —— 它会让「刚才那条去哪了」变成一个偶发问题。
        """
        entry = {
            "seq": len(self.transcript) + self._dropped + 1,
            "at": time.strftime("%H:%M:%S"),
            "direction": direction,
            **fields,
        }
        self.transcript.append(entry)
        if len(self.transcript) > TRANSCRIPT_MAX:
            # 丢最旧的，并把丢掉的条数记住 —— 不记的话序号会重复，页面会把新条目当旧的。
            overflow = len(self.transcript) - TRANSCRIPT_MAX
            del self.transcript[:overflow]
            self._dropped += overflow
        return entry

    def read_transcript(self, since: int = 0, limit: int = 200) -> dict[str, Any]:
        """从游标之后读会话记录。页面每隔一两秒问一次。"""
        rows = [row for row in self.transcript if int(row.get("seq", 0)) > int(since or 0)]
        capped = rows[: max(1, int(limit or 200))]
        return {
            "entries": capped,
            "next": int(capped[-1]["seq"]) if capped else int(since or 0),
            "dropped": self._dropped,
        }

    def chats(self) -> list[dict[str, Any]]:
        """出现过的会话，最近的在前。控制台左边那一列。"""
        latest: dict[str, dict[str, Any]] = {}
        for row in self.transcript:
            chat = str(row.get("chat_id") or "")
            if not chat:
                continue
            entry = latest.setdefault(chat, {"chat_id": chat, "messages": 0})
            entry["messages"] += 1
            entry["at"] = row.get("at", "")
            entry["last"] = str(row.get("text") or "")[:60]
            entry["sender"] = row.get("sender", "")
        return sorted(latest.values(), key=lambda row: row.get("at", ""), reverse=True)

    def send_text(self, chat_id: str, text: str) -> Mapping[str, Any] | None:
        """控制台手打一条发出去。**不走 agent** —— 那一栏是「我自己说」，不是「让它答」。

        ``reply_context`` 从这个 peer 最近一条入站消息里取：微信要求每条出站回复回带该 peer
        最新的 ``context_token``，而那是通道自己维护的。这里传空 mapping，让通道去查它的表 ——
        由页面来提供一个协议内部的令牌是让上层知道了它不该知道的事。
        """
        body = str(text or "").strip()
        if not body:
            raise ChannelError("要发的内容是空的")
        sent = self._safe_send(OutgoingMessage(chat_id=str(chat_id), text=body))
        self._record("out", chat_id=str(chat_id), text=body, by="console", sent=bool(sent))
        return sent

    # ------------------------------------------------------------------ 转写

    def transcribe(self, message: IncomingMessage) -> tuple[str, str]:
        """一条语音消息 -> (文本, 来源)。来源是 ``local`` / ``provider`` / ``none``。"""
        if not message.is_voice:
            return message.text, "text"
        samples = to_16k_mono(message.media, message.media_format) if message.media else None
        if samples is not None and self.asr is not None and samples.size:
            text = self._run_asr(samples)
            if text.strip():
                return text.strip(), "local"
        provider = message.provider_text.strip()
        if provider:
            return f"{provider}{PROVIDER_STT_NOTE}", "provider"
        return "", "none"

    def _run_asr(self, samples: Any) -> str:
        """喂给流式识别器，按 100 ms 一块 —— 和麦克风回调同一个块长。

        用同一个块长不是为了省事：那是这个模型在生产里唯一见过的形状，换一个块长会让
        端点检测的行为和真机不一致。
        """
        try:
            stream = self.asr.create_stream()
            step = 1600
            for offset in range(0, int(samples.size), step):
                self.asr.feed(stream, samples[offset : offset + step], 16000)
            return str(self.asr.finalize(stream) or "")
        except Exception as exc:  # noqa: BLE001 - 转不出来就用平台那份
            self.last_error = f"本机转写失败：{type(exc).__name__}: {exc}"
            return ""

    # ------------------------------------------------------------------ 一条消息

    def handle(self, message: IncomingMessage) -> Mapping[str, Any] | None:
        """一条进来的消息走完一整轮。返回给日志看的东西，``None`` = 这条被跳过。"""
        text, source = self.transcribe(message)
        if not text.strip():
            self._log(
                "weixin",
                f"收到一条{'语音' if message.is_voice else ''}消息但没有可用文本"
                f"（媒体 {len(message.media)} 字节，格式 {message.media_format or '未知'}）"
                + ("；SILK 我们解不了，而它也没带自带转写" if message.media_format == "silk" else ""),
                level="warn",
            )
            return None
        self._log(
            "weixin",
            f"收到：{text[:120]}",
            source=source,
            kind=message.kind,
            sender_tail=message.sender[-4:],
        )
        self._record(
            "in",
            chat_id=message.chat_id,
            sender=message.sender,
            text=text,
            kind=message.kind,
            source=source,
        )
        # **speaker 永远不传。** 微信 id 证明不了对面是谁，而 `shell.run` 要已验证说话人。
        try:
            result = self.runtime.say(text, speak=False)
        except Exception as exc:  # noqa: BLE001 - 一条坏消息不能结束这条通道
            self.failures += 1
            self.last_error = f"这一轮抛了：{type(exc).__name__}: {exc}"
            self._log("weixin", self.last_error, level="error")
            self._safe_send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    text="这一轮出错了，稍后再试。",
                    reply_context=message.reply_context,
                )
            )
            return None
        reply = (getattr(result, "text", "") or "").strip() or "（没有内容）"
        audio, audio_format = self._synthesize(reply)
        sent = self._safe_send(
            OutgoingMessage(
                chat_id=message.chat_id,
                text=reply,
                audio=audio,
                audio_format=audio_format,
                reply_context=message.reply_context,
            )
        )
        self.handled += 1
        self._record(
            "out",
            chat_id=message.chat_id,
            text=reply,
            by="agent",
            route=getattr(result, "route", ""),
            voice_out=bool(audio),
            sent=bool(sent),
        )
        return {
            "route": getattr(result, "route", ""),
            "ok": bool(getattr(result, "ok", False)),
            "source": source,
            "voice_out": bool(audio),
            "sent": sent,
        }

    def _synthesize(self, reply: str) -> tuple[bytes, str]:
        """回复的语音。合不出来就只发文字 —— **不因为语音失败而不回话。**"""
        if not self.reply_with_voice or self.tts is None or not reply.strip():
            return b"", "wav"
        try:
            audio = self.tts.synthesize(reply)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"合成失败，这条只发文字：{type(exc).__name__}: {exc}"
            self._log("weixin", self.last_error, level="warn")
            return b"", "wav"
        samples = getattr(audio, "samples", None)
        rate = int(getattr(audio, "sample_rate", 0) or 0)
        if samples is None or not rate:
            return b"", "wav"
        try:
            return to_wav_bytes(samples, rate), "wav"
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"打包 WAV 失败：{type(exc).__name__}: {exc}"
            return b"", "wav"

    def _safe_send(self, message: OutgoingMessage) -> Mapping[str, Any] | None:
        try:
            return self.channel.send(message)
        except ChannelError as exc:
            self.failures += 1
            self.last_error = f"发不出去：{exc}"
            self._log("weixin", self.last_error, level="error")
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.last_error = f"发不出去：{type(exc).__name__}: {exc}"
            self._log("weixin", self.last_error, level="error")
        return None

    def _log(self, where: str, message: str, **fields: Any) -> None:
        """写运行日志。第一个参数叫 ``where`` 而不是 ``source`` —— 转写来源那个字段
        正好也叫 `source`，两个同名会让 `_log("weixin", …, source="local")` 变成
        「同一个参数给了两个值」的 TypeError。"""
        logger = getattr(self.runtime, "log", None)
        if callable(logger):
            try:
                logger(where, message, **fields)
            except Exception:  # noqa: BLE001 - 日志通道不是前提
                pass

    # ------------------------------------------------------------------ 循环

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, *, poll_timeout_s: float = 35.0, idle_sleep_s: float = 0.5) -> None:
        """长轮询 -> 处理 -> 再轮询。**在调用方线程上跑**，起线程是调用方的事。

        `poll` 不抛（见 `WeixinChannel.poll`），所以这个循环没有 try —— 它靠 `last_error`
        知道上一轮发生了什么。一个吞掉异常又不留痕的循环等于把最需要的线索删掉，
        而这里的痕迹在通道对象上。
        """
        while not self._stop.is_set():
            messages = self.channel.poll(poll_timeout_s)
            if not messages:
                error = getattr(self.channel, "last_error", "")
                if error and error != self.last_error:
                    self.last_error = error
                    self._log("weixin", error, level="warn")
                self._stop.wait(idle_sleep_s)
                continue
            for message in messages:
                if self._stop.is_set():
                    break
                self.handle(message)

    def describe(self) -> dict[str, Any]:
        return {
            "channel": getattr(self.channel, "name", "?"),
            "handled": self.handled,
            "failures": self.failures,
            "voice_out": self.reply_with_voice and self.tts is not None,
            "local_asr": self.asr is not None,
            "last_error": self.last_error,
            "running": not self._stop.is_set(),
            "messages": len(self.transcript) + self._dropped,
        }


__all__ = ["PROVIDER_STT_NOTE", "TRANSCRIPT_MAX", "TURN_TIMEOUT_S", "ChannelRunner"]
