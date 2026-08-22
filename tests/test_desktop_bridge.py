"""The Python -> desktop channel: transport, not rendering.

Every test here runs a **real child process** -- a small Python script standing in
for the orb -- because the properties worth pinning are the pipe's, not a mock's:
a line arrives whole, a dead child does not raise, a confirmation that never comes
back counts as refused.

That child is a stand-in, so these are SIM. The orb is a Tauri window and the
translation from envelope to visible state lives in TypeScript; neither is
exercised here. REAL-WIN stays open.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from core.desktop_bridge import DesktopBridge, DesktopBridgeError, find_desktop_binary
from core.events import build_event, validate_any_event

# A stand-in orb: echoes what it was told so the test can assert on the wire
# format, and answers confirmations according to a rule passed on argv.
FAKE_ORB = textwrap.dedent(
    """
    import json, sys
    # 真的球是 Rust，`stdin.lock().lines()` 按定义就是 UTF-8。Python 替身默认按
    # 本地代码页（Windows 上是 GBK）解码，不钉住就会把中文正文解坏 —— 那是替身的
    # 缺陷，不是这条管道的。
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    mode = sys.argv[1] if len(sys.argv) > 1 else "silent"
    print(json.dumps({"kind": "ready"}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        print(json.dumps({"kind": "echo", "got": msg}), flush=True)
        event = msg.get("event") or {}
        if msg.get("kind") == "event" and event.get("type") == "tool.confirm_required":
            if mode == "approve":
                print(json.dumps({"kind": "confirm", "id": event["id"], "approved": True}), flush=True)
            elif mode == "deny":
                print(json.dumps({"kind": "confirm", "id": event["id"], "approved": False}), flush=True)
            elif mode == "garbage":
                print(json.dumps({"kind": "confirm", "id": event["id"], "approved": "yes"}), flush=True)
    """
)


@pytest.fixture
def orb_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_orb.py"
    script.write_text(FAKE_ORB, encoding="utf-8")
    return script


def bridge_for(script: Path, mode: str = "silent", **kwargs) -> DesktopBridge:
    return DesktopBridge([sys.executable, str(script), mode], **kwargs)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestLifecycle:
    def test_ready_arrives_before_anything_is_sent(self, orb_script: Path) -> None:
        # 父进程凭 ready 知道管道通了，而不是猜一个启动延时
        with bridge_for(orb_script) as bridge:
            assert bridge.ready.wait(5.0)
            assert bridge.alive

    def test_start_is_idempotent(self, orb_script: Path) -> None:
        with bridge_for(orb_script) as bridge:
            bridge.start()
            bridge.start()
            assert bridge.alive

    def test_close_is_idempotent_and_stops_the_child(self, orb_script: Path) -> None:
        bridge = bridge_for(orb_script)
        bridge.start()
        assert bridge.ready.wait(5.0)
        bridge.close()
        bridge.close()
        assert not bridge.alive

    def test_a_missing_binary_is_an_error_only_when_asked_to_start(self) -> None:
        bridge = DesktopBridge(["definitely-not-a-real-binary-xyz"])
        with pytest.raises(DesktopBridgeError):
            bridge.start()

    def test_find_binary_returns_none_rather_than_raising(self, tmp_path: Path) -> None:
        # 无头运行是完全正常的状态，不该由这个查找函数来否决
        assert find_desktop_binary(tmp_path) is None


class TestOutward:
    def test_an_envelope_reaches_the_child_intact(self, orb_script: Path) -> None:
        received: list[dict] = []
        with bridge_for(orb_script, on_incoming=received.append) as bridge:
            assert bridge.ready.wait(5.0)
            event = build_event("task.done", {"task_id": "t1", "ok": True})
            assert bridge.send(event) is True
            assert wait_for(lambda: any(m.get("kind") == "echo" for m in received))
            echoed = next(m for m in received if m.get("kind") == "echo")
            assert echoed["got"]["event"] == event

    def test_chinese_survives_the_wire(self, orb_script: Path) -> None:
        received: list[dict] = []
        with bridge_for(orb_script, on_incoming=received.append) as bridge:
            assert bridge.ready.wait(5.0)
            bridge.send(build_event("llm.delta", {"text": "读一下 README 的内容"}))
            assert wait_for(lambda: any(m.get("kind") == "echo" for m in received))
            echoed = next(m for m in received if m.get("kind") == "echo")
            assert echoed["got"]["event"]["payload"]["text"] == "读一下 README 的内容"

    def test_a_newline_in_the_payload_cannot_split_the_line(self, orb_script: Path) -> None:
        # 一行一条是这个协议的全部前提；正文里的换行必须被 JSON 转义掉
        received: list[dict] = []
        with bridge_for(orb_script, on_incoming=received.append) as bridge:
            assert bridge.ready.wait(5.0)
            bridge.send(build_event("llm.delta", {"text": "a\nb\nc"}))
            assert wait_for(lambda: any(m.get("kind") == "echo" for m in received))
            echoes = [m for m in received if m.get("kind") == "echo"]
            assert len(echoes) == 1
            assert echoes[0]["got"]["event"]["payload"]["text"] == "a\nb\nc"

    def test_a_malformed_envelope_is_dropped_not_raised(self, orb_script: Path) -> None:
        with bridge_for(orb_script) as bridge:
            assert bridge.ready.wait(5.0)
            # 拼错的类型名在这里被拦下，而不是变成 UI 静默不动
            assert bridge.send({"version": "1", "type": "task.completed", "id": "x",
                                "timestamp": "2026-08-11T00:00:00+00:00"}) is False
            assert bridge.dropped == 1
            assert bridge.sent == 0

    def test_writing_to_a_dead_child_counts_instead_of_raising(self, orb_script: Path) -> None:
        # 管道断了是一次正常的结束，不该把一轮对话带走
        bridge = bridge_for(orb_script)
        bridge.start()
        assert bridge.ready.wait(5.0)
        bridge.close()
        assert bridge.send(build_event("turn.done", {})) is False
        assert bridge.dropped >= 1

    def test_send_matches_the_five_sink_shape(self, orb_script: Path) -> None:
        # 单个已验证信封、单个位置参数 —— 与 tool runner / 记忆 / dispatcher 同形
        with bridge_for(orb_script) as bridge:
            assert bridge.ready.wait(5.0)
            sink = bridge.send
            sink(build_event("memory.written", {"id": "m1", "scope": "mid"}))
            assert bridge.sent == 1


class TestConfirmation:
    def _request(self) -> dict:
        return validate_any_event(
            build_event(
                "tool.confirm_required",
                {"tool": "shell.run", "origin": "voice", "command": "git status"},
            )
        )

    def test_an_explicit_approval_is_the_only_true(self, orb_script: Path) -> None:
        with bridge_for(orb_script, "approve") as bridge:
            assert bridge.ready.wait(5.0)
            assert bridge.await_confirmation(self._request(), timeout_s=5.0) is True

    def test_a_denial_is_false(self, orb_script: Path) -> None:
        with bridge_for(orb_script, "deny") as bridge:
            assert bridge.ready.wait(5.0)
            assert bridge.await_confirmation(self._request(), timeout_s=5.0) is False

    def test_silence_times_out_as_refused(self, orb_script: Path) -> None:
        # 走开不看的确认卡等于没同意
        with bridge_for(orb_script, "silent") as bridge:
            assert bridge.ready.wait(5.0)
            started = time.monotonic()
            assert bridge.await_confirmation(self._request(), timeout_s=0.3) is False
            assert time.monotonic() - started < 3.0

    def test_a_truthy_string_is_not_an_approval(self, orb_script: Path) -> None:
        # 与 policy.py 同一条规则：必须 is True，不是「真值」。
        # "yes" 是个真值字符串，按真值判断等于替用户点了允许。
        with bridge_for(orb_script, "garbage") as bridge:
            assert bridge.ready.wait(5.0)
            assert bridge.await_confirmation(self._request(), timeout_s=2.0) is False

    def test_a_dead_bridge_refuses_rather_than_hangs(self, orb_script: Path) -> None:
        bridge = bridge_for(orb_script, "approve")
        bridge.start()
        assert bridge.ready.wait(5.0)
        bridge.close()
        assert bridge.await_confirmation(self._request(), timeout_s=5.0) is False

    def test_the_child_dying_mid_wait_settles_as_refused(self, orb_script: Path) -> None:
        # 卡还开着就崩了 —— 调用方必须被放行，且是往拒绝一侧放
        bridge = bridge_for(orb_script, "silent")
        bridge.start()
        assert bridge.ready.wait(5.0)
        import threading

        threading.Timer(0.2, bridge.close).start()
        assert bridge.await_confirmation(self._request(), timeout_s=10.0) is False

    def test_an_envelope_without_an_id_cannot_be_answered(self, orb_script: Path) -> None:
        with bridge_for(orb_script, "approve") as bridge:
            assert bridge.ready.wait(5.0)
            assert bridge.await_confirmation({"type": "tool.confirm_required"}) is False

    def test_hiding_settles_pending_confirmations_as_refused(self, orb_script: Path) -> None:
        # 隐藏一张挂起的卡等于让调用方永久挂起，而「挂起」在安全语义上等价于「未拒绝」
        bridge = bridge_for(orb_script, "silent")
        bridge.start()
        assert bridge.ready.wait(5.0)
        import threading

        threading.Timer(0.2, lambda: bridge.set_visible(False)).start()
        assert bridge.await_confirmation(self._request(), timeout_s=10.0) is False
        bridge.close()

    def test_pending_count_returns_to_zero(self, orb_script: Path) -> None:
        with bridge_for(orb_script, "deny") as bridge:
            assert bridge.ready.wait(5.0)
            bridge.await_confirmation(self._request(), timeout_s=5.0)
            assert bridge.describe()["pending_confirmations"] == 0


class TestDescribe:
    def test_describe_carries_counts_not_content(self, orb_script: Path) -> None:
        with bridge_for(orb_script) as bridge:
            assert bridge.ready.wait(5.0)
            bridge.send(build_event("tool.confirm_required", {
                "tool": "shell.run", "origin": "voice", "command": "git status"}))
            report = bridge.describe()
            assert set(report) == {
                "running", "ready", "sent", "dropped",
                "pending_confirmations", "confirm_timeout_s",
            }
            # 命令原文属于确认卡，不属于诊断
            assert "git status" not in json.dumps(report)


class TestPluginSink:
    """``VoicePlugin`` 的产出端。之前 `_event()` 只 append，没有第二个出口。"""

    def _plugin(self):
        from vox_plugin import VoicePlugin

        seen: list[dict] = []
        return VoicePlugin(on_event=seen.append), seen

    def test_a_whole_turn_reaches_the_sink(self) -> None:
        plugin, seen = self._plugin()
        plugin.start()
        plugin.wake_detected("你好问问", 0.82)
        plugin.submit_text("读一下 README.md")
        plugin.complete_turn("好的")
        types = [e["type"] for e in seen]
        assert "wake.detected" in types
        assert "asr.final" in types
        assert "llm.delta" in types
        assert "turn.done" in types

    def test_state_transitions_reach_the_sink_too(self) -> None:
        # 六态是球的主要视觉；状态事件走的是 machine.transition 那条路，
        # 只给 _event 装 sink 会漏掉它们
        plugin, seen = self._plugin()
        plugin.start()
        plugin.wake_detected("你好问问", 0.9)
        states = [e["payload"].get("to") for e in seen if e["type"] == "state.changed"]
        assert "listening" in states

    def test_the_sink_gets_exactly_what_the_list_gets(self) -> None:
        plugin, seen = self._plugin()
        plugin.start()
        plugin.wake_detected("你好问问", 0.8)
        plugin.submit_text("你好")
        assert seen == plugin.events

    def test_every_event_passes_the_contract(self) -> None:
        plugin, seen = self._plugin()
        plugin.start()
        plugin.wake_detected("你好问问", 0.7)
        plugin.submit_text("你好")
        plugin.complete_turn("在的")
        plugin.stop()
        for event in seen:
            validate_any_event(event)

    def test_a_raising_sink_cannot_break_a_turn(self) -> None:
        # 事件流是遥测，对话才是产品。桥断了不该让说话这件事失败。
        from vox_plugin import VoicePlugin

        def explode(_event: dict) -> None:
            raise RuntimeError("bridge is gone")

        plugin = VoicePlugin(on_event=explode)
        plugin.start()
        plugin.wake_detected("你好问问", 0.8)
        plugin.submit_text("你好")
        assert plugin.complete_turn("在的")
        assert plugin.sink_failures > 0
        assert plugin.events  # 本地记录不受影响

    def test_no_sink_is_still_the_default(self) -> None:
        from vox_plugin import VoicePlugin

        plugin = VoicePlugin()
        plugin.start()
        assert plugin.events
        assert plugin.sink_failures == 0
