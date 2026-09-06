"""The HTTP adapter, against a mock OpenAI-compatible server.

Evidence level: SIM. A local socket and hand-written responses, not a real
OpenAI-compatible gateway or remote agent. A real turn through the OpenClaw
Gateway stays REAL-AGENT (ADR 003 blocker).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.agents.contract import Task
from core.agents.environment import describe_host
from core.agents.http import HttpAgentAdapter, HttpAgentError


def task(text: str = "hello") -> Task:
    return Task(id="t-1", text=text)


def serve(body: bytes, *, content_type: str = "application/json", status: int = 200):
    """A one-shot endpoint recording what it received. Returns a cleanup pair."""
    received: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append((self.path, self.rfile.read(length).decode("utf-8")))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, received


def _stop(server, thread):
    server.shutdown()
    thread.join(timeout=2)


def test_describe_declares_the_http_kind():
    adapter = HttpAgentAdapter(name="openclaw", url="http://127.0.0.1:1")

    assert adapter.describe().kind == "http"
    assert adapter.check()["available"] is True
    assert adapter.check()["token_configured"] is False


def test_streams_sse_deltas_incrementally():
    body = (
        b'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    server, thread, _received = serve(body, content_type="text/event-stream")
    try:
        adapter = HttpAgentAdapter(name="mock", url=f"http://127.0.0.1:{server.server_port}/v1")
        chunks = list(adapter.stream(task()))
        assert [chunk.kind for chunk in chunks] == ["text", "text", "done"]
        assert "".join(chunk.text for chunk in chunks if chunk.kind == "text") == "hello world"
        assert chunks[-1].error is None
        assert chunks[-1].elapsed_ms is not None
    finally:
        _stop(server, thread)


def test_a_chat_completions_request_is_sent_with_model_and_stream():
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    server, thread, received = serve(body)
    try:
        adapter = HttpAgentAdapter(name="mock", url=f"http://127.0.0.1:{server.server_port}/v1", model="claude")
        list(adapter.stream(task("讲个笑话")))
        path, payload = received[0]
        assert path == "/v1/chat/completions"
        sent = json.loads(payload)
        assert sent["model"] == "claude"
        assert sent["stream"] is True
        assert sent["messages"][1] == {"role": "user", "content": "讲个笑话"}
    finally:
        _stop(server, thread)


def test_a_system_message_states_the_real_host_before_the_user_turn():
    """system message 必须在,而且必须说这台机器真实的操作系统。

    不发它时,模型看到的唯一 system prompt 是**端点自己注入的那份**。实测中转站注入的
    那份说「操作系统是 linux,当前工作目录是 /」,于是它给 Windows 用户建议 X11 与
    PulseAudio —— 一段语法正确、事实全错的回答,而 TTS 会把它念出来。

    断言里不写死 "Windows":这个仓库跑在哪台机器上不是测试该规定的事。断言的是
    **它报的和 platform 报的一致**,那才是「不许猜」这条要求。
    """
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    server, thread, received = serve(body)
    try:
        adapter = HttpAgentAdapter(name="mock", url=f"http://127.0.0.1:{server.server_port}/v1")
        list(adapter.stream(task("现在几点")))
        sent = json.loads(received[0][1])
        system = sent["messages"][0]
        assert system["role"] == "system"
        assert describe_host() in system["content"]
        # 两条它自己猜不到、猜错了就会念出错误答案的事。
        assert "没有" in system["content"]  # 没有文件系统/终端
        assert "朗读" in system["content"]  # 回答会被念出来
    finally:
        _stop(server, thread)


def test_a_non_streaming_reply_becomes_one_text_chunk():
    body = b'{"choices":[{"message":{"content":"the answer"}}]}'
    server, thread, _received = serve(body, content_type="application/json")
    try:
        adapter = HttpAgentAdapter(name="mock", url=f"http://127.0.0.1:{server.server_port}")
        chunks = list(adapter.stream(task()))
        assert [chunk.kind for chunk in chunks] == ["text", "done"]
        assert chunks[0].text == "the answer"
    finally:
        _stop(server, thread)


def test_a_non_2xx_is_a_failed_chunk_not_an_exception():
    server, thread, _received = serve(b'{"error":{"message":"boom"}}', status=500)
    try:
        adapter = HttpAgentAdapter(name="mock", url=f"http://127.0.0.1:{server.server_port}")
        chunks = list(adapter.stream(task()))
        assert chunks[-1].kind == "done"
        assert chunks[-1].error == "HTTP 500"
    finally:
        _stop(server, thread)


def test_an_error_frame_in_the_stream_is_a_failed_chunk():
    body = b'data: {"error":{"message":"stream broke"}}\n\n'
    server, thread, _received = serve(body, content_type="text/event-stream")
    try:
        adapter = HttpAgentAdapter(name="mock", url=f"http://127.0.0.1:{server.server_port}")
        chunks = list(adapter.stream(task()))
        assert chunks[-1].kind == "done"
        assert chunks[-1].error == "stream broke"
    finally:
        _stop(server, thread)


def test_bad_urls_are_refused_at_construction():
    for url in (
        "ftp://localhost:1",
        "http://user:pass@localhost:1",
        "http://example.com",
        "not-a-url",
    ):
        with pytest.raises(HttpAgentError):
            HttpAgentAdapter(name="mock", url=url)
    # The accepted set: loopback http, https anywhere.
    HttpAgentAdapter(name="mock", url="http://localhost:1")
    HttpAgentAdapter(name="mock", url="http://127.0.0.1:1")
    HttpAgentAdapter(name="mock", url="https://example.com")


def test_a_bearer_token_comes_from_the_environment_only():
    # ``token=None`` reads VOX_AGENT_HTTP_TOKEN; an explicit empty string opts out.
    adapter = HttpAgentAdapter(name="mock", url="http://127.0.0.1:1", token="")
    assert adapter.token == ""


def test_cancel_is_safe_on_an_unknown_turn():
    adapter = HttpAgentAdapter(name="mock", url="http://127.0.0.1:1")
    adapter.cancel("never-started")  # must not raise



def test_the_credential_variable_can_be_named_in_config(monkeypatch):
    """``key_env`` 指名去读哪个环境变量。**名字，不是值。**

    2026-08-31 实机：relay 的有效凭据在 `ANTHROPIC_AUTH_TOKEN` 里，而适配器只读
    `VOX_AGENT_HTTP_TOKEN`，于是每一轮对话都被端点拒掉。更糟的是失败的形状 —— 流式路径上
    服务端直接断连，Python 报 `URLError: [SSL: UNEXPECTED_EOF_WHILE_READING]`，看起来像
    证书或网络问题，而真实原因是凭据不对。
    """
    monkeypatch.setenv("VOX_AGENT_HTTP_TOKEN", "default-one")
    monkeypatch.setenv("SOME_OTHER_RELAY_KEY", "the-right-one")

    named = HttpAgentAdapter(name="relay", url="https://example.test/v1", key_env="SOME_OTHER_RELAY_KEY")
    fallback = HttpAgentAdapter(name="relay", url="https://example.test/v1")

    assert named.token == "the-right-one"
    assert fallback.token == "default-one", "没指名就用默认那个变量"


def test_check_reports_the_variable_name_but_never_the_value(monkeypatch):
    """`token_configured: true` 不够用 —— 两个变量都有值时它对两边都是 true，
    于是「凭据放错变量了」这件事在读数里看不见。名字必须报出来，值绝不。"""
    monkeypatch.setenv("SOME_OTHER_RELAY_KEY", "sk-secret-value-here")

    view = HttpAgentAdapter(
        name="relay", url="https://example.test/v1", key_env="SOME_OTHER_RELAY_KEY"
    ).check()

    assert view["key_env"] == "SOME_OTHER_RELAY_KEY"
    assert view["token_configured"] is True
    assert "sk-secret-value-here" not in json.dumps(view)


def test_the_credential_variable_is_not_editable_from_a_web_page():
    """反向断言，而且它比上面两条重要：让网页决定读哪个环境变量，等于让它决定把哪个凭据
    发到哪个端点。值走 /api/secret（有白名单），变量**名**留在配置文件里。"""
    from core.console.routes import AGENT_EDITABLE

    assert "key_env" not in AGENT_EDITABLE
    assert "url" not in AGENT_EDITABLE


def test_the_shipped_relay_names_the_variable_that_actually_works():
    """`core/config_edit.py` 只改已存在的键，而 registry 只透传 schema 认识的键 ——
    所以这一行必须真的在文件里。2026-08-31 实测这个端点只接受 ANTHROPIC_AUTH_TOKEN。"""
    from core.agents.registry import load_agents_config

    config = load_agents_config()
    relay = next(entry for entry in config["agents"] if entry["name"] == "relay")
    assert relay["key_env"] == "ANTHROPIC_AUTH_TOKEN"
