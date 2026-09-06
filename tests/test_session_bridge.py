import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.session_bridge import BridgeError, LocalEvoXTransport


class Handler(BaseHTTPRequestHandler):
    paths = []

    def do_POST(self):
        type(self).paths.append(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        assert self.headers["Authorization"] == "Bearer secret"
        body = json.dumps({"turn_id": "turn-1", "echo": payload.get("text")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_bridge_requires_token_and_loopback():
    with pytest.raises(BridgeError):
        LocalEvoXTransport("http://localhost:1", "").send("hi")
    with pytest.raises(BridgeError):
        LocalEvoXTransport("http://example.com", "secret").send("hi")
    with pytest.raises(BridgeError):
        LocalEvoXTransport("http://localhost.evil.example", "secret").send("hi")
    with pytest.raises(BridgeError):
        LocalEvoXTransport("http://user:pass@localhost:1", "secret").send("hi")


def test_bridge_sends_authenticated_turn():
    Handler.paths.clear()
    server = ThreadingHTTPServer(("localhost", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = LocalEvoXTransport(f"http://localhost:{server.server_port}", "secret")
        assert transport.send("hello")["echo"] == "hello"
        transport.cancel("turn/with spaces")
        assert Handler.paths == [
            "/v1/conversation/turns",
            "/v1/conversation/turns/turn%2Fwith%20spaces/cancel",
        ]
    finally:
        server.shutdown()
        thread.join()


def test_bridge_requires_turn_id_in_send_response():
    class MissingTurnHandler(Handler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"reply": "missing turn"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("localhost", 0), MissingTurnHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = LocalEvoXTransport(f"http://localhost:{server.server_port}", "secret")
        with pytest.raises(BridgeError, match="missing turn_id"):
            transport.send("hello")
    finally:
        server.shutdown()
        thread.join()


# -- connect-phase retry -------------------------------------------------------


import socket  # noqa: E402 - test-scoped import keeps the table above stable
from urllib.error import HTTPError, URLError  # noqa: E402


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"turn_id": "turn-1"}).encode()


def _patch_urlopen(monkeypatch, outcomes):
    """Replace urlopen with a scripted list of raises/returns."""
    calls = {"count": 0}
    def fake_urlopen(request, timeout=None):
        index = calls["count"]
        calls["count"] += 1
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr("core.session_bridge.urlopen", fake_urlopen)
    return calls


def refused():
    return URLError(ConnectionRefusedError("target machine refused"))


def test_connection_refused_is_retried_then_recovers(monkeypatch):
    transport = LocalEvoXTransport("http://localhost:9", "secret", attempts=3, retry_backoff_s=0.0)
    calls = _patch_urlopen(monkeypatch, [refused(), FakeResponse()])

    assert transport.send("hello")["turn_id"] == "turn-1"
    assert calls["count"] == 2


def test_dns_failure_is_also_connect_phase(monkeypatch):
    transport = LocalEvoXTransport("http://localhost:9", "secret", attempts=2, retry_backoff_s=0.0)
    calls = _patch_urlopen(monkeypatch, [URLError(socket.gaierror(-2, "name")), FakeResponse()])

    assert transport.send("hello")["turn_id"] == "turn-1"
    assert calls["count"] == 2


def test_exhausted_connect_retries_raise_bridge_error(monkeypatch):
    transport = LocalEvoXTransport("http://localhost:9", "secret", attempts=3, retry_backoff_s=0.0)
    calls = _patch_urlopen(monkeypatch, [refused()])

    with pytest.raises(BridgeError):
        transport.send("hello")
    assert calls["count"] == 3


def test_timeout_is_never_retried_even_with_budget(monkeypatch):
    transport = LocalEvoXTransport("http://localhost:9", "secret", attempts=5, retry_backoff_s=0.0)
    calls = _patch_urlopen(monkeypatch, [TimeoutError("read timed out")])

    with pytest.raises(BridgeError, match="bridge failed"):
        transport.send("hello")
    assert calls["count"] == 1  # the turn may have executed; no re-send


def test_http_error_is_never_retried(monkeypatch):
    transport = LocalEvoXTransport("http://localhost:9", "secret", attempts=4, retry_backoff_s=0.0)
    calls = _patch_urlopen(monkeypatch, [HTTPError("http://x", 503, "boom", None, None)])

    with pytest.raises(BridgeError):
        transport.send("hello")
    assert calls["count"] == 1


def test_default_single_attempt_keeps_historical_behaviour(monkeypatch):
    transport = LocalEvoXTransport("http://localhost:9", "secret")
    assert transport.attempts == 1
    calls = _patch_urlopen(monkeypatch, [refused()])

    with pytest.raises(BridgeError):
        transport.send("hello")
    assert calls["count"] == 1
