"""The console's HTTP server: loopback only, token by default, zero dependencies.

Standard library ``ThreadingHTTPServer``. The reason is the one this project has
given twice already -- ``tomllib`` instead of PyYAML, a hand-written JSON Schema
subset instead of ``jsonschema`` -- and it holds again: the routing surface here is
a dozen endpoints, which is not worth starlette + pydantic + uvicorn + anyio and a
node build chain. It also keeps the console entirely out of ``desktop/``, so the
orb and the console can be worked on at the same time without touching one file
in common.

This module opens a listening socket, which is a real widening of the attack
surface, so three things are non-negotiable:

- **The bind address must be loopback.** Not a default, a check: ``0.0.0.0`` is
  refused rather than warned about.
- **Every request needs the token**, including the page itself. The page is a
  single self-contained HTML file with its CSS and JS inlined, so there is no
  second request that would need an exemption.
- **``--no-token`` exists and says so loudly.** Preview tooling opens a URL without
  a query string, so a development mode is needed; it is only permitted on
  loopback, it prints a warning, and ``describe()`` reports it.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.console.routes import ApiError, ConsoleApi

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Requests larger than this are refused unread. Voiceprint clips arrive as base64
#: WAV, three of them, so the ceiling is a few MB rather than a few kB.
MAX_BODY_BYTES = 8_000_000


class ConsoleError(RuntimeError):
    """A console that cannot be started safely."""


def loopback_problem(host: str) -> str | None:
    """``None`` when ``host`` cannot be reached from another machine."""
    if host in {"localhost", ""}:
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        return f"console host must be a loopback address, got {host!r}"
    return (
        f"console host {host!r} is reachable from the network; "
        "the console binds loopback only"
    )


@dataclass
class ConsoleServer:
    """One HTTP server over one ``ConsoleApi``. ``start`` binds, ``stop`` closes."""

    api: ConsoleApi
    host: str = "127.0.0.1"
    port: int = 8899
    #: Empty means "generate one". Only ``require_token=False`` removes the check.
    token: str = ""
    require_token: bool = True
    _httpd: Any = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        problem = loopback_problem(self.host)
        if problem:
            raise ConsoleError(problem)
        if self.require_token and not self.token:
            self.token = secrets.token_urlsafe(24)

    # ------------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        """The URL to open, token included when one is required."""
        base = f"http://{self.host}:{self.port}/"
        return f"{base}?t={self.token}" if self.require_token else base

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        handler = _make_handler(self)
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            raise ConsoleError(f"cannot bind {self.host}:{self.port}: {exc}") from exc
        # Port 0 means "pick one"; read back what was actually bound.
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="vox-console", daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    def describe(self) -> dict[str, Any]:
        """Status without the secret. The token is never reported, logged or sent."""
        return {
            "host": self.host,
            "port": self.port,
            "running": self._httpd is not None,
            "token_required": self.require_token,
        }

    # -------------------------------------------------------------------- routing

    def authorised(self, header: str | None, query: dict[str, list[str]]) -> bool:
        """Bearer header or ``?t=``, compared in constant time."""
        if not self.require_token:
            return True
        candidate = ""
        if header and header.lower().startswith("bearer "):
            candidate = header[7:].strip()
        elif query.get("t"):
            candidate = query["t"][0]
        if not candidate:
            return False
        return secrets.compare_digest(candidate, self.token)

    def page(self) -> bytes:
        """The single self-contained HTML file. Read per request so an edit shows
        up on reload during development."""
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            return b"<!doctype html><title>Vox</title><p>console/static/index.html is missing"
        return index.read_bytes()

    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> Any:
        """One method + path to one ``ConsoleApi`` call. Unknown paths raise 404."""
        api = self.api
        if method == "GET":
            if path == "/api/state":
                return api.state()
            if path == "/api/events":
                return api.events(int((query.get("since") or ["0"])[0] or 0))
            if path == "/api/agents":
                return api.agents()
            if path == "/api/config":
                return api.config_view()
            if path == "/api/speaker":
                return api.speaker_view()
            if path == "/api/memory":
                return api.memory((query.get("q") or [""])[0])
            if path == "/api/agents/config":
                return api.agents_config()
            if path == "/api/profile":
                return api.profile_list()
            if path == "/api/profile/file":
                return api.profile_read((query.get("name") or [""])[0])
            if path == "/api/mcp":
                return api.mcp_view()
            if path == "/api/models":
                return api.models_view()
            if path == "/api/wake":
                return api.wake_view()
            if path == "/api/secrets":
                return api.secrets_view()
            if path == "/api/log":
                return api.log_view(
                    int((query.get("cursor") or ["0"])[0] or 0),
                    int((query.get("limit") or ["200"])[0] or 200),
                )
        elif method == "POST":
            payload = body if isinstance(body, dict) else {}
            if path == "/api/text":
                return api.text(str(payload.get("text", "")))
            if path == "/api/speaker/enroll":
                return api.enroll(str(payload.get("name", "")), list(payload.get("clips") or ()))
            if path == "/api/speaker/remove":
                return api.remove_speaker(str(payload.get("name", "")))
            if path == "/api/mic/start":
                return api.mic_start()
            if path == "/api/mic/stop":
                return api.mic_stop()
            if path == "/api/test/tts":
                return api.test_tts(
                    str(payload.get("text", "")), play=bool(payload.get("play", True))
                )
            if path == "/api/test/asr":
                return api.test_asr(str(payload.get("clip", "")))
            if path == "/api/test/kws":
                return api.test_kws(str(payload.get("clip", "")))
            if path == "/api/test/speaker":
                return api.test_speaker(str(payload.get("clip", "")))
            if path == "/api/test/agent":
                return api.test_agent(str(payload.get("agent", "")), str(payload.get("text", "")))
            if path == "/api/test/tool":
                return api.test_tool(
                    str(payload.get("tool", "")), dict(payload.get("arguments") or {})
                )
            if path == "/api/profile/save":
                return api.profile_save(str(payload.get("name", "")), str(payload.get("text", "")))
            if path == "/api/profile/delete":
                return api.profile_delete(str(payload.get("name", "")))
            if path == "/api/profile/sync":
                return api.profile_sync(prune=bool(payload.get("prune", False)))
            if path == "/api/models/probe":
                return api.models_probe(
                    str(payload.get("kind", "")),
                    str(payload.get("provider", "")),
                    str(payload.get("base", "")),
                )
            if path == "/api/models/fetch":
                return api.models_fetch(
                    str(payload.get("kind", "")),
                    str(payload.get("provider", "")),
                    str(payload.get("base", "")),
                    str(payload.get("key_env", "")),
                    str(payload.get("proto", "")),
                )
            if path == "/api/models/try":
                return api.models_try(
                    str(payload.get("kind", "")),
                    str(payload.get("provider", "")),
                    str(payload.get("base", "")),
                    str(payload.get("key_env", "")),
                    str(payload.get("proto", "")),
                    str(payload.get("model", "")),
                )
            if path == "/api/secrets":
                return api.secret_set(
                    str(payload.get("name", "")),
                    str(payload.get("value", "")),
                    bool(payload.get("remember", False)),
                )
            if path == "/api/secrets/clear":
                return api.secret_clear(str(payload.get("name", "")))
            if path == "/api/restart":
                return api.restart()
            if path == "/api/log/clear":
                return api.log_clear()
        elif method == "PUT":
            payload = body if isinstance(body, dict) else {}
            if path == "/api/config":
                return api.config_update(
                    str(payload.get("file", "")), dict(payload.get("updates") or {})
                )
            if path == "/api/agents/config":
                return api.agents_update(dict(payload.get("updates") or {}))
            if path == "/api/mcp":
                return api.mcp_update(dict(payload.get("updates") or {}))
            if path == "/api/models":
                return api.models_update(
                    str(payload.get("profile", "")),
                    str(payload.get("kind", "")),
                    dict(payload.get("fields") or {}),
                    str(payload.get("label", "")),
                )
            if path == "/api/wake":
                return api.wake_update(list(payload.get("words") or ()))
        raise ApiError(f"no such endpoint: {method} {path}", status=404)


def _make_handler(server: ConsoleServer):
    """A request handler bound to one ``ConsoleServer``.

    A closure rather than a class attribute so two consoles in one process (which
    the tests do) cannot see each other's token.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "vox-console"
        #: HTTP/1.1 so the browser reuses one connection for the polling loop.
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            """Silence. The default logs every URL to stderr, and URLs here carry
            the token in a query string."""

        # -- helpers -------------------------------------------------------

        def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            body = (
                payload
                if isinstance(payload, bytes)
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Nothing here is meant for another origin, and the page is same-origin
            # by construction. No CORS headers: a console that answers cross-origin
            # requests is a console any web page could drive.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0:
                return None
            if length > MAX_BODY_BYTES:
                raise ApiError("request body is too large", status=413)
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(f"body is not JSON: {type(exc).__name__}") from exc

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not server.authorised(self.headers.get("Authorization"), query):
                # 401 for everything, including the page: there is no unauthenticated
                # surface to probe for endpoint names.
                self._send(401, {"error": "a console token is required"})
                return
            if method == "GET" and parsed.path in {"/", "/index.html"}:
                self._send(200, server.page(), "text/html; charset=utf-8")
                return
            try:
                body = self._read_body() if method in {"POST", "PUT"} else None
                self._send(200, server.dispatch(method, parsed.path, query, body))
            except ApiError as exc:
                self._send(exc.status, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - never leak a traceback
                # The type name only. Exception text here can carry file paths,
                # command text or provider details.
                self._send(500, {"error": f"console failed: {type(exc).__name__}"})

        # -- verbs ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

    return Handler


__all__ = ["MAX_BODY_BYTES", "STATIC_DIR", "ConsoleError", "ConsoleServer", "loopback_problem"]
