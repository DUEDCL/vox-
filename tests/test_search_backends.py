"""Search backends: what is refused, what is extracted, what stays off.

No test here touches the network. The backends take an ``opener`` for exactly that
reason -- a test that reaches DuckDuckGo would be measuring somebody else's uptime
and would fail on a machine with no route out.

Evidence level: AUTO. A real query against a real SearxNG instance or the real
DDG endpoint is a separate, human-run check (recorded in prototype-results).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tools import WebSearchTool, open_tools
from core.tools.contract import ToolRequest
from core.tools.search_backends import (
    DuckDuckGoBackend,
    SearchBackendError,
    SearxBackend,
    endpoint_problem,
    open_search_backend,
)


def fake_opener(body: str, *, record: list[str] | None = None):
    """An ``urlopen`` stand-in that returns ``body`` and records the URL asked for."""

    @contextmanager
    def opener(request, timeout=None):
        del timeout
        if record is not None:
            record.append(request.full_url)

        class Response:
            @staticmethod
            def read() -> bytes:
                return body.encode("utf-8")

        yield Response()

    return opener


def exploding_opener(exc: Exception):
    @contextmanager
    def opener(request, timeout=None):
        del request, timeout
        raise exc
        yield  # pragma: no cover - unreachable, keeps this a generator

    return opener


# ------------------------------------------------------------------- endpoints


@pytest.mark.parametrize(
    "url",
    [
        "https://searx.example.com",
        "http://localhost:8888",
        "http://127.0.0.1:8888",
        "http://[::1]:8888",
    ],
)
def test_acceptable_endpoints(url):
    assert endpoint_problem(url) is None


def test_plain_http_off_loopback_is_refused():
    assert "loopback" in endpoint_problem("http://searx.example.com")


def test_credentials_in_the_url_are_refused():
    """Not masked, refused: a URL with a password in it ends up in logs."""
    assert "credentials" in endpoint_problem("https://user:pass@searx.example.com")


@pytest.mark.parametrize("url", ["searx.example.com", "ftp://searx.example.com", "", "http://"])
def test_non_http_urls_are_refused(url):
    assert "absolute HTTP(S)" in endpoint_problem(url)


def test_a_bad_endpoint_fails_at_construction():
    with pytest.raises(SearchBackendError, match="loopback"):
        SearxBackend("http://searx.example.com")


# ---------------------------------------------------------------------- searx


SEARX_BODY = json.dumps(
    {
        "results": [
            {"title": "Zipformer", "url": "https://example.com/a", "content": "一段摘要"},
            {"title": "Silero", "url": "https://example.com/b", "content": "另一段"},
            {"title": "Third", "url": "https://example.com/c", "content": "第三段"},
        ]
    }
)


def test_searx_maps_its_fields_to_the_tools_shape():
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener(SEARX_BODY))
    hits = backend("zipformer", 5)
    assert hits[0] == {
        "title": "Zipformer",
        "url": "https://example.com/a",
        # SearxNG calls the snippet "content"; the tool expects "snippet".
        "snippet": "一段摘要",
    }


def test_searx_honours_the_limit():
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener(SEARX_BODY))
    assert len(backend("zipformer", 2)) == 2


def test_searx_asks_for_json_and_url_encodes_the_query():
    asked: list[str] = []
    backend = SearxBackend(
        "http://127.0.0.1:8888/", opener=fake_opener(SEARX_BODY, record=asked)
    )
    backend("中文 查询", 3)
    assert "format=json" in asked[0]
    assert "%E4%B8%AD%E6%96%87" in asked[0]
    assert "//127.0.0.1:8888/search?" in asked[0], "no double slash from the trailing /"


def test_searx_without_json_enabled_is_a_failure_not_an_empty_result():
    """A 403 HTML error page must not read as "no matches for your query"."""
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener("<html>403</html>"))
    with pytest.raises(SearchBackendError, match="non-JSON"):
        backend("zipformer", 5)


def test_searx_json_without_results_is_a_failure():
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener('{"query": "x"}'))
    with pytest.raises(SearchBackendError, match="no results array"):
        backend("zipformer", 5)


def test_searx_skips_non_object_entries():
    body = json.dumps({"results": ["junk", {"title": "T", "url": "https://e.com", "content": "s"}]})
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener(body))
    assert [hit["title"] for hit in backend("q", 5)] == ["T"]


# ----------------------------------------------------------------- duckduckgo


DDG_BODY = """
<html><body>
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">
    Zipformer &amp; friends
  </a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
    A local wake word model.
  </a>
</div>
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="https://example.com/b">Direct link</a>
  <a class="result__snippet" href="https://example.com/b">Second snippet</a>
</div>
</body></html>
"""


def test_ddg_unwraps_the_redirect_and_decodes_entities():
    """The wrapper URL is a duckduckgo.com host, so leaving it in place would both
    mislead the user and slip past ``blocked_domains``, which matches on host."""
    backend = DuckDuckGoBackend(opener=fake_opener(DDG_BODY))
    hits = backend("zipformer", 5)
    assert hits[0]["url"] == "https://example.com/a"
    assert hits[0]["title"] == "Zipformer & friends"
    assert hits[0]["snippet"] == "A local wake word model."


def test_ddg_accepts_a_direct_href_too():
    backend = DuckDuckGoBackend(opener=fake_opener(DDG_BODY))
    assert backend("zipformer", 5)[1]["url"] == "https://example.com/b"


def test_ddg_honours_the_limit():
    backend = DuckDuckGoBackend(opener=fake_opener(DDG_BODY))
    assert len(backend("zipformer", 1)) == 1


def test_ddg_drops_hits_with_no_usable_url():
    body = '<a class="result__a" href="javascript:void(0)">Nope</a>'
    backend = DuckDuckGoBackend(opener=fake_opener(f"<html>result{body}</html>"))
    assert backend("q", 5) == []


def test_ddg_reports_an_unrecognised_page_rather_than_no_matches():
    """The layout will change some day. When it does this must be visible as a
    backend failure, not as a query that found nothing."""
    backend = DuckDuckGoBackend(opener=fake_opener("<html><body>captcha</body></html>"))
    with pytest.raises(SearchBackendError, match="unrecognised page"):
        backend("zipformer", 5)


def test_ddg_returns_empty_when_the_page_says_there_are_no_results():
    """A real "no results" page still says "result" somewhere; that is the
    difference between an empty answer and a broken parser."""
    backend = DuckDuckGoBackend(opener=fake_opener("<html>No results found.</html>"))
    assert backend("asdkjhasd", 5) == []


def test_ddg_network_failure_propagates_as_an_exception():
    """``WebSearchTool.run`` catches this and refuses with a type name only."""
    backend = DuckDuckGoBackend(opener=exploding_opener(TimeoutError("timed out")))
    with pytest.raises(TimeoutError):
        backend("q", 5)


# -------------------------------------------------------------------- selection


def test_nothing_configured_means_no_backend():
    """The factory posture: the default install talks to nobody."""
    assert open_search_backend({"web": {"enabled": True}}) is None


def test_the_shipped_defaults_configure_no_backend():
    """出厂姿态：默认装出来的东西不跟任何人说话。

    读的是 ``load_tools_config()`` 的**默认值**，不是本机那份 ``config/tools.toml`` ——
    后者是给人改的，而 ``allow_internet = true`` 是一个正常的、有意的本机决定。读文件的
    版本会在任何人打开搜索之后变红，而那不是「有人偷偷改了出厂默认」的信号。

    真正要守的是：默认值里没有后端。这一条由 ``core/tools/policy.py`` 的 ``_DEFAULTS``
    决定，改它才是要被看见的事。
    """
    from core.tools.policy import load_tools_config

    defaults = load_tools_config(Path("/nonexistent-so-defaults-are-used.toml"))
    assert open_search_backend(defaults) is None


def test_searx_wins_when_configured():
    backend = open_search_backend(
        {"web": {"enabled": True, "searx_url": "http://127.0.0.1:8888", "allow_internet": True}}
    )
    assert isinstance(backend, SearxBackend)


def test_a_broken_searx_url_does_not_fall_through_to_the_internet():
    """Using a different backend than the one configured would be the wrong kind
    of helpful: the operator asked for local-only and would not be told."""
    backend = open_search_backend(
        {"web": {"enabled": True, "searx_url": "http://searx.example.com", "allow_internet": True}}
    )
    assert backend is None


def test_the_internet_fallback_needs_its_own_switch():
    assert open_search_backend({"web": {"enabled": True, "allow_internet": False}}) is None
    backend = open_search_backend({"web": {"enabled": True, "allow_internet": True}})
    assert isinstance(backend, DuckDuckGoBackend)


def test_a_disabled_web_tool_gets_no_backend():
    config = {"web": {"enabled": False, "searx_url": "http://127.0.0.1:8888"}}
    assert open_search_backend(config) is None


def test_timeout_reaches_the_backend():
    backend = open_search_backend(
        {"web": {"enabled": True, "allow_internet": True, "timeout_s": 3}}
    )
    assert backend.timeout_s == 3.0


# ------------------------------------------------------- through the tool gate


def base_config() -> dict:
    return {
        "web": {
            "enabled": True,
            "blocked_domains": ["blocked.example"],
            "max_results": 5,
            "snippet_chars": 20,
            "searx_url": "",
            "allow_internet": False,
            "timeout_s": 8,
        }
    }


def run_search(backend, query="zipformer"):
    tool = WebSearchTool(base_config(), backend=backend)
    return tool.run(ToolRequest(tool="web.search", arguments={"query": query}, origin="voice"))


def test_a_configured_backend_reaches_the_tool():
    """``open_tools`` used to accept a backend and never be given one, so a
    configured SearxNG instance could not reach ``web.search`` at all."""
    runner = open_tools(
        {
            **base_config(),
            "fs": {"enabled": True, "roots": ["."], "max_bytes": 1024, "denied_names": [], "denied_dirs": []},
            "shell": {"enabled": False},
        }
    )
    described = next(d for d in (t.describe() for t in runner.tools.values()) if d["name"] == "web.search")
    assert described["backend_configured"] is False, "nothing configured in this fixture"

    runner = open_tools(
        {
            **base_config(),
            "web": {**base_config()["web"], "searx_url": "http://127.0.0.1:8888"},
            "fs": {"enabled": True, "roots": ["."], "max_bytes": 1024, "denied_names": [], "denied_dirs": []},
            "shell": {"enabled": False},
        }
    )
    described = next(d for d in (t.describe() for t in runner.tools.values()) if d["name"] == "web.search")
    assert described["backend_configured"] is True


def test_blocked_domains_still_filter_a_real_backend():
    body = json.dumps(
        {
            "results": [
                {"title": "Bad", "url": "https://sub.blocked.example/x", "content": "s"},
                {"title": "Good", "url": "https://example.com/y", "content": "s"},
            ]
        }
    )
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener(body))
    result = run_search(backend)
    assert result.ok is True
    assert "blocked.example" not in result.output
    assert "Good" in result.output


def test_snippets_are_capped_so_a_page_cannot_inject_a_wall_of_text():
    body = json.dumps(
        {"results": [{"title": "T", "url": "https://example.com", "content": "x" * 500}]}
    )
    backend = SearxBackend("http://127.0.0.1:8888", opener=fake_opener(body))
    result = run_search(backend)
    assert "x" * 21 not in result.output


def test_a_failing_backend_refuses_with_a_type_name_only():
    """No backend message in the refusal: it can carry a URL or a local path."""
    backend = SearxBackend(
        "http://127.0.0.1:8888", opener=exploding_opener(TimeoutError("connect to 10.1.2.3 timed out"))
    )
    result = run_search(backend)
    assert result.ok is False
    assert result.error == "search backend failed: TimeoutError"
    assert "10.1.2.3" not in repr(result)
