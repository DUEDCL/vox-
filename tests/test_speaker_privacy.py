"""Privacy and fail-closed assertions for the speaker gate.

``tests/test_speaker.py`` covers the provider's own behaviour. This file covers
the two properties that no amount of provider testing can establish, because
they are about what the *system* does not do:

* audio never reaches the filesystem, and
* the gate never degrades into "anyone may wake it".

None of it needs the 37 MB model. That is the point -- the properties worth
asserting here are precisely the ones that must hold when the model is absent.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio import (
    AudioRingBuffer,
    ProviderUnavailable,
    SounddeviceWakeCapture,
    SpeakerVerificationProvider,
)
from core.audio.speaker import load_speaker_config
from vox_plugin import VoicePlugin

ROOT = Path(__file__).resolve().parents[1]


class StubKws:
    """A keyword provider that reports a hit on demand, without a model."""

    def __init__(self, hits: list[str] | None = None) -> None:
        self.hits = hits or []
        self.closed = False

    def load(self):
        from core.audio import ProviderStatus

        return ProviderStatus(True, "stub", {"engine": "stub"})

    def create_stream(self):
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, samples, sample_rate
        hits, self.hits = self.hits, []
        return [(keyword, None) for keyword in hits]

    def close(self):
        self.closed = True


class StubVerifier:
    """Minimal stand-in for ``SpeakerVerificationProvider``."""

    def __init__(self, *, accepted=True, score=0.91, speakers=("owner",), raises=False):
        from core.audio.speaker import VerificationResult

        self.result = VerificationResult(
            accepted, "owner" if accepted else None, score, "match" if accepted else "below threshold"
        )
        self.speakers = list(speakers)
        self.raises = raises
        self.seen: list[int] = []

    def load(self):
        from core.audio import ProviderStatus

        return ProviderStatus(True, "stub", {"dim": 192})

    def verify(self, samples, *, sample_rate=16000):
        del sample_rate
        if self.raises:
            raise RuntimeError("extractor exploded")
        self.seen.append(len(samples))
        return self.result

    def describe(self):
        return {
            "available": True,
            "model": "stub",
            "store": "stub",
            "loaded": True,
            "speakers": list(self.speakers),
            "samples_per_speaker": {name: 3 for name in self.speakers},
            "threshold": 0.5,
        }


def _capture(**kwargs):
    kwargs.setdefault("blocksize", 160)
    return SounddeviceWakeCapture(StubKws(), lambda *a: None, **kwargs)


def _block(samples: int, value: float = 0.2) -> np.ndarray:
    return np.full((samples, 1), value, dtype="float32")


# -- ring buffer ------------------------------------------------------------


def test_ring_buffer_keeps_only_the_most_recent_window():
    ring = AudioRingBuffer(sample_rate=100, seconds=1.0)
    ring.write(np.arange(60, dtype="float32"))
    ring.write(np.arange(60, 160, dtype="float32"))
    window = ring.snapshot()
    assert len(ring) == 100
    # Oldest sample first, and the first 60 values have been overwritten.
    assert window[0] == pytest.approx(60.0)
    assert window[-1] == pytest.approx(159.0)


def test_ring_buffer_survives_a_chunk_larger_than_itself():
    ring = AudioRingBuffer(sample_rate=100, seconds=0.5)
    ring.write(np.arange(500, dtype="float32"))
    window = ring.snapshot()
    assert len(window) == 50
    assert window[-1] == pytest.approx(499.0)


def test_ring_buffer_clear_drops_the_audio():
    ring = AudioRingBuffer(sample_rate=100, seconds=1.0)
    ring.write(np.ones(100, dtype="float32"))
    ring.clear()
    assert len(ring) == 0
    assert ring.snapshot().size == 0


def test_ring_buffer_has_no_filesystem_surface():
    """The class must not be able to persist audio even by accident.

    Parsed rather than grepped: the docstring legitimately talks about sockets
    and files, and a substring search would flag its own explanation.
    """
    import ast

    tree = ast.parse((ROOT / "core" / "audio" / "ring.py").read_text(encoding="utf-8"))
    forbidden = {
        "open",
        "write_text",
        "write_bytes",
        "tofile",
        "save",
        "savetxt",
        "dump",
        "dumps",
        "socket",
        "Popen",
        "run",
    }
    used = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not (used & forbidden), f"ring buffer must not touch {sorted(used & forbidden)}"
    assert imported <= {"numpy", "typing", "__future__"}, (
        f"ring buffer imports beyond its need: {sorted(imported)}"
    )


# -- audio never lands on disk ---------------------------------------------


def test_a_full_gate_cycle_writes_nothing_to_disk(tmp_path, monkeypatch):
    """Drive capture end to end and assert the working tree gained no files."""
    monkeypatch.chdir(tmp_path)
    kws = StubKws()
    verifier = StubVerifier()
    accepted: list[tuple[str, float | None]] = []
    capture = SounddeviceWakeCapture(
        kws,
        lambda keyword, score: accepted.append((keyword, score)),
        blocksize=160,
        verifier=verifier,
        verify_seconds=1.0,
    )
    for _ in range(30):
        capture._callback(_block(160), 160, None, None)
    kws.hits = ["你好问问"]
    capture._callback(_block(160), 160, None, None)

    assert accepted == [("你好问问", pytest.approx(0.91))]
    assert list(tmp_path.iterdir()) == [], "the gate must not create any file"


def test_the_verification_window_is_dropped_after_the_decision():
    kws = StubKws(["你好问问"])
    verifier = StubVerifier()
    capture = SounddeviceWakeCapture(kws, lambda *a: None, blocksize=160, verifier=verifier)
    capture._callback(_block(160), 160, None, None)
    assert verifier.seen, "the verifier should have been handed a window"
    assert len(capture._ring) == 0, "retained audio widens biometric exposure for nothing"


def test_stop_clears_retained_audio():
    capture = _capture(require_verification=False)
    capture._callback(_block(160), 160, None, None)
    assert len(capture._ring) > 0
    capture.stop()
    assert len(capture._ring) == 0


def test_recent_audio_hands_out_a_copy_not_the_buffer():
    """注册（`ConsoleApi.capture_clip`）现在从这里取音频，而它会**握着那个数组几秒钟**
    等下一段。给出视图的话，采集回调会在这几秒里从底下把它改掉 —— 注册用的样本于是
    既不是当时录的那一段，也不是任何一段完整的音频。"""
    import numpy as np

    capture = _capture(require_verification=False)
    capture._callback(_block(160, 0.5), 160, None, None)

    taken = capture.recent_audio(1.0)
    assert taken.size > 0
    taken[:] = 0.0

    assert float(np.max(np.abs(capture.recent_audio(1.0)))) > 0.0, "外面改动不许影响缓冲"
    assert capture.buffer_seconds > 0


def test_forget_recent_audio_drops_the_window():
    capture = _capture(require_verification=False)
    capture._callback(_block(160), 160, None, None)
    assert len(capture._ring) > 0

    capture.forget_recent_audio()

    assert len(capture._ring) == 0
    assert capture.recent_audio(1.0).size == 0


# -- fail-closed at the capture boundary ------------------------------------


def test_start_refuses_when_verification_is_required_but_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", object())
    capture = _capture()
    with pytest.raises(ProviderUnavailable, match="no verifier is attached"):
        capture.start()
    assert capture._stream is None, "a refused gate must not leave a device open"


def test_nobody_enrolled_opens_the_device_only_for_enrolling(monkeypatch):
    """**2026-08-31 改了这一条，而且它不是放宽。**

    此前「一个人都没注册」也抛，于是形成一个死锁：声纹门不许开麦，而控制台的注册要从
    采集缓冲取音频 —— 第一次注册只能用命令行脚本。使用者的要求是「希望在控制台能进行
    全部的设置，包括第一次录制声纹」。

    现在走 ``enroll_only``：设备开着、缓冲照常填，但 ``wake_held`` 恒真，``_authorise``
    永远不会被调到。真正不许绕过的断言是「唤醒不经校验不许通过」—— 下面那两行钉的就是它。
    """
    opened = {}

    class Stream:
        def __init__(self, **kwargs):
            opened.update(kwargs)

        def start(self):
            opened["started"] = True

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    capture = _capture(verifier=StubVerifier(speakers=()))

    capture.start()

    assert capture.enroll_only is True
    assert capture.wake_held is True, "注册模式下唤醒判定必须一直被按住"
    # 一整块正常电平的音频进来，也绝不许产生唤醒。
    woke = []
    capture.on_wake = lambda *a: woke.append(a)
    capture.keyword_provider.hits = ["你好问问"]
    capture._callback(_block(160), 160, None, None)
    assert woke == [], "enroll_only 下不许有任何唤醒被接受"
    assert capture.kws_hits == 0, "连 KWS 都不该被喂到"


def test_enrolling_from_the_console_arms_the_wake_without_a_restart():
    """**「注册完就会响应唤醒词」必须是真的。**

    ``enroll_only`` 只在 ``start()`` 里判一次，而控制台注册发生在麦克风已经跑起来之后。
    2026-09-01 实机：使用者在页面上注册成功，页面写着注册完就会响应唤醒词，可
    ``wake_held`` 仍然恒真 —— 喊什么都没反应，必须重启。页面上那句话就是这个方法。
    """
    verifier = StubVerifier(speakers=())
    capture = _capture(verifier=verifier, require_verification=True)
    capture.enroll_only = True

    # 注册前：解不开，因为档案表还是空的。
    assert capture.arm_after_enrollment() is False
    assert capture.wake_held is True

    verifier.speakers = ["du"]  # 注册成功（控制台走的是同一个 verifier 实例）

    assert capture.arm_after_enrollment() is True
    assert capture.enroll_only is False
    assert capture.wake_held is False, "注册成功之后唤醒判定必须自己解开，不该要求重启"
    # 幂等：已经解开了就不再报「刚解开」。
    assert capture.arm_after_enrollment() is False


def test_arming_never_runs_the_other_way():
    """**只从「按住」走向「正常」。** 关掉唤醒的判定权仍然只属于 `_check_gate_preconditions`
    —— 一个能把 `enroll_only` 打开的公开方法等于给「让唤醒失效」开了一个远程开关。"""
    capture = _capture(verifier=StubVerifier(speakers=()), require_verification=True)

    assert capture.enroll_only is False
    assert capture.arm_after_enrollment() is False
    assert capture.enroll_only is False, "这个方法不许把注册模式打开"


def test_start_still_refuses_when_the_model_is_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "sounddevice", object())
    verifier = SpeakerVerificationProvider(
        model_path=tmp_path / "absent.onnx", store_path=tmp_path / "store.json"
    )
    capture = _capture(verifier=verifier)
    with pytest.raises(ProviderUnavailable, match="unusable"):
        capture.start()


def test_a_verifier_fault_is_a_rejection_not_a_pass():
    kws = StubKws(["你好问问"])
    woken: list = []
    rejected: list = []
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: woken.append(a),
        blocksize=160,
        verifier=StubVerifier(raises=True),
        on_reject=lambda *a: rejected.append(a),
    )
    capture._callback(_block(160), 160, None, None)
    assert woken == []
    assert rejected and "verifier error" in rejected[0][1]


def test_a_low_score_is_rejected_silently():
    kws = StubKws(["你好问问"])
    woken: list = []
    rejected: list = []
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: woken.append(a),
        blocksize=160,
        verifier=StubVerifier(accepted=False, score=0.21),
        on_reject=lambda *a: rejected.append(a),
    )
    capture._callback(_block(160), 160, None, None)
    assert woken == []
    assert rejected == [("你好问问", "below threshold", pytest.approx(0.21))]


def test_gate_active_is_false_for_the_escape_hatch():
    assert _capture(verifier=StubVerifier()).gate_active is True
    assert _capture(verifier=StubVerifier(), require_verification=False).gate_active is False
    assert _capture(require_verification=False).gate_active is False


# -- rejection stays silent at the plugin boundary --------------------------


def test_wake_rejected_changes_no_state_and_emits_no_reply():
    plugin = VoicePlugin()
    plugin.start()
    before = plugin.machine.state
    event = plugin.wake_rejected("你好问问", "below threshold 0.5", 0.31)
    assert plugin.machine.state is before, "a rejection must not move the state machine"
    assert event["type"] == "wake.rejected"
    assert event["payload"] == {
        "keyword": "你好问问",
        "reason": "below threshold 0.5",
        "score": 0.31,
    }
    assert [e["type"] for e in plugin.events if e["type"].startswith("state.")] == [
        "state.changed"
    ], "only the start event should have touched state"
    assert plugin.status()["rejections"] == 1


def test_diagnose_warns_loudly_when_the_gate_is_off():
    plugin = VoicePlugin(audio_capture=_capture(require_verification=False))
    speaker = plugin.diagnose()["speaker"]
    assert speaker["require_verification"] is False
    assert speaker["gate_active"] is False
    assert any("anyone can wake" in w for w in speaker["warnings"])


def test_diagnose_reports_counts_and_never_a_vector():
    verifier = StubVerifier()
    plugin = VoicePlugin(audio_capture=_capture(verifier=verifier))
    plugin.wake_rejected("你好问问", "below threshold", 0.2)
    speaker = plugin.diagnose()["speaker"]
    assert speaker["enrolled_count"] == 1
    assert speaker["enrolled"] == ["owner"]
    assert speaker["rejections"] == 1
    assert speaker["last_rejection"]["score"] == 0.2
    assert "embeddings" not in speaker and "vectors" not in speaker
    assert "raw" not in repr(speaker)


# -- enrollment data stays out of version control --------------------------


def test_enrollment_directory_is_gitignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip().rstrip("/") == "enrollment" for line in ignore), (
        "enrollment/ holds biometric data and must never be committed"
    )


def test_shipped_config_defaults_to_the_secure_setting():
    config = load_speaker_config(ROOT / "config" / "speaker.toml")
    assert config["require_verification"] is True
    assert config["threshold"] > 0


def test_a_missing_config_still_defaults_to_requiring_verification(tmp_path):
    config = load_speaker_config(tmp_path / "absent.toml")
    assert config["require_verification"] is True
