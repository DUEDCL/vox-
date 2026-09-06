from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class BridgeError(RuntimeError):
    pass


#: Failures that prove the request never reached the wire. Deliberately
#: narrow: a reset mid-send or a timeout leaves the executed-or-not state
#: unknown, and only certainty justifies an automatic re-send.
_CONNECT_PHASE_ERRORS = (ConnectionRefusedError, socket.gaierror)


def _connect_phase_failure(exc: BaseException) -> bool:
    """Whether ``exc`` proves the request failed before anything was sent.

    ``urlopen`` wraps OS-level connect failures as ``URLError`` with the
    original OSError attached as ``reason``, so both layers are checked.
    """
    candidates = [exc]
    reason = getattr(exc, "reason", None)
    if reason is not None:
        candidates.append(reason)
    return any(isinstance(candidate, _CONNECT_PHASE_ERRORS) for candidate in candidates)


class ConversationTransport(Protocol):
    def send(self, text: str, *, session_id: str | None = None) -> dict[str, Any]: ...
    def cancel(self, turn_id: str) -> dict[str, Any]: ...


@dataclass
class LocalEvoXTransport:
    """Authenticated localhost HTTP bridge.

    The endpoint is deliberately configurable because EvoX does not expose a
    stable conversation endpoint to this runtime. Plain HTTP is accepted only
    for loopback addresses and always requires a bearer token.
    """

    base_url: str
    token: str
    timeout: float = 30.0
    #: Connect-phase retry budget. The default of 1 keeps the historical
    #: single-attempt behaviour: retries fire only when the request provably
    #: never left this process (connection refused, name resolution). A
    #: timeout or an HTTP status means the endpoint may already have executed
    #: the turn -- an automatic re-send there could run it twice.
    attempts: int = 1
    retry_backoff_s: float = 0.5

    @classmethod
    def from_env(cls) -> "LocalEvoXTransport":
        return cls(
            os.getenv("VOX_VOICE_BRIDGE_URL", "http://localhost:8765"),
            os.getenv("VOX_VOICE_BRIDGE_TOKEN", ""),
            attempts=int(os.getenv("VOX_VOICE_BRIDGE_ATTEMPTS", "1")),
        )

    def _validate(self) -> None:
        if not self.token:
            raise BridgeError("VOX_VOICE_BRIDGE_TOKEN is required")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BridgeError("bridge URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise BridgeError("bridge URL must not contain credentials")
        if parsed.scheme == "https":
            return
        host = parsed.hostname.lower()
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise BridgeError("plain HTTP bridge URL must use a loopback address")

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate()
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url.rstrip("/") + path,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                budget = max(1, self.attempts)
                if attempt >= budget or not _connect_phase_failure(exc):
                    raise BridgeError(f"EvoX conversation bridge failed: {exc}") from exc
                time.sleep(self.retry_backoff_s)
        if not isinstance(data, dict):
            raise BridgeError("EvoX conversation bridge returned a non-object response")
        return data

    def send(self, text: str, *, session_id: str | None = None) -> dict[str, Any]:
        if not text.strip():
            raise BridgeError("conversation text cannot be empty")
        result = self._request("/v1/conversation/turns", {"text": text, "session_id": session_id})
        if not isinstance(result.get("turn_id"), str) or not result["turn_id"]:
            raise BridgeError("EvoX conversation bridge response is missing turn_id")
        return result

    def cancel(self, turn_id: str) -> dict[str, Any]:
        if not turn_id:
            raise BridgeError("turn_id is required")
        encoded_turn_id = quote(turn_id, safe="")
        return self._request(f"/v1/conversation/turns/{encoded_turn_id}/cancel", {})
