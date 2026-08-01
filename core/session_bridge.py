from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class BridgeError(RuntimeError):
    pass


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

    @classmethod
    def from_env(cls) -> "LocalEvoXTransport":
        return cls(
            os.getenv("EVOX_VOICE_BRIDGE_URL", "http://localhost:8765"),
            os.getenv("EVOX_VOICE_BRIDGE_TOKEN", ""),
        )

    def _validate(self) -> None:
        if not self.token:
            raise BridgeError("EVOX_VOICE_BRIDGE_TOKEN is required")
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
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BridgeError(f"EvoX conversation bridge failed: {exc}") from exc
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
