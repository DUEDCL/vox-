"""微信 iLink Bot 通道。同步、只用标准库、可注入传输层。

## 协议事实（读的是 Hermes 的 `gateway/platforms/weixin.py`，社区实现，非腾讯官方文档）

| | |
|---|---|
| 端点根 | `https://ilinkai.weixin.qq.com` |
| 媒体 CDN | `https://novac2c.cdn.weixin.qq.com/c2c` |
| 入站 | `ilink/bot/getupdates` **长轮询**，请求带 `get_updates_buf`，响应回新的 buf |
| 出站 | `ilink/bot/sendmessage`，`item_list` 里一条 `text_item` |
| 媒体 | `ilink/bot/getuploadurl` → AES-128-ECB 加密 → POST 到 CDN → item 引用 |
| 鉴权 | `Authorization: Bearer <token>` + `AuthorizationType: ilink_bot_token` |

**每条出站回复必须回带该 peer 最新的 `context_token`**，漏了就发不出去。所以
`IncomingMessage.reply_context` 带着它，而上层只需要把那个 mapping 原样传回来。

## 语音

入站语音（`ITEM_VOICE = 3`）同时带两样东西：腾讯云自己的 STT 文本 `voice_item.text`，
和原始音频 `voice_item.media`。**两样都带上来，不在这一层替上层选**（`provider_text`
与 `media` 是两个字段）—— 腾讯那份对非中文是错的（Hermes issue #27300），而我们有本机
ASR，所以正确的顺序是「能下载到原件就自己转写，拿不到才用它的文本」。那个决定在
`runner.py`，不在这里。

出站语音：**原生语音气泡在上游没跑通** —— Hermes 自己的 `send_voice` 注释写着
「Native outbound Weixin voice bubbles are not proven-working」，它退化成文件附件。
这里两条都留着：`voice_native = True` 走 `MEDIA_VOICE`，默认走文件附件（对方能播）。
哪条真的能通只有你在微信里看得到，所以它是 REAL-WEIXIN，代码里不假装它已验证。

## 验证等级

传输层可注入（`transport=`），所以整条链路在**离线**下可断言 —— 那是 SIM。真机发一条
微信消息是 REAL-WEIXIN，本文件里没有任何一处声称它已经通过。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core.channels.contract import ChannelError, IncomingMessage, OutgoingMessage
from core.channels.crypto import aes128_ecb_encrypt, padded_size
from core.outbound import API_USER_AGENT

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
#: `(major << 16) | (minor << 8) | patch`，跟着 CHANNEL_VERSION 走。
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"

#: 长轮询的挂起时长。35 秒是上游用的值 —— 比它短会让空闲时的请求量上去，
#: 比它长会撞上中间设备的空闲超时。
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000

ITEM_TEXT = 1
ITEM_VOICE = 3
ITEM_FILE = 4

MEDIA_FILE = 3
MEDIA_VOICE = 4

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

#: 会话过期。iLink 用两个码表达同一件事：-14，以及 errmsg 恰好是 "unknown error" 的 -2
#: （-2 本来是频率限制）。分不开这两个的后果是「被限流」和「要重新扫码」用同一句话报出来。
SESSION_EXPIRED = -14
RATE_LIMITED = -2


def _is_stale_session(ret: Any, errcode: Any, errmsg: Any) -> bool:
    if ret != RATE_LIMITED and errcode != RATE_LIMITED:
        return ret == SESSION_EXPIRED or errcode == SESSION_EXPIRED
    return str(errmsg or "").strip().casefold() == "unknown error"


def _random_wechat_uin() -> str:
    """`X-WECHAT-UIN` 头。上游是随机的 —— 它不是身份，是个分流用的数。"""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _parse_aes_key(raw: str) -> bytes:
    """媒体的 `aes_key`。上游收 base64，也收 32 位十六进制。"""
    text = str(raw or "").strip()
    if len(text) == 32:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    decoded = base64.b64decode(text)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        return bytes.fromhex(decoded.decode("ascii"))
    raise ChannelError(f"看不懂的 aes_key（解出 {len(decoded)} 字节）")


@dataclass
class HttpTransport:
    """标准库的 HTTP。**可被替换掉，测试就是这么做的。**

    不用 aiohttp（这台机器上没有，而且引它会把一个 asyncio 事件循环带进一个同步进程），
    也不伪装 UA —— `core/outbound.py` 那条规矩：声明真名。
    """

    def post_json(
        self, url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout_s: float
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        for name, value in {**headers, "Content-Length": str(len(body))}.items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChannelError(f"iLink 回的不是 JSON：{raw[:200]}") from exc

    def post_bytes(
        self, url: str, data: bytes, headers: Mapping[str, str], timeout_s: float
    ) -> bytes:
        request = urllib.request.Request(url, data=data, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read()

    def get_bytes(self, url: str, timeout_s: float) -> bytes:
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", API_USER_AGENT)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read()

    def get_json(
        self, url: str, headers: Mapping[str, str], timeout_s: float
    ) -> dict[str, Any]:
        """带头的 GET。扫码登录那两个端点用它（取二维码、查状态）。

        和 ``get_bytes`` 分开是因为那个是拉媒体（不带 iLink 的头，也不该带），
        这个是打 API。合成一个会让「下载一段音频」意外带上 App-Id。
        """
        request = urllib.request.Request(url, method="GET")
        for name, value in {"User-Agent": API_USER_AGENT, **dict(headers)}.items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChannelError(f"iLink 回的不是 JSON：{raw[:200]}") from exc


@dataclass
class WeixinChannel:
    """一个微信 iLink bot 账号。

    ``token`` 只从环境变量读 —— 配置文件里只有变量名，和 agents / tts 同一条规矩。
    """

    name: str = "weixin"
    token_env: str = "VOX_WEIXIN_TOKEN"
    base_url: str = ILINK_BASE_URL
    cdn_base_url: str = WEIXIN_CDN_BASE_URL
    #: 出站语音走原生语音气泡还是文件附件。**默认文件附件**：原生那条在上游没跑通，
    #: 而一条发不出去的语音比一条能播的附件差。
    voice_native: bool = False
    transport: Any = field(default_factory=HttpTransport)
    #: 长轮询的游标。iLink 每次回一个新的，下次带上。
    sync_buf: str = ""
    #: 见过的消息 id，去重用。长轮询会重投。
    seen: set[str] = field(default_factory=set)
    #: peer -> 最新的 context_token。回复必须回带它。
    context_tokens: dict[str, str] = field(default_factory=dict)
    #: 最近一次失败的原因，给 `check()` 和日志看。
    last_error: str = ""

    def _token(self) -> str:
        """这个账号的 bot token。**环境变量优先，然后是扫码存下来的那份。**

        顺序是刻意的：环境变量是「我自己已经有一个 token」和自动化那条路，扫码是普通
        使用者那条路。反过来的话，一个想临时换账号的人会发现改了环境变量没有用。

        两条都没有时报错要说清**该往哪走** —— 上一版只说「没有 $VOX_WEIXIN_TOKEN」，
        而使用者的反应是对的：那个 token 本来就是扫码换来的，没有扫码这一步他无处可取。
        """
        import os

        value = os.environ.get(self.token_env, "").strip()
        if value:
            return value
        from core.channels.weixin_login import load_credentials

        saved = load_credentials()
        if saved and saved.get("token"):
            # base_url 也跟着凭据走：`scaned_but_redirect` 会把账号分到另一个域名上，
            # 而那个域名是登录时才知道的。忽略它的症状是长轮询一直空转。
            stored_base = saved.get("base_url", "").strip()
            if stored_base and self.base_url == ILINK_BASE_URL:
                self.base_url = stored_base
            return saved["token"]
        raise ChannelError(
            "微信还没绑定 —— 到控制台「微信」那一栏点「扫码登录」，用手机微信扫一下。"
            f"（自动化可以改走环境变量 ${self.token_env}）"
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            "User-Agent": API_USER_AGENT,
        }

    def _post(self, endpoint: str, payload: Mapping[str, Any], timeout_ms: int) -> dict[str, Any]:
        token = self._token()
        url = f"{self.base_url.rstrip('/')}/{endpoint}"
        body = {**payload, "base_info": {"channel_version": CHANNEL_VERSION}}
        try:
            return self.transport.post_json(url, body, self._headers(token), timeout_ms / 1000)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
            raise ChannelError(f"iLink {endpoint} HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise ChannelError(f"iLink {endpoint} 连不上：{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ 契约

    def check(self) -> dict[str, Any]:
        """能不能用。**不打网络** —— 和 agent 适配器的 `check()` 同一条规矩。"""
        import os

        has_token = bool(os.environ.get(self.token_env, "").strip())
        return {
            "name": self.name,
            "available": has_token,
            "reason": "" if has_token else f"没有 ${self.token_env}",
            "base": self.base_url,
            "voice_native": self.voice_native,
            "last_error": self.last_error,
        }

    def poll(self, timeout_s: float = LONG_POLL_TIMEOUT_MS / 1000) -> tuple[IncomingMessage, ...]:
        """长轮询取一批。**超时与网络错误都返回空元组，不抛。**

        不抛是刻意的：这个方法在一个 while 循环里被反复调用，而「这一轮没消息」和「这一轮
        网线松了」都不该结束那个循环。原因记在 ``last_error`` 里，由调用方决定要不要报。
        """
        try:
            response = self._post(
                EP_GET_UPDATES,
                {"get_updates_buf": self.sync_buf},
                int(timeout_s * 1000),
            )
        except ChannelError as exc:
            self.last_error = str(exc)
            return ()
        ret, errcode = response.get("ret"), response.get("errcode")
        if _is_stale_session(ret, errcode, response.get("errmsg")):
            self.last_error = "会话过期，要重新扫码登录（iLink ret/errcode 说的）"
            return ()
        buf = str(response.get("get_updates_buf") or "")
        if buf:
            self.sync_buf = buf
        self.last_error = ""
        out: list[IncomingMessage] = []
        for raw in response.get("msgs") or []:
            message = self._parse(raw)
            if message is not None:
                out.append(message)
        return tuple(out)

    def _parse(self, raw: Mapping[str, Any]) -> IncomingMessage | None:
        """一条 iLink 消息 -> 契约里的形状。看不懂的返回 ``None``（跳过，不抛）。"""
        if not isinstance(raw, Mapping):
            return None
        message_id = str(raw.get("msg_id") or raw.get("msgid") or "")
        if message_id and message_id in self.seen:
            return None  # 长轮询会重投同一条
        sender = str(raw.get("from_user_id") or "")
        room = str(raw.get("room_id") or "")
        chat_id = room or sender
        if not chat_id:
            return None
        token = str(raw.get("context_token") or "")
        if token:
            # **每条出站必须回带最新的那个。** 存一份是因为回复未必紧跟着这条消息
            # （派发要几秒，中间可能又来一条）。
            self.context_tokens[chat_id] = token
        text_parts: list[str] = []
        provider_text = ""
        media = b""
        media_format = ""
        kind = "text"
        for item in raw.get("item_list") or []:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == ITEM_TEXT:
                text_parts.append(str((item.get("text_item") or {}).get("text") or ""))
            elif item_type == ITEM_VOICE:
                kind = "voice"
                voice = item.get("voice_item") or {}
                provider_text = str(voice.get("text") or "")
                reference = voice.get("media") or {}
                if reference:
                    media, media_format = self._fetch_media(reference)
        if message_id:
            self.seen.add(message_id)
            if len(self.seen) > 4096:
                self.seen.clear()  # 环形而不是无界：去重只需要覆盖长轮询的重投窗口
        return IncomingMessage(
            chat_id=chat_id,
            text="\n".join(part for part in text_parts if part).strip(),
            kind=kind,
            provider_text=provider_text,
            media=media,
            media_format=media_format,
            message_id=message_id,
            sender=sender,
            reply_context={"context_token": self.context_tokens.get(chat_id, token)},
        )

    def _fetch_media(self, reference: Mapping[str, Any]) -> tuple[bytes, str]:
        """下载并解密一份媒体。**失败返回空**，不抛。

        拿不到原件不是失败：语音消息还带着腾讯自己的 STT 文本，上层可以用那一份。
        为此抛异常会把一条能处理的消息变成一次错误。
        """
        encrypted = str(reference.get("encrypt_query_param") or "")
        full_url = str(reference.get("full_url") or reference.get("url") or "")
        if encrypted:
            url = (
                f"{self.cdn_base_url.rstrip('/')}/download"
                f"?encrypt_query_param={urllib.parse.quote(encrypted, safe='')}"
            )
        elif full_url:
            # 只从微信自己的 CDN 拿。一个能指向任意主机的字段就是一次 SSRF。
            host = urllib.parse.urlsplit(full_url).hostname or ""
            if not host.endswith(".weixin.qq.com"):
                self.last_error = f"媒体地址不在微信 CDN 上，拒绝下载：{host}"
                return b"", ""
            url = full_url
        else:
            return b"", ""
        try:
            raw = self.transport.get_bytes(url, API_TIMEOUT_MS / 1000)
        except Exception as exc:  # noqa: BLE001 - 拿不到原件就用平台的 STT
            self.last_error = f"媒体下载失败：{type(exc).__name__}: {exc}"
            return b"", ""
        key = str(reference.get("aes_key") or reference.get("aeskey") or "")
        if key:
            from core.channels.crypto import aes128_ecb_decrypt

            try:
                raw = aes128_ecb_decrypt(raw, _parse_aes_key(key))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"媒体解密失败：{type(exc).__name__}: {exc}"
                return b"", ""
        size = reference.get("rawsize")
        if isinstance(size, int) and 0 < size <= len(raw):
            # 按 rawsize 裁：CDN 上那份是补齐过的，多出来的是填充字节。
            raw = raw[:size]
        suffix = str(reference.get("file_name") or "").rsplit(".", 1)
        fmt = suffix[1].lower() if len(suffix) == 2 else _sniff_audio(raw)
        return raw, fmt

    # ------------------------------------------------------------------ 出站

    def send(self, message: OutgoingMessage) -> dict[str, Any]:
        """发一条。有 ``audio`` 就同时发音频。

        返回的东西**不含正文** —— 它进日志，而日志会被复制到别处去。
        """
        out: dict[str, Any] = {"chat_id_tail": message.chat_id[-4:], "text": False, "audio": False}
        token = str((message.reply_context or {}).get("context_token") or "")
        if not token:
            token = self.context_tokens.get(message.chat_id, "")
        if message.text.strip():
            self._send_text(message.chat_id, message.text, token)
            out["text"] = True
            out["chars"] = len(message.text)
        if message.audio:
            out["audio"] = self._send_audio(
                message.chat_id, message.audio, message.audio_format or "wav", token
            )
            out["audio_bytes"] = len(message.audio)
        if not out["text"] and not out["audio"]:
            raise ChannelError("这条消息既没有文字也没有音频")
        return out

    def _send_text(self, chat_id: str, text: str, context_token: str) -> None:
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": secrets.token_hex(8),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        response = self._post(EP_SEND_MESSAGE, {"msg": message}, API_TIMEOUT_MS)
        self._raise_on_error(response, "sendmessage")

    def _raise_on_error(self, response: Mapping[str, Any], where: str) -> None:
        ret, errcode = response.get("ret"), response.get("errcode")
        if _is_stale_session(ret, errcode, response.get("errmsg")):
            raise ChannelError(f"{where}: 会话过期，要重新扫码登录")
        for code in (ret, errcode):
            if isinstance(code, int) and code not in (0, None):
                raise ChannelError(f"{where}: iLink 回 {code} {response.get('errmsg') or ''}".strip())

    def _send_audio(self, chat_id: str, audio: bytes, fmt: str, context_token: str) -> str:
        """上传音频并发出去。返回走的是哪条路（``voice`` / ``file``）。

        **原生语音气泡在上游没跑通**（Hermes 的 `send_voice` 注释），所以默认走文件附件：
        对方拿到的是一个能播的文件。想试原生就把 ``voice_native`` 打开 —— 那条路是
        REAL-WEIXIN，我没验过。
        """
        native = self.voice_native
        media_type = MEDIA_VOICE if native else MEDIA_FILE
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        raw_size = len(audio)
        upload = self._post(
            EP_GET_UPLOAD_URL,
            {
                "to_user_id": chat_id,
                "media_type": media_type,
                "filekey": filekey,
                "rawsize": raw_size,
                "rawfilemd5": hashlib.md5(audio).hexdigest(),  # noqa: S324 - 协议要 md5
                "filesize": padded_size(raw_size),
                "aeskey": aes_key.hex(),
            },
            API_TIMEOUT_MS,
        )
        self._raise_on_error(upload, "getuploadurl")
        full_url = str(upload.get("upload_full_url") or "")
        param = str(upload.get("upload_param") or "")
        if full_url:
            url = full_url
        elif param:
            url = (
                f"{self.cdn_base_url.rstrip('/')}/upload"
                f"?upload_param={urllib.parse.quote(param, safe='')}"
                f"&filekey={urllib.parse.quote(filekey, safe='')}"
            )
        else:
            raise ChannelError("getuploadurl 既没给 upload_full_url 也没给 upload_param")
        ciphertext = aes128_ecb_encrypt(audio, aes_key)
        # **POST，不是 PUT。** 上游踩过：PUT 在微信 CDN 上回 404。
        body = self.transport.post_bytes(
            url,
            ciphertext,
            {"Content-Type": "application/octet-stream", "User-Agent": API_USER_AGENT},
            60.0,
        )
        encrypted_query = _upload_reply_param(body)
        item_key = "voice_item" if native else "file_item"
        item_type = ITEM_VOICE if native else ITEM_FILE
        media: dict[str, Any] = {
            "filekey": filekey,
            "aes_key": aes_key.hex(),
            "rawsize": raw_size,
            "file_name": f"vox-{int(time.time())}.{fmt}",
        }
        if encrypted_query:
            media["encrypt_query_param"] = encrypted_query
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": secrets.token_hex(8),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": item_type, item_key: {"media": media}}],
        }
        if context_token:
            message["context_token"] = context_token
        response = self._post(EP_SEND_MESSAGE, {"msg": message}, API_TIMEOUT_MS)
        self._raise_on_error(response, "sendmessage(audio)")
        return "voice" if native else "file"

    def close(self) -> None:
        """没有长连接要收 —— 每次请求自带一条。留这个方法是为了满足契约。"""
        return None


def _upload_reply_param(body: bytes) -> str:
    """CDN 上传成功之后回的那个引用参数。回的可能是 JSON，也可能是裸字符串。"""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return ""
        for key in ("encrypt_query_param", "encryptQueryParam", "query_param"):
            value = parsed.get(key)
            if value:
                return str(value)
        return ""
    return text


def _sniff_audio(raw: bytes) -> str:
    """按魔数认格式。微信的语音多是 SILK，而 SILK 我们解不了 —— 认出来是为了**说清楚**。"""
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "wav"
    if raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if raw[:4] == b"OggS":
        return "ogg"
    if raw[:5] == b"#!AMR":
        return "amr"
    if raw[:1] == b"\x02" and b"#!SILK" in raw[:16]:
        return "silk"
    if b"#!SILK" in raw[:16]:
        return "silk"
    return ""


__all__ = [
    "EP_GET_UPDATES",
    "EP_GET_UPLOAD_URL",
    "EP_SEND_MESSAGE",
    "ILINK_BASE_URL",
    "LONG_POLL_TIMEOUT_MS",
    "WEIXIN_CDN_BASE_URL",
    "HttpTransport",
    "WeixinChannel",
]
