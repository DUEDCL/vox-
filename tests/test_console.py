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
import shutil
import struct
import tomllib
import urllib.error
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import SounddeviceWakeCapture
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


def test_a_second_console_on_a_busy_port_is_refused_not_silently_shadowed(api):
    """2026-08-30 实机踩到的那一条：**两个控制台同时 LISTEN 在 8899**。

    `HTTPServer` 把 `allow_reuse_address` 设成 1。POSIX 上那只影响 TIME_WAIT 的重绑，
    一个已经在 LISTEN 的地址仍然绑不上；**Windows 上它允许第二个进程绑同一个地址**。
    于是第二次 `start()` 报告成功、打印一个带新 token 的 URL，而内核可能把连接投给先
    起来的那个进程 —— 打开那个 URL 得到的是 `a console token is required`，而两个进程
    都说自己启动成功了。

    所以判断不能交给「绑定会不会失败」（那个答案是平台相关的），要**正向探测**。
    """
    first = ConsoleServer(api, port=0)
    first.start()
    try:
        second = ConsoleServer(api, port=first.port)
        with pytest.raises(ConsoleError) as caught:
            second.start()
        message = str(caught.value)
        assert str(first.port) in message, "报错必须说清是哪个端口"
        assert "--port" in message, "报错必须给出能照做的下一步"
        assert second.describe()["running"] is False
    finally:
        first.stop()


def test_the_probe_says_nothing_is_there_after_a_stop(api):
    """停掉之后同一个端口必须能再起 —— 否则这道闸会把正常的重启也挡住。"""
    from core.console.server import port_is_served

    server = ConsoleServer(api, port=0)
    server.start()
    port = server.port
    assert port_is_served(server.host, port) is True
    server.stop()
    assert port_is_served(server.host, port) is False
    again = ConsoleServer(api, port=port)
    again.start()  # 不抛就是通过
    again.stop()


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
    that duplicated ``providers.py``.

    字段**从文件里读出来再存回去**，不写死。写死的那一版在 2026-08-29 挂了 —— 使用者从
    控制台把 `[profiles.local.tts]` 改成了自己的 dashscope 条目，于是「把出厂值存回去」
    真的改动了文件，而那是正确行为。一个依赖「使用者没改过配置」的测试不是测试。
    """
    view = api.models_view()
    profile = view["profiles"]["local"]
    before = models_file.read_bytes()
    for kind in ("asr", "tts", "llm"):
        section = dict(profile.get(kind) or {})
        api.models_update(
            "local",
            kind,
            {name: section.get(name, "") for name in ("provider", "model", "base", "proto", "key_env")},
            str(profile.get("label") or ""),
        )
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


# ------------------------------------------------------- 唤醒漏斗（三层分离）
#
# 「喊了没反应」有三个完全不同的根因,而它们在使用者眼里长得一模一样:麦克风没进声音、
# 唤醒词没命中、声纹把它拒了。实机诊断读出「KWS 命中 16/16、声纹接受 0/16」正是靠这个
# 分离 —— 合成一个数字就什么也答不了。


def test_state_reports_the_wake_funnel_in_three_separate_numbers(api):
    api.runtime.wake_stats = {"kws": 16, "accepted": 0, "rejected": 16}
    api.runtime.wake_recent = [
        {"at": 1.0, "keyword": "你好小沃", "verdict": "rejected",
         "score": 0.482, "reason": "below threshold 0.5"},
        {"at": 0.5, "keyword": "你好小沃", "verdict": "kws"},
    ]

    wake = api.state()["wake"]

    assert wake["kws"] == 16
    assert wake["accepted"] == 0
    assert wake["rejected"] == 16
    assert [entry["verdict"] for entry in wake["recent"]] == ["rejected", "kws"]
    assert wake["recent"][0]["score"] == 0.482


def test_the_wake_funnel_survives_a_runtime_without_the_fields(api):
    """老的 runtime(或测试替身)没有这两个属性时报零,而不是让整个 /api/state 500。"""
    for attribute in ("wake_stats", "wake_recent"):
        if hasattr(api.runtime, attribute):
            delattr(api.runtime, attribute)

    wake = api.state()["wake"]

    assert wake == {
        "kws": 0,
        "accepted": 0,
        "rejected": 0,
        "listen_refused": 0,
        "last_listen_refusal": "",
        "muted": 0,
        "enroll_only": False,
        "held": 0,
        "recent": [],
    }


def test_the_funnel_reports_the_fourth_layer_and_the_ack_mute_window(runtime):
    """第四层：**通过了声纹但识别器没开起来**，也就是「唤醒了却没有后文」本身。

    以及 ``muted``（确认音期间被丢掉的块数）。后者平时是个不大的数；一直涨说明静音窗
    没被收回来，而那个症状是「完全没反应」—— 那时这一格是唯一的读数。
    """
    capture = SimpleNamespace(
        last_listen_refusal="没有接 on_recognized —— 转写出来也没人接",
        muted_blocks=17,
    )
    stack = SimpleNamespace(
        capture=capture, verifier=None, readiness=lambda: [], warnings=[]
    )
    api = ConsoleApi(runtime, stack)
    api.runtime.wake_stats = {"kws": 3, "accepted": 3, "rejected": 0, "listen_refused": 2}
    api.runtime.wake_recent = [{"at": 1.0, "verdict": "listen_refused", "reason": "没有接 on_recognized"}]

    wake = api.state()["wake"]

    assert wake["listen_refused"] == 2
    assert "on_recognized" in wake["last_listen_refusal"]
    assert wake["muted"] == 17


def test_the_wake_funnel_never_carries_audio_or_vectors(api):
    """这份数据只到本机控制台,但它仍然不该带音频或向量 —— 相似度是个标量,不是特征。"""
    api.runtime.wake_stats = {"kws": 1, "accepted": 1, "rejected": 0}
    api.runtime.wake_recent = [{"at": 1.0, "keyword": "你好小沃", "verdict": "accepted", "score": 0.71}]

    entry = api.state()["wake"]["recent"][0]

    assert set(entry) <= {"at", "keyword", "verdict", "score", "reason"}


# ------------------------------------- 声纹注册走采集缓冲（2026-08-30，同信道）


class StubRing:
    """站在 ``SounddeviceWakeCapture`` 的位置上，只提供注册要用的那两个方法。

    ``forget_recent_audio()`` 故意不真清：测试里 `time.sleep` 被跳过，真清掉之后快照就是
    空的，那测到的就不是这段逻辑了。清了几次单独记，因为「录之前先清」本身是要断言的行为。
    """

    buffer_seconds = 3.0

    def __init__(self, samples, *, speech: bool = True, verifier=None) -> None:
        self.samples = samples
        self.cleared = 0
        self.held: list[float] = []
        self.listening = False
        #: VAD 的答案由测试指定 —— 「这一段有没有人说话」不是能量问题，所以替身也不该
        #: 从样本里猜。见 core/audio/vad.py。
        self.speech = speech
        #: 注册模式那一路：`arm_after_enrollment` 只看这两格。
        self.enroll_only = False
        self.verifier = verifier

    #: **借真实实现，不在替身里复写一份判定。** 复写的那一份会和 capture 里的漂移，
    #: 而这里要断言的恰恰是「控制台调了它、并把结果报出来」。
    arm_after_enrollment = SounddeviceWakeCapture.arm_after_enrollment

    def has_speech(self, samples) -> bool:
        del samples
        return self.speech

    def hold_wake_for(self, seconds: float) -> None:
        self.held.append(seconds)

    def forget_recent_audio(self) -> None:
        self.cleared += 1

    def recent_audio(self, seconds):
        del seconds
        return self.samples


class StubGate:
    threshold = 0.5

    def __init__(self, *, speakers=("du",)) -> None:
        self.enrolled: list[tuple[str, int]] = []
        self.verified = 0
        self.throttled: list[bool] = []
        #: 已注册的人。**替身也必须有这一格**：没有档案时「试一句」要在录音之前就拒绝，
        #: 而不是录满 3 秒再报一个会被读成测量结果的 0。
        self.speakers = list(speakers)

    def enroll(self, name, chunks):
        from core.audio.speaker import EnrollmentResult

        chunks = list(chunks)
        self.enrolled.append((name, len(chunks)))
        if name not in self.speakers:
            self.speakers.append(name)
        return EnrollmentResult(
            speaker=name,
            samples_used=len(chunks),
            total_seconds=sum(len(c) for c in chunks) / 16000,
            dim=192,
        )

    def verify(self, samples, *, sample_rate=16000, throttle=True):
        from core.audio.speaker import VerificationResult

        del samples, sample_rate
        self.verified += 1
        self.throttled.append(throttle)
        return VerificationResult(True, "du", 0.819, "match")


@pytest.fixture
def mic_api(runtime, monkeypatch):
    """一个「麦克风在跑」的控制台，采集缓冲里装着一段像语音的音频。"""
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    tone = (np.sin(np.linspace(0, 900.0, 3 * 16000)) * 0.3).astype(np.float32)
    ring = StubRing(tone)
    gate = StubGate()
    ring.verifier = gate
    api = ConsoleApi(runtime, SimpleNamespace(capture=ring, verifier=gate))
    api.mic_running = True
    return api, ring, gate


def test_a_clip_comes_from_the_buffer_the_gate_reads(mic_api):
    """**注册和校验必须同信道。**

    浏览器 `getUserMedia` 拿的是浏览器认为的默认设备（不是 `[input] device`），还带它
    自己的 AGC / 降噪 / 回声消除和采样率；而门读的是 `SounddeviceWakeCapture` 那条流。
    两条链路各自都「录成功了」，比出来的相似度却是在比链路而不是比人 —— 使用者
    2026-08-30 的原话：「为何在 web 界面的注册声纹逻辑和你给出的脚本不同」。

    现在取的是 `capture._ring`，也就是唤醒时送去校验的**同一份**音频，所以同信道是
    构造上成立的。
    """
    api, ring, _gate = mic_api

    first = api.capture_clip(3.0)

    assert first["clips"] == 1
    assert first["duration_s"] == pytest.approx(3.0, abs=0.01)
    assert first["peak"] > 0.2
    assert ring.cleared == 1, "录之前要先清缓冲，否则上一段的尾巴会被算进这一段"


def test_a_clip_never_travels_as_audio(mic_api):
    """返回的是**数字**，不是音频。样本留在服务端内存里 —— 少一次编码、少一次传输，
    也少一个音频可能被落到别处的地方（红线 1）。"""
    api, _ring, _gate = mic_api

    payload = api.capture_clip(3.0)

    assert set(payload) == {"index", "clips", "duration_s", "rms", "clip_ratio", "peak"}


def test_recording_without_a_microphone_names_the_button_not_a_terminal(runtime):
    """麦克风没在跑时，这条错误要指向**这一页上的按钮**。

    它此前指向 `scripts/enroll_speaker.py`，因为第一次注册确实只能走终端（声纹门不许在
    没人注册时开麦）。那个死锁已经拆了（`enroll_only`），而使用者的要求是「希望在控制台能
    进行全部的设置，包括第一次录制声纹」—— 一条把人赶去开终端的提示语现在是过时的。
    """
    api = ConsoleApi(runtime, SimpleNamespace(capture=StubRing(None), verifier=None))
    api.mic_running = False

    with pytest.raises(ApiError) as caught:
        api.capture_clip(3.0)

    assert "开麦克风" in str(caught.value)
    assert "enroll_speaker.py" not in str(caught.value)


def test_enrolling_uses_the_buffered_clips_and_then_forgets_them(mic_api):
    api, _ring, gate = mic_api
    api.capture_clip(3.0)
    api.capture_clip(3.0)

    result = api.enroll_captured("du")

    assert gate.enrolled == [("du", 2)]
    assert result["speaker"] == "du"
    assert result["audio_saved"] is False
    assert len(result["quality"]) == 2
    assert api._enroll_clips == [], "注册完音频就该没了"


def test_one_clip_too_many_is_refused_rather_than_silently_dropped(mic_api):
    from core.audio.enroll_prompts import DEFAULT_ROUNDS

    api, _ring, _gate = mic_api
    for _ in range(DEFAULT_ROUNDS):
        api.capture_clip(3.0)

    with pytest.raises(ApiError):
        api.capture_clip(3.0)


def test_every_prompted_sentence_can_actually_be_recorded(mic_api):
    """**画着 6 个格子就必须能录满 6 段。**

    2026-09-01 实机：使用者报「无法进行三个以上的语音录制」。服务端的上限是
    `DEFAULT_ROUNDS`（6），而页面上另写了一个硬编码的 3 —— 于是第 4 段被页面自己拦下，
    提示语还写着「已经录了 3 段」。后两句「往后退两步」永远录不到，而那两句正是
    「不同距离也能唤醒」的全部依据：只用近场注册时，远处的相似度实测掉到 0.607。

    这条断言在服务端；页面那一侧的上限现在读 `/api/speaker` 的 `max_clips`，不再自己写。
    """
    from core.audio.enroll_prompts import DEFAULT_ROUNDS

    api, _ring, gate = mic_api
    for index in range(DEFAULT_ROUNDS):
        assert api.capture_clip(3.0)["clips"] == index + 1

    result = api.enroll_captured("du")

    assert gate.enrolled == [("du", DEFAULT_ROUNDS)]
    assert result["samples_used"] == DEFAULT_ROUNDS
    # 页面据此决定画几格、拦在第几段 —— 两个数字必须是同一个来源。
    assert api.speaker_view()["max_clips"] == DEFAULT_ROUNDS


def test_enrolling_from_the_console_arms_the_wake_gate(mic_api):
    """**注册成功之后唤醒必须立刻可用。**

    ``enroll_only``（注册模式：麦克风开着但唤醒判定被按住）只在 ``capture.start()`` 里判
    一次，而控制台注册发生在麦克风已经跑起来之后。2026-09-01 实机：使用者注册成功，页面
    显示成功，然后「依旧无法进行语音唤醒」—— 因为那一格没人去重判，必须重启才行。

    判定本身在 `core/audio/capture.py` 的 `arm_after_enrollment()`（这里借的就是它，
    替身不复写），fail-closed 的方向由 tests/test_speaker_privacy.py 钉。
    """
    api, ring, _gate = mic_api
    ring.enroll_only = True
    api.capture_clip(3.0)

    result = api.enroll_captured("du")

    assert result["wake_armed"] is True, "注册成功必须解开注册模式，页面据此告诉用户可以喊了"
    assert ring.enroll_only is False
    assert api.speaker_view()["enroll_only"] is False


def test_trying_a_sentence_with_nobody_enrolled_says_so_instead_of_scoring_zero(runtime):
    """**「相似度 0」不许被当成测量结果报出来。**

    2026-09-01 实机：使用者报「声纹试一句里『你好，小沃』的相似度为 0」。那个 0 是对的
    —— `verify()` 在没有档案时返回 `(False, None, 0.0, "no speaker enrolled")` —— 但页面
    把它排在「相似度」那一栏里，读起来像「你的声音不像你」，于是人会去查麦克风、查距离、
    查阈值。真正的原因是档案表是空的。

    而且这个拒绝必须发生在**录音之前**：先安静地录 3 秒再报一个常量，比不给读数更糟。
    """
    api = ConsoleApi(
        runtime,
        SimpleNamespace(capture=StubRing(None), verifier=StubGate(speakers=())),
    )
    api.mic_running = True

    with pytest.raises(ApiError, match="还没有人注册") as caught:
        api.verify_captured(3.0)

    assert "0" in str(caught.value), "要说清楚那个 0 是「没有可比对的档案」而不是分数"


def test_the_wake_funnel_says_when_the_gate_is_held_for_enrolling(mic_api):
    """四个计数全 0 有第五个根因，而它不是故障：注册模式。

    不报出来的话，「唤醒判定被按住」和「KWS 根本没装上」在页面上长得一模一样 ——
    使用者 2026-09-01 的原话是「为什么还是无法唤醒，我看也没有触发唤醒词」。
    """
    api, ring, _gate = mic_api
    ring.enroll_only = True
    ring.wake_holds = 41

    funnel = api._wake_funnel()

    assert funnel["enroll_only"] is True
    assert funnel["held"] == 41
    assert funnel["kws"] == 0


# -- 输入音量（Windows 那一侧）------------------------------------------------


class FakeMixer:
    """一只受控的麦克风 + 它的 OS 音量。

    ``curve`` 决定「音量标度 → 说到的峰值」。**故意是指数而不是线性**：Windows 的
    `SetMasterVolumeLevelScalar` 走 dB 曲线，实测这台机器上 0.01 和 0.82 分别对应约
    0.03× 和 0.54× 的幅度。一个只在线性设备上收敛的算法在真机上会来回过冲。
    """

    class LevelUnavailable(RuntimeError):
        pass

    def __init__(self, *, level: float = 0.01, name: str = "麦克风阵列 (Realtek(R) Audio)",
                 range_db: float = 40.0, sensitivity: float = 1.0) -> None:
        self.name = name
        self.level = float(level)
        self.range_db = range_db
        self.sensitivity = sensitivity
        self.writes: list[float] = []

    # -- 被 routes 当成 winlevel 模块来用 --
    def device_name(self, _selector):
        return self.name

    def read_level(self, name):
        if name != self.name:
            raise self.LevelUnavailable(f"没有名叫「{name}」的活动采集端点")
        # **读到的值当场定格**，和真的 `Endpoint`（frozen dataclass）一样。惰性读 self.level
        # 的替身会让「校准前」这一格在最后被改写成校准后的值，那种测试永远是绿的。
        level = round(self.level, 4)
        return SimpleNamespace(
            name=self.name, level=level, muted=False,
            describe=lambda: {"name": self.name, "level": level, "muted": False},
        )

    def set_level(self, name, value, unmute=True):
        del unmute
        self.writes.append(round(float(value), 4))
        self.level = max(0.0, min(1.0, float(value)))
        return self.read_level(name)

    def peak(self) -> float:
        """这只麦克风在当前音量下听到的说话峰值。"""
        return min(1.0, self.sensitivity * 10 ** ((self.level - 1.0) * self.range_db / 20.0))


def _mixer_api(runtime, monkeypatch, mixer: FakeMixer):
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    monkeypatch.setattr(routes, "winlevel", mixer)
    ring = StubRing(None)

    def snapshot(_seconds):
        # 一段峰值由当前 OS 音量决定的「语音」。VAD 说有人在说话。
        return np.full(int(3 * 16000), mixer.peak(), dtype=np.float32)

    ring.recent_audio = snapshot
    api = ConsoleApi(runtime, SimpleNamespace(capture=ring, verifier=StubGate()))
    api.mic_running = True
    return api


def test_the_input_level_view_names_the_device_and_reads_the_os_volume(runtime, monkeypatch):
    """**「设备坏了」和「音量是 1%」此前长得一模一样。**

    2026-09-01 实测同一时刻：「耳机 (沉麟的耳机)」的 OS 输入音量是 0.01，「麦克风阵列
    (Realtek(R) Audio)」是 0.82。两个症状（唤不醒 / 一说就削波）是同一个可读的数字，而
    界面上此前既没有设备名也没有这个音量 —— 于是只能猜设备。

    设备也必须报**名字**：`input.device = "2"` 这个索引在 08-29 指的是耳机，09-01 实测
    已经指到了麦克风阵列，因为中间插拔过设备。
    """
    mixer = FakeMixer(level=0.01)
    api = _mixer_api(runtime, monkeypatch, mixer)

    level = api._input_level()

    assert level["device"] == "麦克风阵列 (Realtek(R) Audio)"
    assert level["os"]["level"] == 0.01
    assert level["os_reason"] == ""


def test_calibration_walks_a_dead_quiet_microphone_into_the_band(runtime, monkeypatch):
    """使用者的原话：「真正的最佳效果应该是无论何种设备、音量，都能准确的识别唤醒词」。

    一只 OS 音量 1% 的麦克风（实测就是他那只耳机）说话峰值约 0.01 —— 那个量级和「麦克风
    是死的」区分不开。校准把它调进目标带，而**不需要人去找那根滑条**。
    """
    mixer = FakeMixer(level=0.01)
    api = _mixer_api(runtime, monkeypatch, mixer)

    report = api.calibrate_input(2.0)

    assert report["settled"] is True, report["trail"]
    assert report["before"]["level"] == 0.01
    low, high = routes.CALIBRATE_BAND
    assert low <= report["trail"][-1]["peak"] <= high
    assert mixer.writes, "校准必须真的写过 OS 那一侧的音量"
    assert len(report["trail"]) <= routes.CALIBRATE_ROUNDS


def test_calibration_pulls_a_clipping_microphone_back_down(runtime, monkeypatch):
    """另一端：削波。**这一端软件救不了** —— 削波发生在 ADC 里，采样进来已经是一排平顶，
    任何软件增益只能等比缩小它。所以它只能在 OS 那一侧解决，而这正是那一侧。

    出处是 2026-09-01 的实机截图：注册第 3 段 `peak 1.000`，而当时 OS 音量是 0.82。
    """
    mixer = FakeMixer(level=0.82, sensitivity=8.0)
    api = _mixer_api(runtime, monkeypatch, mixer)
    assert mixer.peak() >= 0.999, "先确认这只替身在起点是削波的"

    report = api.calibrate_input(2.0)

    assert report["settled"] is True, report["trail"]
    assert report["after"]["level"] < report["before"]["level"]
    low, high = routes.CALIBRATE_BAND
    assert low <= report["trail"][-1]["peak"] <= high


def test_calibration_never_chases_a_silent_room(runtime, monkeypatch):
    """没人说话时**一格都不许动**。

    拿房间底噪去校准会把音量一路推到顶，那正好是削波那一端 —— 也就是把一台好机器调坏。
    唯一诚实的动作是什么都不做，然后说清楚要人说话。
    """
    mixer = FakeMixer(level=0.30)
    api = _mixer_api(runtime, monkeypatch, mixer)
    api.stack.capture.speech = False  # VAD：这几秒里没有人说话

    report = api.calibrate_input(2.0)

    assert mixer.writes == [], "没听到说话就改音量，等于拿底噪校准"
    assert report["heard"] == 0
    assert report["after"]["level"] == 0.30
    assert "说话" in report["hint"]


def test_calibration_refuses_at_once_when_the_microphone_is_off(runtime, monkeypatch):
    """麦克风没开时**立刻**拒绝，不走满 8 轮再报「没听到说话」。

    实机（8900 端口的探针）走过那条路：8 条 trail 全是「麦克风没在跑」，最后 hint 说
    「没听到说话，再点一次」—— 那句话会让人以为自己说得不够大声。
    """
    mixer = FakeMixer(level=0.30)
    api = _mixer_api(runtime, monkeypatch, mixer)
    api.mic_running = False

    with pytest.raises(ApiError, match="开麦克风"):
        api.calibrate_input(2.0)

    assert mixer.writes == []


def test_calibration_says_so_when_the_host_has_no_level_control(runtime, monkeypatch):
    """没有音量控制的设备是存在的（虚拟设备、远程会话里的端点）。那时要说「这台机器上没法
    自动调」，而不是假装调过了。"""
    mixer = FakeMixer()

    def refuse(_name):
        raise mixer.LevelUnavailable("「x」没有音量控制（虚拟设备常见）")

    mixer.read_level = refuse
    api = _mixer_api(runtime, monkeypatch, mixer)

    with pytest.raises(ApiError, match="没有音量控制"):
        api.calibrate_input(2.0)


def test_the_page_is_told_which_sentences_to_read(mic_api):
    """提示句由服务端给（`core/audio/enroll_prompts.py`），页面不自己写一份 —— 脚本和
    页面共用同一份，两条路的提示才不会各自漂移。

    使用者 2026-08-31 的原话：「至少 6 条的不同文字（尽量长一点），不要至少『你好，问问』」。
    六句各不相同是刻意的：同一句念六遍练出来的质心对那一句的音素组合过拟合，而唤醒时
    说的是唤醒词、之后说的是任意请求。
    """
    from core.audio.enroll_prompts import DEFAULT_ROUNDS

    api, _ring, _gate = mic_api
    view = api.speaker_view()

    assert view["max_clips"] == DEFAULT_ROUNDS >= 6
    texts = [item["text"] for item in view["prompts"]]
    assert len(texts) == len(set(texts)) == DEFAULT_ROUNDS, "六句必须各不相同"
    assert all(len(text) >= 12 for text in texts), "太短的句子 embedding 不稳（实测 0.8s 窗只有 0.59–0.67）"
    assert not any("你好问问" in text for text in texts)
    # 最后两句换距离：只用近场注册时，远处的相似度实测掉到 0.607。
    assert len({item["condition"] for item in view["prompts"]}) == 2
    assert view["prompts"][-1]["condition"] != view["prompts"][0]["condition"]


def test_enrolling_with_nothing_recorded_is_refused(mic_api):
    api, _ring, _gate = mic_api

    with pytest.raises(ApiError):
        api.enroll_captured("du")


def test_a_window_with_no_speech_in_it_is_refused_by_the_vad(runtime, monkeypatch):
    """**这是「我没说话，相似度 0.9」的正解。**

    使用者 2026-08-31：什么都没说、等了一会，试一句报「相似度 0.979 · 通过」。数字是真
    算出来的，但两边都是放大后的房间底噪。峰值 / RMS / 削波比例都是**能量**统计量，而
    「是不是人声」不是能量问题 —— 拿能量去近似它，必然在某台设备上翻车。

    所以判据是 VAD。实测（`core/audio/vad.py` 的冒烟）：同一段底噪放大 10 倍后判 False，
    而真实人声缩到峰值 **0.01** 仍然判 True —— 那才是使用者要的「无论何种设备、音量」。
    """
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    # 电平健康（峰值 0.5，过得了峰值线），但里面没有人说话。
    loud_noise = np.full(3 * 16000, 0.5, dtype=np.float32)
    api = ConsoleApi(
        runtime,
        SimpleNamespace(capture=StubRing(loud_noise, speech=False), verifier=StubGate()),
    )
    api.mic_running = True

    with pytest.raises(ApiError, match="没有检测到人说话"):
        api.capture_clip(3.0)

    assert api._enroll_clips == [], "拒掉的窗口不许留在注册缓冲里"


def test_a_window_that_could_be_a_dead_microphone_is_refused(runtime, monkeypatch):
    """**这是「我没说话，相似度 0.9」的第二道保险。**

    使用者 2026-08-31：什么都没说、等了一会，试一句报「相似度 0.979 · 通过」。那个数字是
    真算出来的，但两边都是**放大后的房间底噪** —— 缓冲当时存的是加过增益的样本（约 10 倍），
    一段静音于是看上去是 rms 0.21 的健康语音。缓冲已改成存原始音频；这条线兜住剩下的情况：
    原始峰值低到和「麦克风是死的」区分不开时，**报错比报一个 0.979 诚实**。

    0.05 的出处是那台机器的实测 —— 五分钟内原始峰值最大值 0.0587，而那期间使用者在说话。
    """
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    dead = np.full(3 * 16000, 0.01, dtype=np.float32)
    api = ConsoleApi(
        runtime,
        SimpleNamespace(capture=StubRing(dead, speech=True), verifier=StubGate()),
    )
    api.mic_running = True

    with pytest.raises(ApiError, match="麦克风没在收音"):
        api.capture_clip(3.0)

    assert api._enroll_clips == [], "拒掉的窗口不许留在注册缓冲里"


def test_the_state_view_says_when_the_input_is_too_quiet_to_mean_anything(runtime):
    """`input_level` 现在带 `too_quiet` 与增益倍数。

    光报 `peak: 0.0587 / silent: false` 不够 —— 「不是全零」被读成了「没问题」，而真实情况
    是那个量级根本承载不了唤醒。增益倍数同样必须可见：它越大说明设备来的越轻，而它抬起来的
    是信号也是底噪。
    """
    gain = SimpleNamespace(describe=lambda: {"gain": 9.6, "clipped_blocks": 0})
    capture = SimpleNamespace(
        input_peak=0.0587, input_blocks=3134, sample_rate=16000, blocksize=1600,
        input_silent=False, auto_gain=gain,
    )
    api = ConsoleApi(runtime, SimpleNamespace(capture=capture, verifier=None))
    api.mic_running = True

    level = api._input_level()

    assert level["too_quiet"] is True
    assert level["want_peak"] == routes.LIVE_MIN_PEAK
    assert level["gain"]["gain"] == 9.6
    assert level["silent"] is False, "不是全零 —— 而这正是它此前被读成「没问题」的原因"


def test_trying_one_sentence_does_not_eat_the_enrollment_clips(mic_api):
    """「试一句」是脚本那段闭环校验搬到页面上。它和唤醒同路，所以分数能预测唤醒时的
    分数 —— 但它不该把正在准备的注册段吃掉。"""
    api, _ring, gate = mic_api
    api.capture_clip(3.0)

    result = api.verify_captured(3.0)

    assert result["accepted"] is True
    assert result["score"] == pytest.approx(0.819)
    assert result["threshold"] == 0.5
    assert gate.verified == 1
    assert len(api._enroll_clips) == 1, "试一句不许占用注册的那三格"
    assert gate.throttled == [False], "诊断不许消耗本人的暴力防护额度"


def test_recording_holds_the_wake_word_so_it_cannot_fight_the_gate(mic_api):
    """使用者 2026-08-31 报的「试一句经常相似度为 0，我怀疑是与真实唤醒冲突了」—— 是的。

    页面提示让人说的就是唤醒词，于是取样期间 KWS 真的命中：`_authorise` 的 finally 里
    `_ring.clear()` 把刚录的清掉，`_start_listening()` 又把模式切成聆听（之后的块不再进
    环形缓冲）。取样结束时快照几乎是空的 -> 质量门判「太轻」-> 0 分。顺带白弹一次球、
    白播一次确认音。

    所以取样期间要把唤醒判定按住。这不放宽任何边界 —— 它让唤醒**更难**发生。
    """
    api, ring, _gate = mic_api

    api.capture_clip(3.0)

    assert ring.held == [pytest.approx(3.5)], "取样窗口 + 余量都要按住"


def test_recording_is_refused_while_a_wake_is_being_listened_to(mic_api):
    """聆听期间音频全喂识别器、一个样本都不进环形缓冲。那时取样只会拿到空的 —— 拒绝
    比给一个假读数好。"""
    api, ring, _gate = mic_api
    ring.listening = True

    with pytest.raises(ApiError, match="聆听"):
        api.capture_clip(3.0)


def test_trying_one_sentence_uses_the_window_the_gate_uses(runtime, monkeypatch):
    """**窗长必须和门一致。** 2026-08-30 使用者报的：试一句的相似度和实机命中最大差 0.2，
    而且是在同等距离、同等音量下。

    根因之一就在这里：唤醒时送去校验的是「命中前 `verify_seconds`（1.5）秒」，而这个按钮
    此前拿整段 3 秒去算。实测同一档案 1.0 s 窗得 0.774、3.0 s 窗得 0.846 —— 这个诊断会比
    门实际给的分高 0.07 以上，叠上实机窗里那截静音（再掉 0.05–0.09）就是 0.2。

    取**尾部**：唤醒时那个窗口以唤醒词结尾，形状要一样。
    """
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    tone = (np.sin(np.linspace(0, 900.0, 3 * 16000)) * 0.3).astype(np.float32)
    tone[: 16000] = 0.0  # 前 1 秒是静音，只有尾部才是「说的那一句」
    seen: list[int] = []

    class WindowGate(StubGate):
        def verify(self, samples, *, sample_rate=16000, throttle=True):
            seen.append(len(samples))
            return super().verify(samples, sample_rate=sample_rate, throttle=throttle)

    api = ConsoleApi(
        runtime,
        SimpleNamespace(capture=SimpleNamespace(
            recent_audio=lambda _s: tone,
            forget_recent_audio=lambda: None,
            has_speech=lambda _s: True,
            buffer_seconds=3.0,
            verify_seconds=1.5,
        ), verifier=WindowGate()),
    )
    api.mic_running = True

    result = api.verify_captured(3.0)

    assert seen == [int(1.5 * 16000)], "必须只把尾部 verify_seconds 秒送进门"
    assert result["window_s"] == pytest.approx(1.5)
    # 尾部那一段没有静音，所以质量读数也该是尾部的 —— 报整段会把静音算进 rms。
    assert result["rms"] > 0.1


# ------------------------------------------------- 2026-08-29 实机报的两个崩溃
#
# 两条都是「全量测试绿着,而按钮一点就炸」——原因相同:既有用例只走到 ApiError 的早退
# 分支,没有一条真的走到发请求 / 起子进程那一步。所以这两条测试的价值不在断言本身,
# 在于它们**走完整条路**。


def test_models_try_actually_posts_a_chat_request(api):
    """「试一句」此前必炸:`models_try` 调 `_chat_request` / `_chat_reply` / `CHAT_TIMEOUT_S`,
    而这三个名字**都不存在** —— 点一下换回 `console failed: NameError`。

    这条测试起一个真的 HTTP 服务器,所以它必然走到构造请求那一步。
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[tuple[str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名
            length = int(self.headers.get("content-length") or 0)
            seen.append((self.path, _json.loads(self.rfile.read(length) or b"{}")))
            body = _json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A003 - 静音
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = api.models_try(
            "llm", "", f"http://127.0.0.1:{server.server_port}/v1", "", "openai", "test-model"
        )
        path, payload = seen[0]
        assert path == "/v1/chat/completions"
        assert payload["model"] == "test-model"
        assert payload["max_tokens"] == 16, "试一句要最小:一句 ping、16 个 token"
        assert payload["stream"] is False
        assert out["reply"] == "pong"
        assert out["status"] == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_the_anthropic_shape_uses_messages_and_content():
    """两种协议的形状不同,写错哪一层都是 400,而 400 会让「密钥不对」和「请求不对」
    在界面上看起来一样。"""
    from core.console.routes import _chat_reply, _chat_request

    path, body = _chat_request("anthropic", "claude-opus-5")
    assert path == "/messages"
    assert body["max_tokens"] == 16
    assert "stream" not in body, "Anthropic 这条路不发 stream 键"
    assert _chat_reply({"content": [{"type": "text", "text": "你好"}]}) == "你好"
    assert _chat_reply({"choices": [{"message": {"content": " pong "}}]}) == "pong"
    # 认不出的形状返回空字符串而不是抛 —— 调用方要把「通了但读不出」和「没通」分开报。
    assert _chat_reply({"weird": True}) == ""
    assert _chat_reply("not a mapping") == ""


def test_test_agent_calls_stream_not_run(runtime):
    """契约里的方法是 `stream`。此前这里写的是 `adapter.run(task)`,于是每次「试跑」
    都换回 `AttributeError: 'CliAgentAdapter' object has no attribute 'run'`,
    而外层 except 把它记成「受阻」并显示「claude 没答」——
    一个把自己的拼写错误报成「agent 受阻」的探针比没有探针更糟:它指向了错的地方。
    """
    from core.agents.contract import AgentChunk

    class Adapter:
        def __init__(self) -> None:
            self.streamed = 0

        def stream(self, task):
            del task
            self.streamed += 1
            yield AgentChunk(kind="text", text="在的")
            yield AgentChunk(kind="done", elapsed_ms=12)

        def run(self, task):  # 故意留着:如果实现回退到 run,这条测试要能发现
            raise AssertionError("不该调 run —— 契约里的方法是 stream")

    adapter = Adapter()
    runtime.adapters = {"claude": adapter}
    out = ConsoleApi(runtime).test_agent("claude", "你好")
    assert adapter.streamed == 1
    assert out["ok"] is True
    assert out["text"] == "在的"
    assert out["error"] == ""


def test_the_first_enrollment_can_be_done_from_the_console(runtime, monkeypatch):
    """使用者 2026-08-31：「我希望在控制台能进行全部的设置，包括第一次录制声纹」。

    此前那是个**死锁**：声纹门 fail-closed，一个人都没注册就不许开麦；而控制台注册要从
    采集缓冲取音频 —— 于是第一次注册只能先开终端跑脚本。一个必须先开终端才能用的产品
    配不上「成熟」两个字。

    现在开的是**注册模式**：设备开着、缓冲照常填，但 `enroll_only` 让唤醒判定永久按住，
    `_authorise` 一次都不会被调到。不许绕过的那条断言（唤醒不经校验不许通过）仍然成立 ——
    `tests/test_speaker_privacy.py` 那一条钉的就是它。
    """
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    tone = (np.sin(np.linspace(0, 900.0, 3 * 16000)) * 0.3).astype(np.float32)
    ring = StubRing(tone)
    ring.enroll_only = True
    gate = StubGate()
    api = ConsoleApi(runtime, SimpleNamespace(capture=ring, verifier=gate))

    view = api.speaker_view()
    assert view["mic_running"] is False, "页面据此显示「开麦克风」那颗按钮"
    assert view["enroll_only"] is True

    api.mic_running = True  # 页面点了「开麦克风」
    api.capture_clip(3.0)
    api.capture_clip(3.0)
    result = api.enroll_captured("du")

    assert gate.enrolled == [("du", 2)]
    assert result["audio_saved"] is False


# ------------------------------------------- 微信那一栏（扫码 + 实时收发，2026-09-03）


@pytest.fixture
def weixin_api(tmp_path, monkeypatch):
    monkeypatch.setenv("VOX_WEIXIN_CREDENTIALS", str(tmp_path / "weixin.json"))
    return ConsoleApi(runtime=None, stack=None)


def test_the_weixin_panel_says_unbound_before_anyone_scans(weixin_api):
    view = weixin_api.weixin_view()

    assert view["credentials"]["bound"] is False
    assert view["runner"] is None, "通道没起来就该如实说没起来"
    assert view["chats"] == []


def test_polling_before_starting_a_login_is_refused(weixin_api):
    with pytest.raises(ApiError, match="还没开始"):
        weixin_api.weixin_login_poll()


def test_a_login_renders_the_qr_on_the_server(weixin_api, monkeypatch):
    """二维码在**服务端**渲成 SVG。一个从 CDN 拉 JS 二维码库的登录页等于把登录流程交给
    第三方，而这一页上过的是使用者的微信账号。

    这里替换的是 ``QrLogin`` 整个类，不是它的 transport 字段：``field(default_factory=
    HttpTransport)`` 在**类定义时**就把那个函数对象绑好了，改模块属性影响不到它 ——
    第一版这么写的后果是这条测试**真的打了微信的接口**（而且拿回了一张真二维码）。
    """
    import core.channels.weixin_login as login

    class FakeTransport:
        def __init__(self) -> None:
            self.script = [
                {"qrcode": "hex1", "qrcode_img_content": "https://ilinkai.weixin.qq.com/l?t=1"},
                {
                    "status": "confirmed",
                    "ilink_bot_id": "bot-123456789",
                    "bot_token": "tk-" + "z" * 30,
                    "baseurl": "https://ilinkai.weixin.qq.com",
                },
            ]

        def get_json(self, url, headers, timeout_s):
            del url, headers, timeout_s
            return self.script.pop(0)

    class OfflineQrLogin(login.QrLogin):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", FakeTransport())
            super().__init__(**kwargs)

    monkeypatch.setattr(login, "QrLogin", OfflineQrLogin)

    first = weixin_api.weixin_login()
    assert first["svg"].startswith("<?xml")
    assert first["status"] == "wait"

    done = weixin_api.weixin_login_poll()
    assert done["status"] == "confirmed"
    # **token 的值不能出现在返回给页面的任何地方。** 这一页会被截图。
    assert "tk-" + "z" * 30 not in json.dumps(done, ensure_ascii=False)
    assert weixin_api.weixin_view()["credentials"]["bound"] is True
    # 绑上之后会话要丢掉，否则下一次点「扫码登录」拿到一个用过的票据。
    assert weixin_api._weixin_login is None


def test_unbinding_does_not_silently_turn_the_channel_off(weixin_api):
    """解绑只删凭据。``enabled`` 是配置 —— 改它要走配置那条路并重启，
    而一个悄悄改了配置文件的按钮会让「为什么重启后不收消息了」变成一次考古。"""
    before = weixin_api.weixin_view()["enabled"]

    result = weixin_api.weixin_unbind()

    assert result["credentials"]["bound"] is False
    assert weixin_api.weixin_view()["enabled"] == before


def test_sending_without_a_running_channel_says_so_instead_of_failing_silently(weixin_api):
    with pytest.raises(ApiError, match="没在跑"):
        weixin_api.weixin_send("chat-1", "在吗")


def test_the_message_cursor_only_returns_new_entries(weixin_api):
    """页面每两秒问一次。重复给会让同一条消息在界面上出现好几遍。"""
    from core.channels.runner import ChannelRunner

    runner = ChannelRunner(channel=SimpleNamespace(name="weixin"), runtime=None)
    runner._record("in", chat_id="c1", text="第一条")
    weixin_api.channel_runner = runner

    first = weixin_api.weixin_messages()
    assert [row["text"] for row in first["entries"]] == ["第一条"]
    assert first["running"] is True

    assert weixin_api.weixin_messages(first["next"])["entries"] == []
    runner._record("in", chat_id="c1", text="第二条")
    again = weixin_api.weixin_messages(first["next"])
    assert [row["text"] for row in again["entries"]] == ["第二条"]


# ------------------------------------- 日志：能实时看「所有」日志（2026-09-03）


def test_the_log_ring_holds_enough_for_a_real_investigation():
    """使用者要「能实时查看所有日志」。500 条在一次认真的排查里大约十几分钟 ——
    一次唤醒失败的完整上下文（漏斗 + 声纹分数 + 派发 + 工具参数）能占掉几十条。"""
    from core.console.logbook import MAX_ENTRIES

    assert MAX_ENTRIES >= 2000


def test_the_level_filter_means_at_least_this_level():
    """选 warn 的人要的是「有问题的那些」，而 error 比 warn 更有问题 ——
    一个把 error 滤掉的 warn 筛选是个陷阱。"""
    from core.console.logbook import Logbook

    book = Logbook()
    book.write("turn", "普通一轮")
    book.write("wake", "声纹差一点", level="warn")
    book.write("tool", "拒绝了", level="error")

    assert len(book.read(level="warn")["entries"]) == 2
    assert len(book.read(level="error")["entries"]) == 1
    assert len(book.read()["entries"]) == 3


def test_the_search_looks_inside_the_fields_too():
    """「`fs.read` 收到的 path 到底是什么」这个问题的答案在**字段**里，
    而那正是这份日志存在的理由。"""
    from core.console.logbook import Logbook

    book = Logbook()
    book.write("tool", "fs.read 被拒", level="error", path="C:/keys/id_rsa")
    book.write("turn", "第 1 轮：读一下 README")

    hits = book.read(query="id_rsa")["entries"]

    assert [row["message"] for row in hits] == ["fs.read 被拒"]


def test_the_cursor_does_not_depend_on_the_filter():
    """**这是这一层最容易错的地方。** 先滤再取窗的话，``next`` 会停在最后一条*匹配*的条目
    上；于是被滤掉的每次轮询都重新扫，而一旦当前条件下再没有新条目，游标就永远不动 ——
    表现是「日志卡住不更新」。"""
    from core.console.logbook import Logbook

    book = Logbook()
    book.write("tool", "错误一条", level="error")
    for index in range(5):
        book.write("turn", f"普通 {index}")

    filtered = book.read(level="error")
    unfiltered = book.read()

    assert filtered["next"] == unfiltered["next"] == 6
    # 用那个游标再问一次：两边都该是空的，而不是把前五条又扫一遍。
    assert book.read(filtered["next"], level="error")["entries"] == []


def test_the_source_list_is_reported_so_the_page_can_build_a_dropdown():
    """一个要人手打 `weixin` 的筛选框没人会用。"""
    from core.console.logbook import Logbook

    book = Logbook()
    book.write("weixin", "收到一条")
    book.write("wake", "命中")

    assert book.read()["sources"] == ["wake", "weixin"]


def test_the_route_passes_the_filters_through():
    """接线：查询参数要真的到 logbook。漏接的症状是筛选框点了没反应。"""
    api = ConsoleApi(runtime=None, stack=None)
    api.logbook.write("tool", "拒绝", level="error", path="C:/x")
    api.logbook.write("turn", "普通一轮")

    assert len(api.log_view(level="error")["entries"]) == 1
    assert len(api.log_view(source="turn")["entries"]) == 1
    assert len(api.log_view(query="C:/x")["entries"]) == 1


# ------------------------------ 网站与放歌模板可从页面改（2026-09-03）


#: 出厂的 tools.toml。测试拷一份到 tmp 再改 —— 直接改它等于跑一次测试就动了使用者的配置。
SHIPPED_TOOLS = Path(__file__).resolve().parents[1] / "config" / "tools.toml"


@pytest.fixture
def sites_api(tmp_path):
    """拷一份 tools.toml 到 tmp —— 不隔离的话跑一次测试就会改使用者真的配置。"""
    shutil.copy(SHIPPED_TOOLS, tmp_path / "tools.toml")
    return ConsoleApi(runtime=None, stack=None, config_dir=tmp_path)


def test_a_site_can_be_added_from_the_page(sites_api):
    """使用者的标准：「web 界面应该可以对 vox 进行全范围的配置修改」。
    `apps.sites` 此前是审计脚本里的一条**已知缺口** —— 它是表不是标量，`set_scalars` 写不了。"""
    result = sites_api.sites_save("sites", "小红书", "https://www.xiaohongshu.com/")

    assert result["sites"]["小红书"] == "https://www.xiaohongshu.com/"
    # 别的条目不能被碰掉。
    assert result["sites"]["B站"] == "https://www.bilibili.com/"


def test_an_existing_chinese_key_is_updated_in_place_not_duplicated(sites_api):
    """`[apps.sites]` 的键是中文，在 TOML 里是**引号键**。只认裸键的写入器会以为它不存在，
    然后在下面插一条重复的 —— 而 TOML 里重复键是解析错误，所以症状是「保存失败」。"""
    sites_api.sites_save("sites", "抖音", "https://www.douyin.com/discover")

    text = (sites_api.config_dir / "tools.toml").read_text(encoding="utf-8")
    assert text.count('"抖音"') == 1
    parsed = tomllib.loads(text)
    assert parsed["apps"]["sites"]["抖音"] == "https://www.douyin.com/discover"


def test_the_comments_survive_a_site_edit(sites_api):
    """这个文件里的注释是它一半的价值。"""
    sites_api.sites_save("sites", "抖音", "https://www.douyin.com/")

    text = (sites_api.config_dir / "tools.toml").read_text(encoding="utf-8")
    assert "我习惯使用网页版刷视频" in text


def test_a_non_http_target_is_refused(sites_api):
    """这两张表放开的前提是它们**只产出一个浏览器要打开的地址**。一个 `file://` 或者
    自定义协议就不是那件事了。"""
    for bad in ("file:///C:/Windows/System32", "orpheus://search/x", "javascript:alert(1)"):
        with pytest.raises(ApiError):
            sites_api.sites_save("sites", "坏的", bad)


def test_a_play_template_must_have_a_query_placeholder(sites_api):
    with pytest.raises(ApiError, match=r"\{q\}"):
        sites_api.sites_save("play", "网易云音乐", "https://music.163.com/")


def test_a_play_template_is_accepted_with_the_placeholder(sites_api):
    result = sites_api.sites_save(
        "play", "洛雪音乐", "https://example.com/search?k={q}"
    )

    assert result["play"]["洛雪音乐"] == "https://example.com/search?k={q}"


def test_only_these_two_tables_are_reachable(sites_api):
    """**`apps.entries` 不在这个入口的射程内。** 它是「名字 → 可执行文件绝对路径」，
    让网页往里加一条等于给它代码执行。"""
    with pytest.raises(ApiError, match="kind"):
        sites_api.sites_save("entries", "后门", "https://example.com/")


def test_a_site_can_be_deleted(sites_api):
    sites_api.sites_save("sites", "小红书", "https://www.xiaohongshu.com/")

    result = sites_api.sites_delete("sites", "小红书")

    assert "小红书" not in result["sites"]
    assert result["sites"]["B站"], "删一条把别的删掉了"


def test_deleting_something_that_is_not_there_says_so(sites_api):
    with pytest.raises(ApiError, match="没有"):
        sites_api.sites_delete("sites", "从来没有过的")


def test_the_view_reports_what_this_machine_can_actually_open(sites_api):
    """放歌模板的键必须对上一个**真的能开的**应用 —— 配一个不存在的名字，
    那条模板永远不会被用到，而页面上看不出来。"""
    view = sites_api.sites_view()

    assert isinstance(view["discovered"], list)
    assert view["entries"], "白名单里那几个也要报出来"


def test_every_nav_link_has_a_view_and_a_section():
    """**点了「微信」却跳到运行态** —— 使用者 2026-09-03 报的。

    真因是三处要同时改而我只改了两处：加了 `<a data-view="weixin">`、加了
    `<section id="v-weixin">`，但**忘了把 `weixin` 加进 `VIEWS` 数组** ——
    而 `setView` 是 `VIEWS.includes(name) ? name : "overview"`，所以它静默落回运行态。

    这一条把「三处必须一致」变成一个断言：导航链接、`VIEWS`、`<section id="v-...">`。
    """
    import re

    html = (Path(__file__).resolve().parents[1] / "core/console/static/index.html").read_text(
        encoding="utf-8"
    )
    nav = set(re.findall(r'<a href="#[^"]*" data-view="([^"]+)"', html))
    listed = set(re.findall(r'const VIEWS = \[([^\]]+)\]', html)[0].replace('"', "").split(", "))
    sections = set(re.findall(r'<section class="view" id="v-([^"]+)"', html))

    assert nav, "一个导航链接都没找到 —— 这条测试的正则跟着标记漂了"
    assert nav - listed == set(), f"导航里有但 VIEWS 里没有（会静默落回运行态）：{nav - listed}"
    assert nav - sections == set(), f"导航里有但没有对应的 section：{nav - sections}"
    assert listed - sections == set(), f"VIEWS 里有但没有 section：{listed - sections}"
