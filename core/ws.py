"""WebSocket 客户端 —— 标准库实现，**只做这个项目需要的那个子集**。

## 为什么自己写

百炼的流式识别与流式合成只有 WebSocket 接口（`qwen3-asr-flash-realtime` /
`qwen3-tts-flash-realtime`），而它们是「第一声要等多久」里剩下的两大块：2026-09-05 实测
ASR 往返 2.7 s、TTS 整段 3.6 s，两条流式路都能压到亚秒。标准库没有 WS 客户端。

按项目那条「先搜先例后手写」的三级筛选：`websockets`（BSD-3）与 `websocket-client`
（Apache-2.0）都能用、都不带遥测、都不夺架构所有权 —— 所以否决它们的理由不是那三条，是
**需要的子集小到不值一个依赖**：客户端单向掩码、文本 + 二进制帧、分片重组、ping/pong、
close。没有扩展、没有 permessage-deflate、没有服务端、没有 asyncio。这个仓库为同样的理由
自己写过 AES-128（`core/channels/crypto.py`，比帧协议难得多），先例在那里。

代价说清楚：**握手与掩码写错的失败模式很静默** —— 不掩码的帧会被服务端直接断连，看起来
像网络问题。所以那两处各有一条钉在 RFC 6455 官方示例上的测试，和 crypto.py 钉 FIPS-197
同一个办法。

## 出网边界

`connect()` 沿用本仓库其余四处出网点同一条 URL 规则：**明文 `ws://` 只许回环**、
**URL 里带凭据一律拒绝**。凭据走 header，永不进 URL —— 一个带 token 的 URL 会进日志、
进异常消息、进代理的访问记录。

## 线程模型

一个连接一个线程用，**不加锁**。`send` 与 `recv` 从两个线程同时调是未定义的 —— 需要那样
用的调用方自己加锁。这不是偷懒：这一层加了锁之后，「谁在等谁」会变成一个要读两个文件才能
回答的问题，而流式合成的取消路径正需要那个答案是显然的。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

#: RFC 6455 §1.3 的魔术串。服务端把它接在客户端 key 后面做 SHA-1 —— 验这一步才知道对面
#: 真的是个 WebSocket 服务端，而不是一个把 Upgrade 请求当普通 GET 处理的反向代理。
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: opcode（RFC 6455 §5.2）。这一层只认这五个，其余按协议错误处理。
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: 单帧上限。**不是协议限制，是内存保险**：一个坏掉（或恶意）的服务端可以在长度字段里写
#: 2^63，而 `recv` 会照着它去分配。64 MiB 远大于任何一段语音回复。
MAX_FRAME_BYTES = 64 * 1024 * 1024

#: 一条消息（含分片）的上限，同一条理由。
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class WebSocketError(RuntimeError):
    """握手失败、协议错、或者连接在中途断了。带上原因 —— 「对面不是 WS」和「凭据不对」
    要分开看，而两者在裸 socket 层都只是「连接断了」。"""


@dataclass
class WebSocketClient:
    """一个已连上的 WebSocket。用 `connect()` 建，别自己 new。"""

    sock: Any  # noqa: ANN401 - ssl.SSLSocket 或 socket.socket
    url: str
    #: 收到但还没被 `recv` 取走的字节（TCP 是流，帧边界要自己找）。
    _buffer: bytearray = field(default_factory=bytearray, repr=False)
    _closed: bool = False

    # -- 发 ------------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, str(text).encode("utf-8"))

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(OP_BINARY, bytes(payload))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """一帧，不分片。**客户端必须掩码**（RFC 6455 §5.3）—— 不掩码的帧服务端直接断连，
        而那看起来像网络问题。掩码键必须来自 CSPRNG（同一节明写），所以是 `os.urandom`。"""
        if self._closed:
            raise WebSocketError("连接已经关了")
        header = bytearray([0x80 | opcode])  # FIN = 1，一帧一条消息
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        key = os.urandom(4)
        header += key
        masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        try:
            self.sock.sendall(bytes(header) + masked)
        except OSError as exc:
            self._closed = True
            raise WebSocketError(f"发不出去：{type(exc).__name__}: {exc}") from exc

    # -- 收 ------------------------------------------------------------------

    def recv(self) -> tuple[int, bytes] | None:
        """下一条**消息**（分片已重组）。``None`` = 对面关了。

        ping 在这里就地回 pong 并继续等 —— 让调用方处理保活等于让每个调用方都记得处理它，
        而漏掉的那个会在长静默之后被服务端断开，症状是「合成到一半没了」。
        """
        opcode = 0
        chunks: list[bytes] = []
        total = 0
        while True:
            frame = self._read_frame()
            if frame is None:
                return None
            fin, this_op, payload = frame
            if this_op == OP_CLOSE:
                self._closed = True
                return None
            if this_op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if this_op == OP_PONG:
                continue
            if this_op != OP_CONTINUATION:
                opcode = this_op
            chunks.append(payload)
            total += len(payload)
            if total > MAX_MESSAGE_BYTES:
                raise WebSocketError(f"一条消息超过 {MAX_MESSAGE_BYTES} 字节，不收")
            if fin:
                return opcode, b"".join(chunks)

    def _read_frame(self) -> tuple[bool, int, bytes] | None:
        head = self._read_exactly(2)
        if head is None:
            return None
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            extra = self._read_exactly(2)
            if extra is None:
                return None
            length = struct.unpack("!H", extra)[0]
        elif length == 127:
            extra = self._read_exactly(8)
            if extra is None:
                return None
            length = struct.unpack("!Q", extra)[0]
        if length > MAX_FRAME_BYTES:
            raise WebSocketError(f"一帧声称有 {length} 字节，超过上限，不收")
        if masked:
            # 服务端发来的帧**不该**掩码（RFC 6455 §5.1）。收到就是对面实现有问题，
            # 而按它继续解会把一段乱码当成音频 —— 那比断开难查。
            raise WebSocketError("服务端发来了掩码帧，不合协议")
        payload = self._read_exactly(length) if length else b""
        if payload is None:
            return None
        return fin, opcode, bytes(payload)

    def _read_exactly(self, count: int) -> bytes | None:
        while len(self._buffer) < count:
            try:
                chunk = self.sock.recv(65536)
            except (TimeoutError, socket.timeout) as exc:
                raise WebSocketError("等对面回话超时") from exc
            except OSError as exc:
                self._closed = True
                raise WebSocketError(f"读不了：{type(exc).__name__}: {exc}") from exc
            if not chunk:
                self._closed = True
                return None
            self._buffer += chunk
        taken = bytes(self._buffer[:count])
        del self._buffer[:count]
        return taken

    # -- 收尾 ----------------------------------------------------------------

    def close(self) -> None:
        """发一个 close 帧再关 socket。**失败一律吞掉** —— 关一个已经断了的连接不是错误，
        而在收尾路径上抛异常会盖掉真正的那个原因。"""
        if not self._closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except Exception:  # noqa: BLE001
                pass
        self._closed = True
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> WebSocketClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()



def check_url(url: str) -> str:
    """URL 合规就原样返回，否则抛。**和本仓库其余四处出网点同一条规则。**

    两条：明文 `ws://` 只许回环（外网明文传的是使用者说的话）；URL 里带凭据一律拒绝
    （带 token 的 URL 会进日志、进异常消息、进代理的访问记录 —— 凭据走 header）。
    """
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"ws", "wss"}:
        raise WebSocketError(f"不是 WebSocket 地址：{text[:60]!r}")
    if not parsed.hostname:
        raise WebSocketError(f"地址里没有主机名：{text[:60]!r}")
    if parsed.username or parsed.password:
        raise WebSocketError("URL 里不许带凭据 —— 凭据走 header")
    if parsed.scheme == "ws" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise WebSocketError(f"明文 ws:// 只许连回环，不是 {parsed.hostname}")
    return text


def connect(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
) -> WebSocketClient:
    """握手并返回一个连好的客户端。

    **`Sec-WebSocket-Accept` 必须验。** 不验的话一个把 Upgrade 当普通 GET 处理的代理会让
    我们开始按帧协议解一段 HTML —— 那是一堆「不合协议」的报错，而根因（对面不是 WS 服务端）
    一个字都不会出现。
    """
    target = check_url(url)
    parsed = urlparse(target)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}" if parsed.port else f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    raw = socket.create_connection((host, port), timeout=timeout_s)
    try:
        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        sock.settimeout(timeout_s)
        sock.sendall(request)
        client = WebSocketClient(sock=sock, url=target)
        _finish_handshake(client, key)
        return client
    except Exception:
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass
        raise


def _finish_handshake(client: WebSocketClient, key: str) -> None:
    """读到响应头结束（``\\r\\n\\r\\n``），检查 101 与 accept。

    响应头之后可能**紧接着**就是第一帧（服务端不必等），所以多读到的字节留在缓冲里 ——
    丢掉它们会让第一条消息神秘消失。
    """
    while b"\r\n\r\n" not in client._buffer:  # noqa: SLF001 - 同一个模块
        if len(client._buffer) > 65536:  # noqa: SLF001
            raise WebSocketError("握手响应头超过 64 KiB，对面大概不是 WebSocket 服务端")
        try:
            more = client.sock.recv(65536)
        except (TimeoutError, socket.timeout) as exc:
            raise WebSocketError("握手超时") from exc
        if not more:
            raise WebSocketError("握手时对面就断了")
        client._buffer += more  # noqa: SLF001
    head, _, rest = bytes(client._buffer).partition(b"\r\n\r\n")
    client._buffer[:] = bytearray(rest)  # noqa: SLF001
    text = head.decode("latin-1")
    first = text.split("\r\n", 1)[0]
    if " 101" not in first:
        raise WebSocketError(f"握手没换协议：{first[:120]}")
    accept = ""
    for line in text.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "sec-websocket-accept":
            accept = value.strip()
    expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode()
    if accept != expected:
        raise WebSocketError("Sec-WebSocket-Accept 不对 —— 对面不是 WebSocket 服务端")


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_MESSAGE_BYTES",
    "OP_BINARY",
    "OP_CLOSE",
    "OP_PING",
    "OP_PONG",
    "OP_TEXT",
    "WebSocketClient",
    "WebSocketError",
    "check_url",
    "connect",
]
