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
        assert sent["messages"][0]["content"] == "讲个笑话"
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
    # ``token=None`` reads EVOX_AGENT_HTTP_TOKEN; an explicit empty string opts out.
    adapter = HttpAgentAdapter(name="mock", url="http://127.0.0.1:1", token="")
    assert adapter.token == ""


def test_cancel_is_safe_on_an_unknown_turn():
    adapter = HttpAgentAdapter(name="mock", url="http://127.0.0.1:1")
    adapter.cancel("never-started")  # must not raise

