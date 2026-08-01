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
