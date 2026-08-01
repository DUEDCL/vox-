"""``web.search`` -- titles, URLs and snippets only.

No search provider ships with the platform. That is a decision, not an omission:
every hosted search API is a cloud dependency with a key, and red line 1 says the
default install talks to nobody. A backend is injected by whoever configures one,
and until then the tool reports itself unavailable instead of pretending.

What the tool does own is the shape of the result. Full page text is never
returned, so a page cannot inject instructions into the model's context by being
searched -- the snippet the provider already wrote is the whole payload, capped.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from .contract import ToolRequest, ToolResult
from .policy import load_tools_config, refuse

#: A backend takes a query and a result cap, and returns mappings with any of
#: ``title`` / ``url`` / ``snippet``. Anything else it returns is dropped here.
SearchBackend = Callable[[str, int], Sequence[Mapping[str, Any]]]


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold()
    except ValueError:
        return ""


def is_blocked(url: str, blocked: Sequence[str]) -> bool:
    """Suffix match, so one entry covers a domain and all of its subdomains."""
    host = host_of(url)
    if not host:
        return True
    for entry in blocked:
        needle = entry.strip().casefold().lstrip(".")
        if needle and (host == needle or host.endswith("." + needle)):
            return True
    return False


class WebSearchTool:
    """Normalise a search backend's output down to what is safe to inject."""

    name = "web.search"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        backend: SearchBackend | None = None,
    ) -> None:
        self.config = dict(config) if config is not None else load_tools_config()
        self.settings = dict(self.config.get("web", {}))
        self.backend = backend

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {"query": "str", "limit": "int, optional"},
            "max_results": int(self.settings.get("max_results", 5)),
            "snippet_chars": int(self.settings.get("snippet_chars", 280)),
            "backend_configured": self.backend is not None,
            "returns": "title, url, snippet -- never page text",
        }

    def run(self, request: ToolRequest) -> ToolResult:
        query = request.arguments.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return refuse(self.name, "query is required")
        if self.backend is None:
            return refuse(self.name, "no search backend is configured")
        cap = int(self.settings.get("max_results", 5))
        try:
            limit = int(request.arguments.get("limit", cap))
        except (TypeError, ValueError):
            limit = cap
        limit = max(1, min(limit, cap))
        try:
            raw = self.backend(query.strip(), limit)
        except Exception as exc:  # a backend is third-party code
            return refuse(self.name, f"search backend failed: {type(exc).__name__}")
        hits = self._normalise(raw, limit)
        lines = [f"{i}. {h['title']} — {h['url']}\n   {h['snippet']}".rstrip()
                 for i, h in enumerate(hits, start=1)]
        return ToolResult(
            tool=self.name,
            ok=True,
            output="\n".join(lines),
            audit={
                "decision": "executed",
                "hits": len(hits),
                "blocked": max(0, len(list(raw)) - len(hits)) if raw else 0,
                "hosts": sorted({host_of(h["url"]) for h in hits}),
            },
        )

    def _normalise(
        self, raw: Sequence[Mapping[str, Any]], limit: int
    ) -> list[dict[str, str]]:
        blocked = self.settings.get("blocked_domains", ())
        chars = int(self.settings.get("snippet_chars", 280))
        out: list[dict[str, str]] = []
        for item in raw or ():
            url = str(item.get("url", "")).strip()
            if not url or is_blocked(url, blocked):
                continue
            out.append(
                {
                    "title": str(item.get("title", "")).strip()[:200],
                    "url": url,
                    "snippet": " ".join(str(item.get("snippet", "")).split())[:chars],
                }
            )
            if len(out) >= limit:
                break
        return out
