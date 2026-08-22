import json
from pathlib import Path

import pytest

from core.state import VoiceState
from vox_plugin import VoicePlugin


class StubTransport:
    def __init__(self):
        self.sent = []
        self.cancelled = []

    def send(self, text, *, session_id=None):
        self.sent.append(text)
        return {"turn_id": "t-1", "reply": "pong"}

    def cancel(self, turn_id):
        self.cancelled.append(turn_id)
        return {"cancelled": turn_id}


class StubCapture:
    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("capture failed")

    def stop(self):
        self.stops += 1


def test_pause_blocks_wake_and_resume_restores():
    plugin = VoicePlugin()
    plugin.start()
    plugin.pause()
    with pytest.raises(RuntimeError, match="paused"):
        plugin.wake_detected("wake", 0.9)
    plugin.resume()
    assert plugin.wake_detected("wake", 0.9)[0]["type"] == "wake.detected"


def test_pause_requires_running():
    with pytest.raises(RuntimeError):
        VoicePlugin().pause()


def test_capture_follows_plugin_lifecycle():
    capture = StubCapture()
    plugin = VoicePlugin(audio_capture=capture)
    plugin.start()
    plugin.pause()
    plugin.resume()
    plugin.stop()
    assert capture.starts == 2
    assert capture.stops == 2


def test_capture_start_failure_rolls_back_running_state():
    plugin = VoicePlugin(audio_capture=StubCapture(fail_start=True))
    with pytest.raises(RuntimeError, match="capture failed"):
        plugin.start()
    assert plugin.running is False


def test_wake_test_marks_synthetic():
    plugin = VoicePlugin()
    plugin.start()
    events = plugin.wake_test()
    assert events[0]["type"] == "wake.detected"
    assert events[0]["payload"]["synthetic"] is True


def test_complete_turn_cycle_matches_contract():
    schema = json.loads(Path("contracts/voice-events.schema.json").read_text(encoding="utf-8"))
    allowed = set(schema["properties"]["type"]["enum"])
    plugin = VoicePlugin()
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    plugin.submit_text("你好")
    events = plugin.complete_turn("你好，我在")
    assert [e["type"] for e in events] == [
        "llm.delta", "tts.chunk", "state.changed", "turn.done", "state.changed"
    ]
    assert all(e["type"] in allowed for e in events)
    assert plugin.machine.state == VoiceState.LISTENING


def test_complete_turn_requires_thinking():
    plugin = VoicePlugin()
    plugin.start()
    with pytest.raises(RuntimeError):
        plugin.complete_turn("reply")


def test_transport_send_and_cancel_wiring():
    transport = StubTransport()
    plugin = VoicePlugin(transport=transport)
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    plugin.submit_text("hello")
    assert transport.sent == ["hello"]
    assert plugin.last_turn_id == "t-1"
    assert plugin.last_reply == "pong"
    plugin.cancel()
    assert transport.cancelled == ["t-1"]
    assert plugin.last_turn_id is None


def test_diagnose_reports_without_leaking_token(monkeypatch):
    monkeypatch.setenv("VOX_VOICE_BRIDGE_TOKEN", "super-secret-value")
    plugin = VoicePlugin()
    report = plugin.diagnose()
    assert report["bridge"]["token_configured"] is True
    assert "super-secret-value" not in json.dumps(report, ensure_ascii=False)
    assert set(report) >= {"local_voice", "provider", "bridge", "transport_attached", "audio_backend"}
    assert report["local_voice"]["kws_model_ready"] is True
    assert report["local_voice"]["vad_model_ready"] is True


def test_devices_reports_availability():
    report = VoicePlugin().devices()
    assert "available" in report and "inputs" in report
    if not report["available"]:
        assert "reason" in report


# -- memory wiring (P3) ------------------------------------------------------


@pytest.fixture()
def wired(tmp_path):
    """A running plugin with a real memory writer over a temporary database."""
    from core.memory import MemoryRecaller, MemoryWriter, SqliteMemoryStore

    store = SqliteMemoryStore(tmp_path / "memory.db")
    writer = MemoryWriter(store, facts_dir=tmp_path / "facts", session_id="s1")
    plugin = VoicePlugin()
    plugin.attach_memory(writer, MemoryRecaller(store))
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    yield plugin, store, writer
    store.close()


def test_a_turn_is_remembered_on_both_sides(wired):
    plugin, store, _writer = wired
    plugin.submit_text("今天天气怎么样")
    plugin.complete_turn("今天晴")

    records = list(reversed(store.list_records(scope="short")))
    assert [record.text for record in records] == ["今天天气怎么样", "今天晴"]
    assert "role:user" in records[0].tags
    assert "role:assistant" in records[1].tags


def test_memory_is_opt_in(tmp_path):
    """No attach, no database: an unasked-for store must not appear on disk."""
    plugin = VoicePlugin()
    assert plugin.attach_memory() == {"memory_attached": False}
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    plugin.submit_text("你好")
    plugin.complete_turn("我在")

    assert list(tmp_path.iterdir()) == []
    assert plugin.machine.state == VoiceState.LISTENING


def test_a_broken_writer_cannot_break_the_conversation():
    class ExplodingWriter:
        def write_turn(self, text, *, role="user"):
            raise RuntimeError("database is locked")

    plugin = VoicePlugin()
    plugin.attach_memory(ExplodingWriter())
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    plugin.submit_text("你好")

    assert plugin.complete_turn("我在")[-1]["payload"]["to"] == "listening"


def test_a_credential_utterance_never_reaches_the_store(wired):
    """FR-12.6 through the real voice path, not just the writer's own unit test."""
    plugin, store, writer = wired
    plugin.submit_text("我的 key 是 sk-abcdefghijklmnopqrstuvwxyz012345")

    assert store.count() == 0
    assert writer.refusals == 1


def test_diagnose_reports_memory_counts_and_no_text(wired):
    plugin, _store, _writer = wired
    plugin.submit_text("我住在北京")
    report = plugin.diagnose()["memory"]

    assert report["attached"] is True
    assert report["records"] == 1
    assert report["by_scope"]["short"] == 1
    assert report["warnings"] == []
    assert "北京" not in json.dumps(report, ensure_ascii=False)


def test_diagnose_warns_when_memory_is_absent():
    report = VoicePlugin().diagnose()["memory"]
    assert report["attached"] is False
    assert report["warnings"]


# -- tool wiring (P4) --------------------------------------------------------


@pytest.fixture()
def toolset(tmp_path):
    """A plugin with the tool gate attached and rooted at a temporary directory."""
    from core.tools import open_tools

    config = {
        "fs": {
            "enabled": True,
            "roots": [str(tmp_path)],
            "max_bytes": 1024,
            "denied_names": [".env"],
            "denied_dirs": ["memory"],
        },
        "web": {"enabled": True, "blocked_domains": [], "max_results": 3,
                "snippet_chars": 80},
        "shell": {"enabled": False, "allow": ["git status"],
                  "require_confirmation": True, "require_verified_speaker": True,
                  "timeout_s": 5, "max_output_bytes": 500},
    }
    plugin = VoicePlugin()
    plugin.attach_tools(open_tools(config))
    return plugin, tmp_path, config


def test_a_tool_runs_through_the_plugin(toolset):
    plugin, root, _config = toolset
    (root / "notes.md").write_text("第一段", encoding="utf-8")

    result = plugin.run_tool("fs.read", {"path": "notes.md"})

    assert result["ok"] is True
    assert result["output"] == "第一段"


def test_tools_are_opt_in(tmp_path):
    """No attach, no capability: the platform can still talk, and only talk."""
    plugin = VoicePlugin()
    assert plugin.attach_tools() == {"tools_attached": False}
    assert plugin.run_tool("fs.read", {"path": "x.md"})["ok"] is False


def test_the_plugin_does_not_widen_the_sandbox(toolset):
    plugin, _root, _config = toolset
    result = plugin.run_tool("fs.read", {"path": "../outside.md"})
    assert (result["ok"], result["error"]) == (False, "path is outside the sandbox")


def test_the_plugin_supplies_no_speaker_of_its_own(toolset):
    """``shell.run`` demands a verified name; the plugin must not invent one."""
    plugin, _root, config = toolset
    from core.tools import open_tools

    config = {section: dict(values) for section, values in config.items()}
    config["shell"]["enabled"] = True
    plugin.attach_tools(open_tools(config))

    result = plugin.run_tool("shell.run", {"command": "git status", "confirmed": True})

    assert (result["ok"], result["error"]) == (False, "no verified speaker")


def test_a_confirmation_is_surfaced_rather_than_swallowed(toolset):
    plugin, _root, config = toolset
    from core.tools import open_tools

    config = {section: dict(values) for section, values in config.items()}
    config["shell"]["enabled"] = True
    plugin.attach_tools(open_tools(config))

    result = plugin.run_tool(
        "shell.run", {"command": "git status"}, speaker="due"
    )

    assert result["needs_confirmation"] is True
    assert result["ok"] is False


def test_diagnose_reports_the_gate_without_naming_arguments(toolset):
    plugin, root, _config = toolset
    (root / "notes.md").write_text("x", encoding="utf-8")
    plugin.run_tool("fs.read", {"path": "notes.md"})
    plugin.run_tool("fs.read", {"path": "../outside.md"})

    report = plugin.diagnose()["tools"]

    assert report["attached"] is True
    assert report["registered"] == ["fs.read", "web.search"]
    assert report["executed"] == 1
    assert report["refused"] == 1
    assert report["shell_enabled"] is False
    assert report["warnings"] == []
    assert "notes.md" not in json.dumps(report, ensure_ascii=False)


def test_diagnose_warns_when_the_shell_is_switched_on(toolset):
    plugin, _root, config = toolset
    from core.tools import open_tools

    config = {section: dict(values) for section, values in config.items()}
    config["shell"]["enabled"] = True
    plugin.attach_tools(open_tools(config))

    report = plugin.diagnose()["tools"]

    assert report["shell_enabled"] is True
    assert any("shell.run is enabled" in warning for warning in report["warnings"])


def test_diagnose_warns_when_tools_are_absent():
    report = VoicePlugin().diagnose()["tools"]
    assert report["attached"] is False
    assert report["warnings"]
