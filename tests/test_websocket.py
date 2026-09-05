"""标准库自己写的 WebSocket 客户端，以及百炼流式合成的协议。

**这两处的失败模式都很静默**，所以判据必须钉在别人的规范上，不是钉在我们自己的实现上：
握手的 `Sec-WebSocket-Accept` 与客户端掩码钉 **RFC 6455** 的官方示例（§1.3 / §5.7），
和 `core/channels/crypto.py` 钉 FIPS-197 官方向量同一个办法。不掩码的帧服务端会直接断连，
而那看起来像网络问题；accept 不验的话一个把 Upgrade 当普通 GET 的代理会让我们开始按帧协议
解 HTML，报出来的是一串「不合协议」而根因一个字都不出现。

Evidence level: AUTO（假 socket，一个字节都不出网）。真机那几次在提交信息里（REAL）。
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio.tts_ws import WsTtsError, synthesize_pcm
from core.ws import (
    OP_BINARY,
    OP_TEXT,
    WebSocketClient,
    WebSocketError,
    check_url,
    connect,
)


class FakeSocket:
    """内存里的 socket。``inbound`` 是服务端会说的字节，``sent`` 记下我们说了什么。"""

    def __init__(self, inbound: bytes = b"") -> None:
        self.inbound = bytearray(inbound)
        self.sent = bytearray()
        self.closed = False

    def recv(self, count: int) -> bytes:
        take = bytes(self.inbound[:count])
        del self.inbound[:count]
        return take  # 空 = 对面关了，这正是 `_read_exactly` 要认的那个信号

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _seconds: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _client(inbound: bytes = b"") -> WebSocketClient:
    return WebSocketClient(sock=FakeSocket(inbound), url="wss://example.test/x")


def _frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """服务端→客户端的一帧（**不掩码**，RFC 6455 §5.1）。"""
    head = bytearray([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        head.append(len(payload))
    elif len(payload) < 65536:
        head.append(126)
        head += struct.pack("!H", len(payload))
    else:
        head.append(127)
        head += struct.pack("!Q", len(payload))
    return bytes(head) + payload


def _unmask(frame: bytes) -> tuple[int, bytes]:
    """把我们发出去的一帧解开。**顺带断言掩码位是 1** —— 那一位是这个文件存在的一半理由。"""
    opcode = frame[0] & 0x0F
    assert frame[1] & 0x80, "客户端帧必须掩码（RFC 6455 §5.3）"
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack("!H", frame[2:4])[0]
        offset = 4
    elif length == 127:
        length = struct.unpack("!Q", frame[2:10])[0]
        offset = 10
    key = frame[offset : offset + 4]
    body = frame[offset + 4 : offset + 4 + length]
    return opcode, bytes(byte ^ key[i % 4] for i, byte in enumerate(body))


# -- 握手 --------------------------------------------------------------------


def test_the_accept_value_matches_the_rfc_6455_example():
    """RFC 6455 §1.3 的官方示例：key `dGhlIHNhbXBsZSBub25jZQ==` → accept
    `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`。算错这一步的后果不是「握手失败」，是**握手看起来成功**
    然后我们开始解一段不是帧的东西。"""
    import base64
    import hashlib

    key = "dGhlIHNhbXBsZSBub25jZQ=="
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = base64.b64encode(hashlib.sha1((key + guid).encode("ascii")).digest()).decode()

    assert accept == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_a_server_that_does_not_upgrade_is_refused_with_its_own_status_line(monkeypatch):
    """一个把 Upgrade 当普通 GET 处理的代理回 200。**必须在这里就停** —— 否则那段 HTML
    会被按帧协议解，报出来是一串「不合协议」而根因一个字都不出现。"""
    sock = FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html>")
    monkeypatch.setattr("socket.create_connection", lambda *_a, **_k: sock)

    with pytest.raises(WebSocketError, match="握手没换协议"):
        connect("ws://127.0.0.1:9/x")


def test_bytes_that_arrive_with_the_handshake_response_are_not_lost(monkeypatch):
    """服务端不必等 —— 第一帧可能**紧跟**在响应头后面同一个 TCP 段里。丢掉它的症状是
    「第一条消息神秘消失」，而那在真机上极难复现。"""
    import base64
    import hashlib

    captured = {}

    def fake_connection(*_args, **_kwargs):
        return captured["sock"]

    monkeypatch.setattr("socket.create_connection", fake_connection)
    monkeypatch.setattr("os.urandom", lambda n: b"\x00" * n)
    key = base64.b64encode(b"\x00" * 16).decode("ascii")
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = base64.b64encode(hashlib.sha1((key + guid).encode()).digest()).decode()
    captured["sock"] = FakeSocket(
        f"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        + _frame(OP_TEXT, b"first")
    )

    client = connect("ws://127.0.0.1:9/x")

    assert client.recv() == (OP_TEXT, b"first")


# -- 帧 ----------------------------------------------------------------------


def test_what_we_send_is_masked_and_round_trips():
    """掩码键必须来自 CSPRNG，所以字节比不了 —— 但解开之后必须等于原文，而掩码位必须是 1。
    不掩码的帧服务端**直接断连**，看起来像网络问题。"""
    client = _client()

    client.send_text("你好 Vox")
    client.send_binary(b"\x00\x01\x02")

    frames = bytes(client.sock.sent)
    opcode, body = _unmask(frames)
    assert (opcode, body) == (OP_TEXT, "你好 Vox".encode("utf-8"))


@pytest.mark.parametrize("size", [0, 1, 125, 126, 127, 65535, 65536])
def test_every_length_encoding_round_trips(size):
    """长度有三种编码（7 位 / 16 位 / 64 位），边界正好在 126 和 65536。写错一个边界的
    症状是「偶尔一句话发不出去」，而它取决于那句话有多长。"""
    client = _client()
    payload = b"x" * size

    client.send_binary(payload)

    opcode, body = _unmask(bytes(client.sock.sent))
    assert (opcode, len(body)) == (OP_BINARY, size)


def test_fragments_are_reassembled_into_one_message():
    """RFC 6455 §5.4：第一帧带 opcode 且 FIN=0，后续是 continuation。分片是服务端的自由，
    不重组的话一段音频会被当成两条消息，而第二条的 opcode 是 0（我们认不出来）。"""
    client = _client(_frame(OP_TEXT, b"Hel", fin=False) + _frame(0x0, b"lo"))

    assert client.recv() == (OP_TEXT, b"Hello")


def test_a_ping_is_answered_without_bothering_the_caller():
    """让调用方处理保活等于让**每个**调用方都记得处理它，而漏掉的那个会在长静默之后被
    服务端断开 —— 症状是「合成到一半没了」。"""
    client = _client(_frame(0x9, b"ping-payload") + _frame(OP_TEXT, b"after"))

    assert client.recv() == (OP_TEXT, b"after")
    opcode, body = _unmask(bytes(client.sock.sent))
    assert (opcode, body) == (0xA, b"ping-payload"), "pong 必须原样回 payload"


def test_a_close_frame_ends_the_stream():
    client = _client(_frame(0x8, struct.pack("!H", 1000)))

    assert client.recv() is None


def test_a_masked_frame_from_the_server_is_a_protocol_error():
    """服务端发来的帧不该掩码（RFC 6455 §5.1）。按它继续解会把一段乱码当成音频 ——
    那比断开难查得多。"""
    masked = bytearray(_frame(OP_TEXT, b"abc"))
    masked[1] |= 0x80  # 谎称掩码
    client = _client(bytes(masked) + b"\x00\x00\x00\x00abc")

    with pytest.raises(WebSocketError, match="掩码帧"):
        client.recv()


def test_an_absurd_frame_length_is_refused_before_allocating():
    """一个坏掉（或恶意）的服务端可以在长度字段里写 2^63，而 `recv` 会照着它去分配。"""
    head = bytes([0x80 | OP_BINARY, 127]) + struct.pack("!Q", 2**40)
    client = _client(head)

    with pytest.raises(WebSocketError, match="超过上限"):
        client.recv()


def test_closing_twice_is_not_an_error():
    """关一个已经断了的连接不是错误，而在收尾路径上抛异常会盖掉真正的那个原因。"""
    client = _client()

    client.close()
    client.close()

    assert client.sock.closed


# -- URL 规则（和本仓库其余四处出网点同一条）----------------------------------


@pytest.mark.parametrize(
    "bad, because",
    [
        ("http://x/y", "不是 WebSocket"),
        ("wss:///y", "没有主机名"),
        ("wss://user:pass@x/y", "不许带凭据"),
        ("ws://example.com/x", "只许连回环"),
    ],
)
def test_the_url_rules_are_the_same_four_as_everywhere_else(bad, because):
    with pytest.raises(WebSocketError, match=because):
        check_url(bad)


@pytest.mark.parametrize(
    "good",
    ["wss://dashscope.aliyuncs.com/api-ws/v1/inference", "ws://127.0.0.1:9/x", "ws://localhost/y"],
)
def test_a_legitimate_url_passes_through_unchanged(good):
    assert check_url(good) == good


# -- 百炼流式合成的协议 -------------------------------------------------------
#
# 时序是实测出来的（`help.aliyun.com` 在这台机器上取不到）：
#   run-task → task-started → continue-task + finish-task → binary 帧 → task-finished


def _event(name: str, **header) -> bytes:
    return _frame(OP_TEXT, json.dumps({"header": {"event": name, **header}, "payload": {}}).encode())


def _served(monkeypatch, inbound: bytes) -> dict:
    """把 `connect` 替成「一个连好的、服务端会说 ``inbound`` 的客户端」。"""
    box: dict = {}

    def fake_connect(url, *, headers=None, timeout_s=30.0):
        client = WebSocketClient(sock=FakeSocket(inbound), url=url)
        box["client"] = client
        box["headers"] = dict(headers or {})
        box["url"] = url
        return client

    monkeypatch.setattr("core.audio.tts_ws.connect", fake_connect)
    return box


def _uplink(box: dict) -> list[dict]:
    """我们发出去的那几个 JSON 事件（已解掩码）。"""
    sent = bytes(box["client"].sock.sent)
    events = []
    offset = 0
    while offset < len(sent):
        # 逐帧走：每帧 = 2 字节头 + 可能的扩展长度 + 4 字节掩码键 + payload
        length = sent[offset + 1] & 0x7F
        extra = 0 if length < 126 else (2 if length == 126 else 8)
        if extra == 2:
            length = struct.unpack("!H", sent[offset + 2 : offset + 4])[0]
        elif extra == 8:
            length = struct.unpack("!Q", sent[offset + 2 : offset + 10])[0]
        end = offset + 2 + extra + 4 + length
        _op, body = _unmask(sent[offset:end])
        if body[:1] == b"{":
            events.append(json.loads(body.decode("utf-8")))
        offset = end
    return events


def test_the_three_uplink_events_share_one_task_id(monkeypatch):
    """三个上行事件的 `task_id` **必须是同一个**，否则服务端按「没有这个任务」拒。"""
    box = _served(
        monkeypatch,
        _event("task-started") + _frame(OP_BINARY, b"\x01\x00") + _event("task-finished"),
    )

    synthesize_pcm(model="m", voice="v", text="喂", key="k")

    events = _uplink(box)
    assert [e["header"]["action"] for e in events] == ["run-task", "continue-task", "finish-task"]
    assert len({e["header"]["task_id"] for e in events}) == 1


def test_the_instruction_goes_into_parameters_not_input(monkeypatch):
    """**HTTP 那条路把它放 `input` 里，WS 上放 `parameters` 里。** 放错位置不报错，只会让
    使用者配的语气静默失效 —— 而这个仓库为「能改、能存、不生效」栽过三次。"""
    box = _served(monkeypatch, _event("task-started") + _frame(OP_BINARY, b"\x01\x00")
                  + _event("task-finished"))

    synthesize_pcm(model="m", voice="v", text="喂", key="k", instruction=" 温柔一点 ")

    run = _uplink(box)[0]["payload"]
    assert run["parameters"]["instruction"] == "温柔一点"
    assert "instruction" not in run["input"]


def test_the_credential_goes_in_the_header_never_the_url(monkeypatch):
    """一个带 token 的 URL 会进日志、进异常消息、进代理的访问记录。"""
    box = _served(monkeypatch, _event("task-started") + _frame(OP_BINARY, b"\x01\x00")
                  + _event("task-finished"))

    synthesize_pcm(model="m", voice="v", text="喂", key="secret-token")

    assert box["headers"]["Authorization"] == "bearer secret-token"
    assert "secret-token" not in box["url"]


def test_audio_frames_are_concatenated_in_order(monkeypatch):
    _served(monkeypatch, _event("task-started") + _frame(OP_BINARY, b"\x01\x00")
            + _frame(OP_BINARY, b"\x02\x00") + _event("task-finished"))

    raw, first_ms = synthesize_pcm(model="m", voice="v", text="喂", key="k")

    assert raw == b"\x01\x00\x02\x00"
    assert first_ms >= 0


def test_a_task_failed_event_carries_the_error_code(monkeypatch):
    """`ModelNotFound` 与 `AllocationQuota.FreeTierOnly` 是两件完全不同的事，而两者在裸
    socket 层都只是「断了」。实测这两条都真的出现过。"""
    _served(monkeypatch, _frame(
        OP_TEXT,
        json.dumps({"header": {"event": "task-failed", "error_code": "ModelNotFound",
                               "error_message": "Model not found (x)!"}, "payload": {}}).encode(),
    ))

    with pytest.raises(WsTtsError, match="ModelNotFound"):
        synthesize_pcm(model="x", voice="v", text="喂", key="k")


def test_a_barge_in_stops_between_frames(monkeypatch):
    """打断不必等整句合成完。已收到的音频**原样交还** —— 丢掉它等于让一次打断多花一次合成。"""
    _served(monkeypatch, _event("task-started") + _frame(OP_BINARY, b"\x01\x00")
            + _frame(OP_BINARY, b"\x02\x00") + _event("task-finished"))

    raw, _first = synthesize_pcm(model="m", voice="v", text="喂", key="k",
                                 should_stop=lambda: True)

    assert raw == b"\x01\x00", "第一帧之后就该停"


def test_a_connection_that_drops_with_audio_in_hand_counts_as_complete(monkeypatch):
    """服务端在 task-finished 之后立刻关很常见。有音频就当它完整 —— 报失败会让一句已经
    合成好的话被丢掉。"""
    _served(monkeypatch, _event("task-started") + _frame(OP_BINARY, b"\x01\x00"))

    raw, _first = synthesize_pcm(model="m", voice="v", text="喂", key="k")

    assert raw == b"\x01\x00"


def test_a_connection_that_drops_with_nothing_is_a_failure(monkeypatch):
    """「静音」和「网断了」在使用者那一侧同形，所以这两个必须分开报。"""
    _served(monkeypatch, _event("task-started"))

    with pytest.raises(WsTtsError, match="断了"):
        synthesize_pcm(model="m", voice="v", text="喂", key="k")


def test_empty_text_never_opens_a_connection(monkeypatch):
    """一次握手 + TLS 是 200–500 ms。为一句空话付它是纯浪费。"""
    calls = []
    monkeypatch.setattr("core.audio.tts_ws.connect",
                        lambda *a, **k: calls.append(1) or pytest.fail("不该连"))

    assert synthesize_pcm(model="m", voice="v", text="   ", key="k") == (b"", 0)
    assert not calls


def test_a_missing_credential_fails_before_connecting(monkeypatch):
    monkeypatch.setattr("core.audio.tts_ws.connect",
                        lambda *a, **k: pytest.fail("没有凭据就不该连"))

    with pytest.raises(WsTtsError, match="凭据"):
        synthesize_pcm(model="m", voice="v", text="喂", key="")


