"""Search backends for ``web.search``. Both off by default.

``core/tools/web.py`` ships no backend and says why: every hosted search API is a
cloud dependency with a key, and the default install talks to nobody. That
sentence has two load-bearing words -- *hosted* and *key* -- and this module is
what fits inside them:

- ``SearxBackend`` talks to a **SearxNG instance you run yourself**, on loopback.
  No key, no third party, no cloud. This is the one that keeps the default posture
  intact; it just needs you to have a service running.
- ``DuckDuckGoBackend`` scrapes the keyless HTML endpoint. It *is* a network call
  to somebody else's server, so it is behind an explicit ``allow_internet``
  switch that ships ``false``. Turning it on is a decision with a name.

With neither configured ``open_search_backend`` returns ``None`` and the tool goes
on reporting ``no search backend is configured`` -- the factory behaviour does not
change by one byte.

Page text is never returned by either backend. That rule lives in ``web.py`` and
is repeated here in the extraction: a searched page must not be able to inject
instructions into the model's context, so only the snippet its own author wrote
comes back, and only up to the configured cap.
"""

from __future__ import annotations

import ipaddress
import json
import re
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen

from .policy import load_tools_config

#: What a backend hands back to ``WebSearchTool._normalise``.
Hit = dict[str, str]

DDG_ENDPOINT = "https://html.duckduckgo.com/html/"

#: A browser-shaped UA. The endpoint serves a JS-free page either way; sending a
#: plausible one avoids being handed the "please enable JavaScript" variant.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


class SearchBackendError(RuntimeError):
    """A backend that cannot be trusted to reach what it claims to reach."""


def endpoint_problem(url: str) -> str | None:
    """``None`` when ``url`` is safe to POST/GET a query to, else the reason.

    This is the same rule ``core/session_bridge.py`` and ``core/agents/http.py``
    enforce -- absolute HTTP(S), no embedded credentials, plain HTTP only on
    loopback -- and it is deliberately a **third copy** rather than a refactor of
    those two. Both of them are security boundaries with their own exception types
    and pinned error messages; extracting a shared helper would mean editing two
    tested security modules to add a feature to a third. That extraction is worth
    doing on its own, with its own tests, and is recorded in the backlog.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "search endpoint must be an absolute HTTP(S) URL"
    if parsed.username or parsed.password:
        return "search endpoint must not contain credentials"
    if parsed.scheme == "https":
        return None
    host = parsed.hostname.lower()
    if host == "localhost":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return "plain HTTP search endpoint must use a loopback address"


def _fetch(url: str, *, timeout: float, opener: Any) -> str:
    """One GET, decoded as UTF-8. ``opener`` is how tests avoid the network."""
    request = Request(url, headers={"User-Agent": _UA, "Accept": "text/html,application/json"})
    with (opener or urlopen)(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


class SearxBackend:
    """A self-hosted SearxNG instance. Zero cloud, zero keys.

    The JSON API has to be enabled in the instance's ``settings.yml``
    (``search.formats`` must include ``json``); when it is not, SearxNG answers
    403 and this reports a backend failure rather than silently returning nothing.
    """

    name = "searx"

    def __init__(self, url: str, *, timeout_s: float = 8.0, opener: Any = None) -> None:
        problem = endpoint_problem(url)
        if problem:
            raise SearchBackendError(problem)
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.opener = opener

    def __call__(self, query: str, limit: int) -> list[Hit]:
        target = f"{self.url}/search?q={quote_plus(query)}&format=json&safesearch=1"
        raw = _fetch(target, timeout=self.timeout_s, opener=self.opener)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SearchBackendError(f"searx returned non-JSON: {exc}") from exc
        results = payload.get("results")
        if not isinstance(results, list):
            raise SearchBackendError("searx response has no results array")
        hits: list[Hit] = []
        for item in results[: max(1, limit)]:
            if not isinstance(item, Mapping):
                continue
            hits.append(
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("url", "")),
                    # ``content`` is SearxNG's field name for the snippet.
                    "snippet": str(item.get("content", "")),
                }
            )
        return hits


class _DdgParser(HTMLParser):
    """Pull title / url / snippet out of DuckDuckGo's JS-free result page.

    An HTML parser rather than a regex, because the input is somebody else's
    markup: a parser degrades to "found nothing" on a page it does not recognise,
    while a regex can match across element boundaries and invent a hit.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[Hit] = []
        self._mode: str | None = None
        self._buffer: list[str] = []

    @staticmethod
    def _unwrap(href: str) -> str:
        """DDG wraps outbound links as ``/l/?uddg=<encoded>``. Unwrap or drop.

        Returning the wrapper would put a duckduckgo.com URL in front of the user
        and defeat ``blocked_domains``, which matches on the host.
        """
        if not href:
            return ""
        parsed = urlparse(href)
        if "uddg" in (parsed.query or ""):
            values = parse_qs(parsed.query).get("uddg") or []
            return values[0] if values else ""
        if parsed.scheme in {"http", "https"}:
            return href
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = dict(attrs).get("class") or ""
        if "result__a" in classes:
            url = self._unwrap(dict(attrs).get("href") or "")
            self.hits.append({"title": "", "url": url, "snippet": ""})
            self._mode, self._buffer = "title", []
        elif "result__snippet" in classes and self.hits:
            self._mode, self._buffer = "snippet", []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._mode is None or not self.hits:
            return
        self.hits[-1][self._mode] = " ".join("".join(self._buffer).split())
        self._mode, self._buffer = None, []

    def handle_data(self, data: str) -> None:
        if self._mode is not None:
            self._buffer.append(data)


class DuckDuckGoBackend:
    """The keyless HTML endpoint. A real network call, hence opt-in.

    No API key exists to leak and no account is involved, which is why this is
    reachable at all under red line 1. What it is not is local: enabling it means
    every ``web.search`` query leaves the machine. ``allow_internet`` is the switch
    that says so out loud.
    """

    name = "duckduckgo"

    def __init__(self, *, timeout_s: float = 8.0, opener: Any = None) -> None:
        self.timeout_s = timeout_s
        self.opener = opener

    def __call__(self, query: str, limit: int) -> list[Hit]:
        target = f"{DDG_ENDPOINT}?q={quote_plus(query)}&kl=wt-wt"
        body = _fetch(target, timeout=self.timeout_s, opener=self.opener)
        parser = _DdgParser()
        parser.feed(body)
        parser.close()
        hits = [hit for hit in parser.hits if hit["url"]]
        if not hits and re.search(r"(?i)result", body) is None:
            # Neither results nor anything that looks like a result page: report a
            # failure instead of "no matches", so a changed layout is visible.
            raise SearchBackendError("duckduckgo returned an unrecognised page")
        return hits[: max(1, limit)]


def open_search_backend(
    config: Mapping[str, Any] | None = None,
) -> Callable[[str, int], Sequence[Hit]] | None:
    """The configured backend, or ``None`` for "the tool stays unavailable".

    Order is deliberate: a self-hosted instance wins whenever one is configured,
    because it is the option that keeps every query on this machine. The internet
    fallback is only reached when there is no local instance *and* somebody set
    ``allow_internet = true``.

    A misconfigured ``searx_url`` returns ``None`` rather than falling through to
    the internet. Silently using a different backend than the operator configured
    would be the wrong kind of helpful.
    """
    resolved = dict(config) if config is not None else load_tools_config()
    web = dict(resolved.get("web", {}))
    if not web.get("enabled", True):
        return None
    timeout = float(web.get("timeout_s", 8))
    url = str(web.get("searx_url", "")).strip()
    if url:
        try:
            return SearxBackend(url, timeout_s=timeout)
        except SearchBackendError:
            return None
    if web.get("allow_internet", False):
        return DuckDuckGoBackend(timeout_s=timeout)
    return None


__all__ = [
    "DDG_ENDPOINT",
    "DuckDuckGoBackend",
    "SearchBackendError",
    "SearxBackend",
    "endpoint_problem",
    "open_search_backend",
]
