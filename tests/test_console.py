"""The console: loopback, token, and what must never appear in a response.

This module opens a listening socket, so the tests that matter most are the ones
about who can reach it and what leaks through it. The API-shape tests come after.

Evidence level: AUTO. A rendered page is SIM (screenshot), and speaking into the
microphone it can start is REAL-MIC.
"""

from __future__ import annotations

import base64
import io
import json
import struct
import urllib.error
import urllib.request
import wave
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.console import routes
from core.console.audio import (
    MAX_WAV_BYTES,
    AudioDecodeError,
    decode_wav_base64,
    quality,
)
from core.console.routes import AGENT_EDITABLE, EDITABLE, ApiError, ConsoleApi
from core.console.server import ConsoleError, ConsoleServer, loopback_problem
from core.state import VoiceState
from vox_plugin.runtime import VoiceRuntime


def wav_base64(
    seconds: float = 1.0, *, rate: int = 16000, channels: int = 1, width: int = 2, value: float = 0.3
) -> str:
    """A WAV clip as the browser would post it."""
    frames = int(seconds * rate)
    samples = np.full(frames * channels, value, dtype=np.float32)
    raw = (samples * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        if width == 2:
            handle.writeframes(raw)
        else:
            handle.writeframes(bytes(len(raw) // 2))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture
def runtime():
    rt = VoiceRuntime(with_desktop=False, with_memory=False)
    rt._started = True
    rt.plugin.machine.state = VoiceState.IDLE
    yield rt
    rt.close()


@pytest.fixture
def api(runtime):
    return ConsoleApi(runtime)


# ---------------------------------------------------------------------- audio


def test_a_clean_clip_decodes_to_float_samples():
    samples = decode_wav_base64(wav_base64(1.0))
    assert samples.dtype == np.float32
    assert samples.size == 16000
    assert -1.0 <= float(samples.max()) <= 1.0


def test_a_data_url_prefix_is_tolerated():
    payload = "data:audio/wav;base64," + wav_base64(0.5)
    assert decode_wav_base64(payload).size == 8000


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"rate": 44100}, "16000 Hz"),
        ({"channels": 2}, "mono"),
        ({"width": 1}, "16-bit"),
    ],
)
def test_audio_that_would_silently_mismatch_the_model_is_refused(kwargs, message):
    """Each rejection names the value found. A clip that was silently resampled
    would produce a voiceprint that stops matching weeks later."""
    with pytest.raises(AudioDecodeError, match=message):
        decode_wav_base64(wav_base64(0.5, **kwargs))


def test_empty_and_garbage_payloads_are_refused():
    with pytest.raises(AudioDecodeError, match="no audio"):
        decode_wav_base64("")
    with pytest.raises(AudioDecodeError, match="not valid base64"):
        decode_wav_base64("not base64 at all!!")
    with pytest.raises(AudioDecodeError, match="not a readable WAV"):
        decode_wav_base64(base64.b64encode(b"RIFFnope").decode())


def test_an_oversized_clip_is_refused_before_it_is_parsed():
    payload = base64.b64encode(b"\0" * (MAX_WAV_BYTES + 10)).decode()
    with pytest.raises(AudioDecodeError, match="too long"):
        decode_wav_base64(payload)


def test_quality_reports_the_three_numbers_the_gate_uses():
    measured = quality(decode_wav_base64(wav_base64(2.0, value=0.5)))
    assert measured["duration_s"] == 2.0
    assert 0.4 < measured["rms"] < 0.6
    assert measured["clip_ratio"] == 0.0


def test_clipping_is_measured():
    measured = quality(decode_wav_base64(wav_base64(0.5, value=1.0)))
    assert measured["clip_ratio"] > 0.9


# ------------------------------------------------------------------- loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_hosts_are_accepted(host):
    assert loopback_problem(host) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_non_loopback_hosts_are_refused(host):
    assert loopback_problem(host) is not None


def test_the_server_refuses_to_construct_off_loopback(api):
    with pytest.raises(ConsoleError, match="loopback"):
        ConsoleServer(api, host="0.0.0.0")


# ---------------------------------------------------------------------- token


@pytest.fixture
def live(api):
    """A running console on an OS-assigned port, with a token."""
    server = ConsoleServer(api, port=0)
    server.start()
    yield server
    server.stop()


def fetch(server: ConsoleServer, path: str, *, token: str | None = "auto", method="GET", body=None):
    """Returns ``(status, payload)``; a 4xx/5xx does not raise."""
    url = f"http://{server.host}:{server.port}{path}"
    headers = {}
    if token == "auto":
        headers["Authorization"] = f"Bearer {server.token}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return response.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_a_token_is_generated_and_carried_in_the_url(live):
    assert len(live.token) > 20
    assert f"?t={live.token}" in live.url


def test_no_token_means_401_even_for_the_page(live):
    """There is no unauthenticated surface to probe for endpoint names."""
    assert fetch(live, "/", token=None)[0] == 401
    assert fetch(live, "/api/state", token=None)[0] == 401


def test_a_wrong_token_is_401(live):
    assert fetch(live, "/api/state", token="wrong-token-entirely")[0] == 401


def test_a_bearer_header_authorises(live):
    status, body = fetch(live, "/api/state")
    assert status == 200
    assert json.loads(body)["state"] == "idle"


def test_a_query_string_token_authorises(live):
    status, _ = fetch(live, f"/api/state?t={live.token}", token=None)
    assert status == 200


def test_the_page_is_served_and_is_self_contained(live):
    """CSS and JS are inlined, which is what lets every request need the token:
    there is no second request that would need an exemption."""
    status, body = fetch(live, "/")
    assert status == 200
    assert "<style>" in body and "<script>" in body
    assert "<link" not in body
    assert 'src="' not in body


def test_describe_never_reports_the_token(live):
    described = live.describe()
    assert live.token not in json.dumps(described)
    assert described["token_required"] is True
    assert described["running"] is True


def test_an_unknown_endpoint_is_404(live):
    status, body = fetch(live, "/api/nope")
    assert status == 404
    assert "no such endpoint" in json.loads(body)["error"]


def test_a_body_that_is_not_json_is_a_400(live):
    url = f"http://{live.host}:{live.port}/api/text"
    request = urllib.request.Request(
        url,
        data=b"{not json",
        headers={"Authorization": f"Bearer {live.token}", "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 400


def test_no_token_mode_serves_without_one(api):
    server = ConsoleServer(api, port=0, require_token=False)
    server.start()
    try:
        assert server.describe()["token_required"] is False
        assert "?t=" not in server.url
        assert fetch(server, "/api/state", token=None)[0] == 200
    finally:
        server.stop()


def test_stop_is_idempotent(api):
    server = ConsoleServer(api, port=0)
    server.start()
    server.stop()
    server.stop()
    assert server.describe()["running"] is False


# ------------------------------------------------------------------ what leaks


def test_state_reports_counts_and_readiness_but_no_secrets(api):
    state = api.state()
    rendered = json.dumps(state, ensure_ascii=False)
    for forbidden in ("token", "voiceprint", "embedding", "vector", "password"):
        assert forbidden not in rendered.casefold()
    assert state["runtime"]["gate_source"] == "caller"
    assert "uptime_s" in state


def test_state_survives_a_runtime_that_cannot_describe_itself(api):
    def explode():
        raise RuntimeError("nope")

    api.runtime.describe = explode
    assert api.state()["runtime"] == {"error": "RuntimeError"}


def test_a_pending_confirmation_is_announced_without_the_command(api):
    """The orb shows the command (FR-6.13). The console says one is waiting."""
    api.runtime._pending_confirm = {
        "type": "tool.confirm_required",
        "payload": {"tool": "shell.run", "command": "git push --force"},
    }
    state = api.state()
    assert state["pending_confirmation"] == {"tool": "shell.run", "where": "orb"}
    assert "force" not in json.dumps(state)


def test_events_are_returned_from_a_cursor(api):
    api.runtime.seen = [{"type": "a"}, {"type": "b"}, {"type": "c"}]
    assert api.events(0)["events"] == api.runtime.seen
    assert api.events(2)["events"] == [{"type": "c"}]
    assert api.events(99)["events"] == []
    assert api.events(0)["next"] == 3


# ----------------------------------------------------------------- config gate


@pytest.fixture
def config_dir(tmp_path):
    """A copy of the shipped configs, so a test can write to them."""
    source = Path(__file__).resolve().parents[1] / "config"
    target = tmp_path / "config"
    target.mkdir()
    for name in ("voice.toml", "speaker.toml", "tools.toml", "memory.toml", "agents.toml"):
        (target / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_config_view_lists_the_editable_keys(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    files = {entry["file"]: entry for entry in api.config_view()["files"]}
    assert set(files) == set(EDITABLE)
    keys = {key["key"] for key in files["voice.toml"]["keys"]}
    assert "wake.keywords_threshold" in keys
    assert "input.sample_rate" not in keys, "16 kHz is an agreement between three models"


def test_a_security_boundary_cannot_be_changed_from_the_console(runtime, config_dir):
    """The four layers between "a voice said something" and "a command ran" are
    not toggles. Refused before validation: the reason has nothing to do with
    whether the value would parse."""
    api = ConsoleApi(runtime, config_dir=config_dir)
    for key, value in (
        ("shell.enabled", True),
        ("shell.allow", ["git status"]),
        ("fs.roots", ["/"]),
        ("fs.denied_names", []),
    ):
        with pytest.raises(ApiError, match="not editable from the console") as caught:
            api.config_update("tools.toml", {key: value})
        assert caught.value.status == 403


def test_require_verification_is_not_editable_from_the_console(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    with pytest.raises(ApiError, match="not editable"):
        api.config_update("speaker.toml", {"speaker.require_verification": False})
    # And the file is untouched.
    assert "require_verification = true" in (config_dir / "speaker.toml").read_text(encoding="utf-8")


def test_an_allowed_key_is_written_and_keeps_its_comment(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    result = api.config_update("speaker.toml", {"speaker.threshold": 0.62})
    assert result["changed"]["speaker.threshold"]["to"] == "0.62"
    text = (config_dir / "speaker.toml").read_text(encoding="utf-8")
    assert "threshold = 0.62" in text
    assert "余弦相似度阈值" in text, "the reasoning must survive a settings change"


def test_a_value_the_loader_would_reject_never_lands(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    before = (config_dir / "voice.toml").read_text(encoding="utf-8")
    with pytest.raises(ApiError, match="rejected"):
        api.config_update("voice.toml", {"wake.num_threads": "many"})
    assert (config_dir / "voice.toml").read_text(encoding="utf-8") == before


def test_an_unknown_config_file_is_refused(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    with pytest.raises(ApiError, match="not a console-editable config file"):
        api.config_update("passwords.toml", {"a.b": 1})


# ------------------------------------------------------------- agent registry


def test_agent_entries_are_listed_with_their_locks(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    view = api.agents_config()
    assert view["present"] is True
    first = view["entries"][0]
    assert first["name"] == "claude" and first["kind"] == "cli"
    locks = {key["name"]: key["locked"] for key in first["keys"]}
    assert locks["enabled"] is False
    for locked in ("command", "args", "name", "kind"):
        assert locks[locked] is True, f"{locked} must not be editable from a web page"


@pytest.mark.parametrize(
    "key", ["agents[0].command", "agents[0].args", "agents[0].name", "agents[0].kind"]
)
def test_the_keys_that_choose_what_runs_are_refused(runtime, config_dir, key):
    """``command`` from a web page is a remote code execution primitive, and no
    amount of loopback binding changes that."""
    api = ConsoleApi(runtime, config_dir=config_dir)
    with pytest.raises(ApiError, match="not editable from the console") as caught:
        api.agents_update({key: "anything"})
    assert caught.value.status == 403


def test_the_key_that_chooses_where_data_goes_is_refused(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    with pytest.raises(ApiError, match="not editable"):
        api.agents_update({"agents[3].url": "https://exfiltrate.example.com"})


def test_enabling_an_agent_is_allowed(runtime, config_dir):
    api = ConsoleApi(runtime, config_dir=config_dir)
    result = api.agents_update({"agents[1].enabled": True, "agents[1].cost": 2})
    assert set(result["changed"]) == {"agents[1].enabled", "agents[1].cost"}
    text = (config_dir / "agents.toml").read_text(encoding="utf-8")
    assert "enabled = true" in text
    assert "**这里永远不写密钥。**" in text, "the file's own warnings survive an edit"


def test_agent_editable_set_excludes_every_execution_key():
    for forbidden in ("command", "args", "cwd", "env_passthrough", "url", "name", "kind"):
        assert forbidden not in AGENT_EDITABLE


# -------------------------------------------------------------------- models


@pytest.fixture
def models_file(tmp_path, monkeypatch):
    """A writable copy of the shipped ``config/models.toml``, via its own env var."""
    source = Path(__file__).resolve().parents[1] / "config" / "models.toml"
    target = tmp_path / "models.toml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("VOX_MODELS_CONFIG", str(target))
    return target


def test_models_view_reports_the_profiles_and_the_presets(api, models_file):
    view = api.models_view()
    assert view["present"] is True
    assert view["active"] in view["profiles"]
    assert set(view["presets"]) == {"asr", "tts", "llm"}
    slugs = {preset["slug"] for preset in view["presets"]["llm"]}
    assert {"ollama", "deepseek", "custom"} <= slugs


def test_models_view_never_carries_a_key_only_an_env_var_name(api, models_file):
    """The file cannot hold a key, and this asserts the response shape agrees."""
    view = api.models_view()
    blob = json.dumps(view, ensure_ascii=False)
    assert "sk-" not in blob
    for preset in view["presets"]["llm"]:
        assert set(preset) == {
            "slug", "name", "base", "proto", "key_env", "domestic", "local", "note"
        }
        assert preset["key_env"] in {"", "VOX_LLM_KEY"}


def test_a_broken_models_file_says_what_is_wrong_rather_than_showing_nothing(api, models_file):
    models_file.write_text('active = "ghost"\n', encoding="utf-8")
    with pytest.raises(ApiError, match="names no profile") as caught:
        api.models_view()
    assert caught.value.status == 500


def test_a_model_profile_is_written_back_with_its_comments(api, models_file):
    result = api.models_update("local", "llm", {"model": "qwen3:8b"})
    assert result["restart_required"] is True
    text = models_file.read_text(encoding="utf-8")
    assert 'model = "qwen3:8b"' in text
    assert "这里永远不写密钥" in text, "the file's own reasoning survives an edit"


def test_a_new_profile_can_be_created_from_the_page(api, models_file):
    api.models_update(
        "cloudall", "llm",
        {"provider": "custom", "base": "https://api.example.com/v1", "proto": "openai",
         "key_env": "VOX_LLM_KEY", "model": "x-1"},
        "全云端",
    )
    view = api.models_view()
    assert view["profiles"]["cloudall"]["label"] == "全云端"
    assert view["active"] == "local", "creating a profile must not switch the active one"


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"key_env": "sk-live-abcdefghijklmnop"}, "密钥"),
        ({"base": "http://evil.example.com/v1"}, "回环"),
        ({"base": "https://user:pass@example.com/v1"}, "凭据"),
        ({"proto": "grpc"}, "must be one of"),
        ({"command": "curl"}, "unknown model key"),
    ],
)
def test_a_models_write_outside_the_allow_list_is_refused(api, models_file, fields, message):
    before = models_file.read_text(encoding="utf-8")
    with pytest.raises(ApiError, match=message):
        api.models_update("local", "llm", fields)
    assert models_file.read_text(encoding="utf-8") == before


def test_models_toml_is_not_reachable_through_the_generic_config_editor(runtime, config_dir):
    """One door, with its own allow-list. A second door through ``config_update``
    would validate the same write more weakly."""
    assert "models.toml" not in EDITABLE
    api = ConsoleApi(runtime, config_dir=config_dir)
    with pytest.raises(ApiError, match="not a console-editable config file"):
        api.config_update("models.toml", {"active": "cloud-llm"})


#: Exactly what the page PUTs for the shipped ``local`` profile when 保存方案 is
#: clicked with nothing edited: the provider dropdown's preset fills base / proto /
#: key_env in for display, so they ride along in the request.
PAGE_SAVE_LOCAL = {
    "asr": {"provider": "sherpa-local", "model": "zipformer-zh-14M", "base": "",
            "proto": "custom", "key_env": ""},
    "tts": {"provider": "sherpa-local", "model": "vits-zh-single", "base": "",
            "proto": "custom", "key_env": ""},
    "llm": {"provider": "ollama", "model": "qwen2.5:7b", "base": "http://127.0.0.1:11434/v1",
            "proto": "ollama", "key_env": ""},
}


def test_a_save_with_nothing_edited_leaves_the_file_byte_for_byte(api, models_file):
    """The regression this asserts was real: the first version pinned the preset's
    endpoint into the file, so opening the page and clicking save grew four lines
    that duplicated ``providers.py``."""
    before = models_file.read_bytes()
    for kind, fields in PAGE_SAVE_LOCAL.items():
        api.models_update("local", kind, fields, "全本机（离线）")
    assert models_file.read_bytes() == before


def test_a_custom_provider_does_persist_its_endpoint(api, models_file):
    """The exemption: for ``custom`` the file *is* the source of truth."""
    api.models_update(
        "local", "llm",
        {"provider": "custom", "model": "m-1", "base": "https://gw.example.com/v1",
         "proto": "openai", "key_env": "VOX_LLM_KEY"},
    )
    llm = api.models_view()["profiles"]["local"]["llm"]
    assert llm["base"] == "https://gw.example.com/v1"
    assert llm["proto"] == "openai"
    assert llm["key_env"] == "VOX_LLM_KEY"


# -- the probe: the console's only outbound request ---------------------------


def probe_opener(status: int, *, record: list[str] | None = None, headers: dict | None = None):
    """A stand-in for ``urllib``'s opener that answers with one status code."""

    class Opener:
        @staticmethod
        def open(request, timeout=None):
            del timeout
            if record is not None:
                record.append(request.full_url)
                record.append(json.dumps(dict(request.headers), sort_keys=True))
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, "no", headers or {}, None)

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                @property
                def status(self):
                    return status

            return Response()

    return Opener()


def test_the_probe_reports_the_status_and_sends_no_credential(api, monkeypatch):
    """401 is the good answer, and it is only readable because nothing was sent."""
    seen: list[str] = []
    monkeypatch.setattr(routes, "_probe_opener", lambda: probe_opener(401, record=seen))
    result = api.models_probe("llm", "deepseek", "")
    assert result["status"] == 401
    assert result["url"] == "https://api.deepseek.com/v1/models"
    assert result["elapsed_ms"] >= 0
    assert "Authorization" not in seen[1] and "authorization" not in seen[1]


def test_the_probe_uses_the_preset_endpoint_when_the_page_sends_none(api, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(routes, "_probe_opener", lambda: probe_opener(200, record=seen))
    assert api.models_probe("llm", "ollama", "")["url"] == "http://127.0.0.1:11434/v1/models"
    assert seen[0] == "http://127.0.0.1:11434/v1/models"


def test_a_local_provider_with_no_endpoint_is_not_probed(api):
    with pytest.raises(ApiError, match="没有 HTTP 端点"):
        api.models_probe("asr", "sherpa-local", "")


@pytest.mark.parametrize(
    "base, message",
    [
        ("http://evil.example.com/v1", "回环"),
        ("https://user:pass@example.com/v1", "凭据"),
        ("file:///etc/passwd", "完整的 http"),
    ],
)
def test_the_probe_refuses_an_endpoint_it_should_not_touch(api, base, message):
    with pytest.raises(ApiError, match=message):
        api.models_probe("llm", "custom", base)


def test_a_probe_that_cannot_connect_is_a_502_not_a_404(api, monkeypatch):
    """The page reads 404/405 as "this daemon has no probe endpoint"; a dead
    endpoint must not be reported as a missing feature."""

    class Dead:
        @staticmethod
        def open(request, timeout=None):
            del request, timeout
            raise urllib.error.URLError("timed out")

    monkeypatch.setattr(routes, "_probe_opener", lambda: Dead())
    with pytest.raises(ApiError, match="连不上") as caught:
        api.models_probe("llm", "custom", "https://api.example.com/v1")
    assert caught.value.status == 502


def test_the_probe_does_not_follow_a_redirect(api, monkeypatch):
    """A 302 reports about this host; following it would report about another."""
    monkeypatch.setattr(routes, "_probe_opener", lambda: probe_opener(302))
    assert api.models_probe("llm", "custom", "https://api.example.com/v1")["status"] == 302


# -- fetching the model list: the probe's opposite on the credential question --


def fetch_opener(status: int, body: bytes = b"", *, record: list[str] | None = None):
    """Like ``probe_opener`` but the response has a body.

    That is the whole difference between the two endpoints: the probe never reads
    one, and this must.
    """

    class Opener:
        @staticmethod
        def open(request, timeout=None):
            del timeout
            if record is not None:
                record.append(request.full_url)
                record.append(json.dumps(dict(request.headers), sort_keys=True))
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                @property
                def status(self):
                    return status

                @staticmethod
                def read(limit=None):
                    return body[:limit] if limit is not None else body

            return Response()

    return Opener()


OPENAI_BODY = json.dumps(
    {"object": "list", "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}
).encode("utf-8")


def test_fetch_parses_the_openai_shape_and_carries_the_key_from_the_environment(
    api, monkeypatch
):
    """The page sends the variable *name*; the value is read here and only here."""
    seen: list[str] = []
    monkeypatch.setenv("VOX_LLM_KEY", "sk-secret-value")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, OPENAI_BODY, record=seen))

    result = api.models_fetch("llm", "deepseek", "")

    assert result["models"] == ["deepseek-chat", "deepseek-reasoner"]
    assert result["count"] == 2
    assert result["url"] == "https://api.deepseek.com/v1/models"
    assert result["authenticated"] is True
    assert result["key_env"] == "VOX_LLM_KEY"
    assert "Bearer sk-secret-value" in seen[1]


def test_fetch_never_returns_the_key_itself_only_the_variable_name(api, monkeypatch):
    """The one assertion that must never regress: a response that carries the key
    would put it in the browser, the access log and anything replaying the API."""
    monkeypatch.setenv("VOX_LLM_KEY", "sk-secret-value")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, OPENAI_BODY))

    result = api.models_fetch("llm", "deepseek", "")

    assert "sk-secret-value" not in json.dumps(result)


def test_fetch_sends_no_authorisation_header_to_a_local_service(api, monkeypatch):
    """Ollama needs no key, and an empty ``Bearer`` would come back 401 -- a fault
    that is not there."""
    seen: list[str] = []
    monkeypatch.delenv("VOX_LLM_KEY", raising=False)
    body = json.dumps({"data": [{"id": "qwen2.5:7b"}]}).encode("utf-8")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, body, record=seen))

    result = api.models_fetch("llm", "ollama", "")

    assert result["models"] == ["qwen2.5:7b"]
    assert result["authenticated"] is False
    assert "uthorization" not in seen[1]


def test_fetch_uses_anthropic_headers_for_the_anthropic_shape(api, monkeypatch):
    """``x-api-key`` plus a version: without the version the answer is 400, which
    looks like a bad request rather than a bad key."""
    seen: list[str] = []
    monkeypatch.setenv("VOX_LLM_KEY", "sk-ant-x")
    body = json.dumps({"data": [{"id": "claude-opus-5"}]}).encode("utf-8")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, body, record=seen))

    result = api.models_fetch("llm", "anthropic", "")

    assert result["models"] == ["claude-opus-5"]
    assert "X-api-key" in seen[1] or "x-api-key" in seen[1]
    assert routes.ANTHROPIC_VERSION in seen[1]
    assert "Bearer" not in seen[1]


def test_fetch_reads_the_ollama_native_shape_too(api, monkeypatch):
    body = json.dumps({"models": [{"name": "llama3.2"}, {"name": "qwen2.5:7b"}]}).encode("utf-8")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, body))
    assert api.models_fetch("llm", "ollama", "")["models"] == ["llama3.2", "qwen2.5:7b"]


def test_fetch_reads_a_bare_array_from_a_home_grown_gateway(api, monkeypatch):
    body = json.dumps(["b-model", "a-model", "b-model"]).encode("utf-8")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, body))
    # Sorted and de-duplicated: the list goes into a dropdown, and a dropdown with
    # the same name twice reads as a bug in this page rather than in the endpoint.
    assert api.models_fetch("llm", "ollama", "")["models"] == ["a-model", "b-model"]


def test_a_401_without_a_key_names_the_variable_to_set(api, monkeypatch):
    """401 is the probe's good answer and this one's failure -- and the two need
    different next steps, so the message has to say which one happened."""
    monkeypatch.delenv("VOX_LLM_KEY", raising=False)
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(401))
    with pytest.raises(ApiError, match=r"VOX_LLM_KEY") as caught:
        api.models_fetch("llm", "deepseek", "")
    assert caught.value.status == 502


def test_a_401_with_a_key_says_the_key_was_refused(api, monkeypatch):
    monkeypatch.setenv("VOX_LLM_KEY", "sk-wrong")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(403))
    with pytest.raises(ApiError, match="密钥被拒") as caught:
        api.models_fetch("llm", "deepseek", "")
    assert "sk-wrong" not in str(caught.value)


def test_a_404_says_the_path_is_wrong_rather_than_the_host_is_down(api, monkeypatch):
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(404))
    with pytest.raises(ApiError, match="路径不对"):
        api.models_fetch("llm", "deepseek", "")


def test_an_oversized_body_is_refused_rather_than_truncated(api, monkeypatch):
    """Half a JSON document parses into nothing useful, so saying so beats guessing."""
    body = b"[" + b'"x",' * routes.MAX_MODELS_BODY
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, body))
    with pytest.raises(ApiError, match="字节就不读了"):
        api.models_fetch("llm", "ollama", "")


def test_a_body_that_is_not_json_is_reported_as_such(api, monkeypatch):
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, b"<html>nope"))
    with pytest.raises(ApiError, match="不是 JSON"):
        api.models_fetch("llm", "ollama", "")


def test_a_shape_with_no_model_list_is_a_different_failure_from_no_connection(
    api, monkeypatch
):
    """"Connected but unreadable" and "could not connect" send you to different
    places: one is the response format, the other is the network."""
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, b'{"ok": true}'))
    with pytest.raises(ApiError, match="找不到模型列表"):
        api.models_fetch("llm", "ollama", "")


def test_the_name_list_is_capped(api, monkeypatch):
    rows = [{"id": f"m-{index:05d}"} for index in range(routes.MAX_MODEL_NAMES + 50)]
    body = json.dumps({"data": rows}).encode("utf-8")
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, body))
    assert len(api.models_fetch("llm", "ollama", "")["models"]) == routes.MAX_MODEL_NAMES


def test_a_local_provider_with_no_endpoint_cannot_be_listed(api):
    with pytest.raises(ApiError, match="没有 HTTP 端点可拉"):
        api.models_fetch("asr", "sherpa-local", "")


@pytest.mark.parametrize(
    "base, message",
    [
        ("http://evil.example.com/v1", "回环"),
        ("https://user:pass@example.com/v1", "凭据"),
        ("file:///etc/passwd", "完整的 http"),
    ],
)
def test_fetch_refuses_an_endpoint_it_should_not_touch(api, base, message):
    """Same gate as the probe, and it has to be re-asserted here: this request
    carries a credential, so a wrong host is worse than it is for the probe."""
    with pytest.raises(ApiError, match=message):
        api.models_fetch("llm", "custom", base)


def test_a_fetch_that_cannot_connect_is_a_502_not_a_404(api, monkeypatch):
    class Dead:
        @staticmethod
        def open(request, timeout=None):
            del request, timeout
            raise urllib.error.URLError("timed out")

    monkeypatch.setattr(routes, "_probe_opener", lambda: Dead())
    with pytest.raises(ApiError, match="连不上") as caught:
        api.models_fetch("llm", "custom", "https://api.example.com/v1")
    assert caught.value.status == 502


def test_the_fetch_endpoint_is_actually_routed(live, monkeypatch):
    """The page reads 404/405 as "this daemon is too old to have the button", so an
    unrouted endpoint would present as a version problem instead of a missing route.
    Every other assertion here calls the method directly and would stay green."""
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, OPENAI_BODY))

    status, body = fetch(
        live,
        "/api/models/fetch",
        method="POST",
        body={"kind": "llm", "provider": "deepseek"},
    )

    assert status == 200
    assert json.loads(body)["models"] == ["deepseek-chat", "deepseek-reasoner"]


def test_both_outbound_requests_announce_themselves(api, monkeypatch):
    """urllib's default ``Python-urllib/3.x`` is 403'd outright by a good number of
    gateways. Measured against a live relay: same key, same path, UA
    ``Python-urllib/3.13`` -> 403, curl's UA or none -> 200. That 403 surfaces as
    "the key was refused" while the key is fine, so both endpoints send a real name.
    """
    seen: list[str] = []
    monkeypatch.setattr(routes, "_probe_opener", lambda: fetch_opener(200, OPENAI_BODY, record=seen))

    api.models_fetch("llm", "ollama", "")
    assert routes.USER_AGENT in seen[1]
    assert "Python-urllib" not in seen[1]

    seen.clear()
    monkeypatch.setattr(routes, "_probe_opener", lambda: probe_opener(401, record=seen))
    api.models_probe("llm", "deepseek", "")
    assert routes.USER_AGENT in seen[1]


def test_the_announced_name_is_not_a_browser_disguise():
    """Being 403'd is for looking like a script, and this is a script. Pretending to
    be Chrome would defeat the other side's rate limiting and auditing."""
    assert "Vox" in routes.USER_AGENT
    for browser in ("Mozilla", "Chrome", "Safari", "AppleWebKit"):
        assert browser not in routes.USER_AGENT


# -- wake keywords: the table the page never used to show ----------------------


@pytest.fixture
def keywords_file(tmp_path, monkeypatch):
    path = tmp_path / "keywords.txt"
    monkeypatch.setenv("VOX_KEYWORDS_FILE", str(path))
    return path


def test_wake_view_reports_the_shipped_words_when_there_is_no_custom_table(api, keywords_file):
    """"Which words work right now" is the question this endpoint exists for: not
    being able to see the table is why "it will not wake" is usually just a word that
    was never in it."""
    view = api.wake_view()

    assert view["custom"] is False
    assert view["words"] == view["shipped_words"]
    assert view["restart_required"] is True
    assert view["limits"]["min_chars"] >= 3


def test_a_custom_table_takes_over_and_says_so(api, keywords_file):
    api.wake_update(["小沃小沃", "你好沃克"])

    view = api.wake_view()

    assert view["custom"] is True
    assert view["words"] == ["小沃小沃", "你好沃克"]
    assert view["active_path"] == str(keywords_file)


def test_a_word_that_could_never_wake_is_refused_and_nothing_is_written(api, keywords_file):
    with pytest.raises(ApiError, match="非汉字"):
        api.wake_update(["hello"])

    assert not keywords_file.exists()


def test_an_empty_list_deletes_the_custom_table(api, keywords_file):
    api.wake_update(["小沃小沃"])
    assert keywords_file.exists()

    result = api.wake_update([])

    assert result["custom"] is False
    assert not keywords_file.exists()


def test_a_bare_string_is_refused_rather_than_iterated(api, keywords_file):
    """A string is a Sequence: without the check every character becomes a keyword,
    and each one fails the length rule with a message about the wrong thing."""
    with pytest.raises(ApiError, match="array"):
        api.wake_update("小沃小沃")


def test_the_wake_endpoints_are_actually_routed(live, keywords_file):
    status, body = fetch(live, "/api/wake")
    assert status == 200
    assert json.loads(body)["restart_required"] is True

    # PUT rather than POST: this replaces the whole table, same as /api/models and
    # /api/config. A POST here would read as "add a word".
    status, body = fetch(live, "/api/wake", method="PUT", body={"words": ["小沃小沃"]})
    assert status == 200
    assert json.loads(body)["words"] == ["小沃小沃"]


# ------------------------------------------------------------------- profile


@pytest.fixture
def facts(tmp_path, monkeypatch):
    directory = tmp_path / "facts"
    directory.mkdir()
    monkeypatch.setenv("VOX_MEMORY_FACTS", str(directory))
    monkeypatch.setenv("VOX_MEMORY_DB", str(tmp_path / "memory.db"))
    return directory


def test_profile_lists_the_markdown_files_that_are_the_facts(api, facts):
    (facts / "prefers-chinese.md").write_text("# 语言偏好\n\n用中文交流。\n", encoding="utf-8")
    view = api.profile_list()
    assert view["present"] is True
    assert view["files"][0]["file"] == "prefers-chinese.md"
    assert view["files"][0]["title"] == "语言偏好"


def test_profile_reads_and_writes_one_file(api, facts):
    api.profile_save("about-me.md", "# 关于我\n\n我在 Windows 上做语音项目。\n")
    assert (facts / "about-me.md").is_file()
    assert "语音项目" in api.profile_read("about-me.md")["text"]


@pytest.mark.parametrize(
    "name",
    ["../escape.md", "sub/dir.md", "..\\escape.md", "notes.txt", "", ".md", "a" * 200 + ".md"],
)
def test_a_profile_name_that_could_escape_is_refused(api, facts, name):
    """Two checks, not one: the pattern rejects separators up front and the
    resolved path is confirmed to still be inside the directory."""
    with pytest.raises(ApiError):
        api.profile_save(name, "text")


def test_credential_shaped_profile_text_is_refused_whole(api, facts):
    """Not masked. A multi-line private key is exactly the case where masking
    leaves the body on disk."""
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    with pytest.raises(ApiError, match="credential") as caught:
        api.profile_save("keys.md", key)
    assert caught.value.status == 403
    assert not (facts / "keys.md").exists()


def test_an_empty_profile_file_is_refused(api, facts):
    with pytest.raises(ApiError, match="cannot be empty"):
        api.profile_save("blank.md", "   ")


def test_reading_a_missing_profile_file_is_404(api, facts):
    with pytest.raises(ApiError) as caught:
        api.profile_read("nope.md")
    assert caught.value.status == 404


def test_deleting_a_profile_file_removes_it(api, facts):
    api.profile_save("tmp.md", "# 临时\n\n内容。\n")
    assert api.profile_delete("tmp.md")["deleted"] is True
    assert not (facts / "tmp.md").exists()
    assert api.profile_delete("tmp.md")["deleted"] is False


def test_profile_sync_is_a_noop_without_memory(api, facts):
    assert api.profile_sync()["synced"] is False


# --------------------------------------------------------------- tool testing


def test_testing_a_tool_goes_through_the_real_gate(runtime):
    """No shortcut: a tool refused for a spoken request is refused here too."""
    from core.tools import open_tools

    runtime.tool_runner = open_tools(mcp=False)
    api = ConsoleApi(runtime)

    result = api.test_tool("shell.run", {"command": "git status"})

    assert result["ok"] is False
    # shell.run is not registered (disabled in the shipped config), so the gate
    # refuses it before any confirmation question arises.
    assert result["error"]


def test_testing_a_tool_reports_a_pending_confirmation_without_the_command(runtime):
    from core.tools import ToolRequest, ToolResult

    class Confirming:
        def run(self, request):
            return ToolResult(
                tool=request.tool,
                ok=False,
                error="confirmation required",
                needs_confirmation=True,
                audit={"decision": "confirm", "command": "git push --force"},
            )

    runtime.tool_runner = Confirming()
    api = ConsoleApi(runtime)
    result = api.test_tool("shell.run", {"command": "git push --force"})
    assert result["needs_confirmation"] is True
    assert result["confirm_where"] == "orb"
    assert "force" not in json.dumps(result["audit"])


def test_testing_a_tool_without_a_runner_is_409(api):
    api.runtime.tool_runner = None
    with pytest.raises(ApiError) as caught:
        api.test_tool("fs.read", {"path": "README.md"})
    assert caught.value.status == 409


def test_testing_an_unknown_agent_is_404(api):
    with pytest.raises(ApiError) as caught:
        api.test_agent("nonexistent", "hi")
    assert caught.value.status == 404


def test_testing_a_model_that_is_not_there_says_which_one(api):
    """``api`` has no stack attached, which is the headless case."""
    for which in ("tts", "asr", "kws"):
        with pytest.raises(ApiError, match=which) as caught:
            getattr(api, f"test_{which}")("" if which == "tts" else wav_base64(0.7))
        assert caught.value.status == 409


def test_testing_the_speaker_without_a_gate_is_409(api):
    with pytest.raises(ApiError, match="voiceprint gate") as caught:
        api.test_speaker(wav_base64(0.7))
    assert caught.value.status == 409


def test_a_bad_clip_is_reported_before_any_model_runs(api):
    with pytest.raises(ApiError, match="recording"):
        api.test_asr("not base64")


# --------------------------------------------------------------- microphone


def test_starting_the_microphone_without_a_stack_is_409(api):
    with pytest.raises(ApiError) as caught:
        api.mic_start()
    assert caught.value.status == 409


def test_stopping_a_microphone_that_never_started_is_harmless(api):
    assert api.mic_stop() == {"running": False}


def test_enrolling_without_a_gate_is_409(api):
    with pytest.raises(ApiError, match="voiceprint gate") as caught:
        api.enroll("due", [wav_base64(1.0)])
    assert caught.value.status == 409


def test_enrolling_needs_a_name_and_a_clip(api):
    with pytest.raises(ApiError, match="name is required"):
        api.enroll("", [wav_base64(1.0)])
    with pytest.raises(ApiError, match="at least one recording"):
        api.enroll("due", [])


# ---------------------------------------------------------------- mcp管理


@pytest.fixture
def mcp_dir(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config" / "mcp.toml"
    target = tmp_path / "config"
    target.mkdir()
    (target / "mcp.toml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_mcp_view_reports_the_shipped_posture(runtime, mcp_dir):
    api = ConsoleApi(runtime, config_dir=mcp_dir)
    view = api.mcp_view()
    assert view["present"] is True
    settings = {entry["name"]: entry for entry in view["settings"]}
    assert settings["enabled"]["value"] is False
    assert settings["enabled"]["editable"] is True
    assert settings["require_confirmation"]["locked"] is True, "the last gate is not a toggle"
    assert view["tools"] == []


def test_the_master_switch_can_be_flipped_from_the_console(runtime, mcp_dir):
    api = ConsoleApi(runtime, config_dir=mcp_dir)
    api.mcp_update({"mcp.enabled": True})
    text = (mcp_dir / "mcp.toml").read_text(encoding="utf-8")
    assert "enabled = true" in text
    assert "三层默认关" in text, "the file's own reasoning survives the edit"


@pytest.mark.parametrize(
    "key, value",
    [
        ("mcp.require_confirmation", False),
        ("servers[0].command", "rm"),
        ("servers[0].args", ["-rf", "/"]),
        ("servers[0].allow", []),
        ("servers[0].auto_allow", ["write_file"]),
        ("servers[0].env_passthrough", ["AWS_SECRET_ACCESS_KEY"]),
    ],
)
def test_the_mcp_boundaries_are_refused(runtime, mcp_dir, key, value):
    """``require_confirmation`` is the last gate; ``allow``/``auto_allow`` only widen
    when edited; ``command``/``args`` choose what executes."""
    api = ConsoleApi(runtime, config_dir=mcp_dir)
    with pytest.raises(ApiError, match="not editable from the console") as caught:
        api.mcp_update({key: value})
    assert caught.value.status == 403


def test_a_broken_mcp_edit_never_lands(runtime, mcp_dir):
    api = ConsoleApi(runtime, config_dir=mcp_dir)
    before = (mcp_dir / "mcp.toml").read_text(encoding="utf-8")
    with pytest.raises(ApiError, match="rejected"):
        api.mcp_update({"mcp.enabled": "yes please"})
    assert (mcp_dir / "mcp.toml").read_text(encoding="utf-8") == before


def test_mcp_view_lists_running_tools(runtime, mcp_dir):
    from core.tools import open_mcp_tools, open_tools
    from core.tools.mcp import McpServerConfig
    from tests.test_mcp import FakeProc, spawner

    proc = FakeProc(tools=[{"name": "read_file"}])
    registry = open_mcp_tools(
        {
            "enabled": True,
            "max_output_bytes": 2000,
            "servers": (
                McpServerConfig(name="fake", command=sys.executable, enabled=True),
            ),
        },
        spawn=spawner(proc),
    )
    runtime.tool_runner = open_tools(mcp=registry)
    api = ConsoleApi(runtime, config_dir=mcp_dir)

    view = api.mcp_view()

    assert view["tools"] == ["mcp.fake.read_file"]
    assert view["runtime"]["servers"][0]["name"] == "fake"
    runtime.tool_runner.close()
