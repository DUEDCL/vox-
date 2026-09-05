"""``open_voice_stack``: what degrades, what refuses, and what it reports.

The load-bearing test in this file is
``test_a_missing_voiceprint_model_does_not_turn_the_gate_off``. Everything else
here is about honest reporting; that one is about the difference between a
degraded mode and a different product.

Nothing loads a model: the providers are lazy, so pointing them at an empty
directory exercises the whole assembly at AUTO level with no ``models/`` present.
"""

from __future__ import annotations

import pytest

from core.audio.config import load_voice_config
from vox_plugin.voice_stack import VoiceStack, open_voice_stack


@pytest.fixture
def nowhere(monkeypatch, tmp_path):
    """Point all four model paths at directories that do not exist."""
    monkeypatch.setenv("VOX_KWS_MODEL_DIR", str(tmp_path / "kws"))
    monkeypatch.setenv("VOX_ASR_MODEL_DIR", str(tmp_path / "asr"))
    monkeypatch.setenv("VOX_TTS_MODEL_DIR", str(tmp_path / "tts"))
    monkeypatch.setenv("VOX_VAD_MODEL", str(tmp_path / "vad.onnx"))
    monkeypatch.setenv("VOX_SPEAKER_ENROLLMENT", str(tmp_path / "voiceprints.json"))
    return load_voice_config(tmp_path / "absent.toml")


def test_a_missing_asr_model_degrades_to_wake_only(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.asr is None
    assert stack.capture.asr_provider is None
    assert any("asr model not found" in w for w in stack.warnings)
    stack.close()


def test_a_missing_tts_model_degrades_to_silence(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.tts is None
    assert any("answers stay silent" in w for w in stack.warnings)
    stack.close()


def test_a_missing_voiceprint_model_does_not_turn_the_gate_off(nowhere):
    """A stack built with the gate on keeps it on even with nothing to verify against.

    ``capture.start()`` is what refuses, and that refusal is the product working.
    Silently flipping ``require_verification`` to False here would turn "nobody is
    enrolled" into "anyone may wake it" -- the exact substitution the fail-closed
    design exists to prevent.
    """
    stack = open_voice_stack(nowhere, require_verification=True)
    assert stack.capture.require_verification is True
    assert stack.gate_off is False
    stack.close()


def test_turning_the_gate_off_is_always_reported(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.gate_off is True
    assert stack.capture.verifier is None
    assert any("gate is OFF" in w for w in stack.warnings)
    stack.close()


def test_the_gate_defaults_to_the_speaker_config(nowhere):
    """With no explicit argument the shipped ``require_verification = true`` wins."""
    stack = open_voice_stack(nowhere)
    assert stack.capture.require_verification is True
    stack.close()


def test_disabling_asr_and_tts_by_config(nowhere):
    nowhere["asr.enabled"] = False
    nowhere["tts.enabled"] = False
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.asr is None and stack.tts is None
    # A disabled provider is not a missing one: no "not found" warning for it.
    assert not any("asr model not found" in w for w in stack.warnings)
    stack.close()


def test_capture_receives_the_configured_audio_parameters(nowhere):
    nowhere["input.sample_rate"] = 16000
    nowhere["input.blocksize"] = 800
    nowhere["input.device"] = "7"
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.capture.sample_rate == 16000
    assert stack.capture.blocksize == 800
    assert stack.capture.device == 7
    stack.close()


def test_an_explicit_device_beats_the_config(nowhere):
    nowhere["input.device"] = "7"
    stack = open_voice_stack(nowhere, require_verification=False, device="USB")
    assert stack.capture.device == "USB"
    stack.close()


def test_wake_threshold_and_threads_reach_the_provider(nowhere):
    nowhere["wake.keywords_threshold"] = 0.4
    nowhere["wake.num_threads"] = 3
    stack = open_voice_stack(nowhere, require_verification=False)
    assert stack.kws.keywords_threshold == 0.4
    assert stack.kws.num_threads == 3
    stack.close()


def test_readiness_reports_one_row_per_thing_that_can_be_missing(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    rows = stack.readiness()
    assert [row["item"] for row in rows] == ["wake", "asr", "tts", "speaker"]
    for row in rows:
        assert set(row) == {"item", "ready", "detail", "hint"}
    stack.close()


def test_readiness_hints_say_what_to_do_about_each_gap(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    hints = {row["item"]: row["hint"] for row in stack.readiness()}
    assert "VOX_KWS_MODEL_DIR" in hints["wake"]
    assert hints["speaker"]  # the escape hatch is a gap worth naming, not a pass
    stack.close()


def test_readiness_marks_a_disabled_provider_as_not_blocking(nowhere):
    nowhere["tts.enabled"] = False
    stack = open_voice_stack(nowhere, require_verification=False)
    tts_row = next(row for row in stack.readiness() if row["item"] == "tts")
    assert tts_row["ready"] is True
    assert tts_row["hint"] == ""
    stack.close()


def test_readiness_never_reports_a_voiceprint_vector(nowhere):
    """``describe()`` is the only sanctioned view of enrollment data."""
    stack = open_voice_stack(nowhere)
    rows = repr(stack.readiness())
    assert "embedding" not in rows and "vector" not in rows
    stack.close()


def test_close_is_idempotent(nowhere):
    stack = open_voice_stack(nowhere, require_verification=False)
    stack.close()
    stack.close()


def test_close_survives_a_provider_that_raises(nowhere):
    class Angry:
        def close(self) -> None:
            raise RuntimeError("native teardown failed")

    stack = VoiceStack(config=nowhere, tts=Angry(), kws=Angry())
    stack.close()  # best-effort: neither raise escapes


def test_a_stack_holds_no_speaker_identity(nowhere):
    """Identity lives on the plugin, set by the gate. A second holder would be a
    second source of truth for the one fact ``shell.run`` demands."""
    stack = open_voice_stack(nowhere, require_verification=False)
    assert not hasattr(stack, "speaker")
    assert not hasattr(stack, "verified_speaker")
    stack.close()


def test_an_unresolvable_config_name_falls_back_to_the_default_device(nowhere):
    """**名字解析不到时不要把名字原样交给 PortAudio。**

    交给它抛的是 `Multiple input devices found for '耳机'` 后面跟两条 WDM-KS 条目 ——
    而那两条恰恰是 `_match_device` 刚刚判断过「开不起来」才排除掉的。让一个已经做过判断的
    解析结果去触发一条列举它们的报错，是最容易把人带错方向的一种失败。

    退到系统默认之后麦克风可能是聋的，但那条路有专门的探测（开麦 4 秒后「全零输入」进
    运行日志）。「有一只可能听不见的麦克风」比「一只都没有」可诊断得多。
    """
    nowhere["input.device"] = "根本不存在的设备名"

    stack = open_voice_stack(nowhere, require_verification=False)

    assert stack.capture.device is None
    assert any("没匹配到任何可用的输入设备" in w for w in stack.warnings)
    assert any("系统默认" in w for w in stack.warnings)


# -- 就绪清单不许依赖 provider 的私有方法 ---------------------------------------
#
# 这一组的根因是一次真实故障，而且**同形的它出现过两次**：`readiness()` 里写着
# `self.asr._safe_endpoint()` / `engine._safe_endpoint()`，于是 2026-09-05 新增的流式
# provider（没有那个私有方法）让 `GET /api/state` 每次轮询都抛 AttributeError —— 页面上是
# 一句「连接失败 · failed: AttributeError · 还没成功读到过」，而语音其实一直在正常工作。
#
# 一个包装层或一条新 provider 让**别的**层报错，就是这种「看起来毫不相关」的故障。所以判据
# 不是「那个方法还在不在」，是**每一条 provider 都能过一遍就绪清单**。


def _stack_with(**providers):
    from vox_plugin.voice_stack import VoiceStack

    from core.audio.config import load_voice_config

    return VoiceStack(config=load_voice_config(), **providers)


def _asr_providers():
    from core.audio.asr import SherpaStreamingAsrProvider
    from core.audio.asr_cloud import DashScopeAsrProvider
    from core.audio.asr_ws import DashScopeWsAsrProvider

    return [
        SherpaStreamingAsrProvider("models/nonexistent"),
        DashScopeAsrProvider(model="fun-asr-flash-2026-06-15"),
        DashScopeWsAsrProvider(model="fun-asr-realtime"),
    ]


def _tts_providers():
    from core.audio.tts import SherpaTtsProvider
    from core.audio.tts_cloud import DashScopeTtsProvider
    from core.audio.tts_fallback import FallbackTts

    cloud = DashScopeTtsProvider(model="qwen-audio-3.0-tts-plus", voice="longanhuan_v3.6")
    return [
        SherpaTtsProvider("models/nonexistent"),
        cloud,
        FallbackTts(cloud, SherpaTtsProvider("models/nonexistent")),
    ]


@pytest.mark.parametrize("provider", _asr_providers(), ids=lambda p: type(p).__name__)
def test_every_asr_provider_survives_the_readiness_board(provider):
    """三条识别路都要能被印出来。**这条测试就是那个 bug 缺的东西。**"""
    rows = {row["item"]: row for row in _stack_with(asr=provider).readiness()}

    assert rows["asr"]["detail"], "「在哪」那一栏不许是空的 —— 它是「配置有没有生效」的答案"


@pytest.mark.parametrize("provider", _tts_providers(), ids=lambda p: type(p).__name__)
def test_every_tts_provider_survives_the_readiness_board(provider):
    """合成这一侧同理，而它多一层 `FallbackTts` 包装 —— 2026-09-03 那次就是没穿过它。"""
    rows = {row["item"]: row for row in _stack_with(tts=provider).readiness()}

    assert rows["tts"]["detail"]


def test_the_board_never_reaches_for_a_private_attribute():
    """一个只实现了**公开**契约的 provider 也要能过。

    直接钉住这条立场：跨模块调 `_foo()` 的代码在下一条 provider 上就会炸，而那时报错的
    地方（`/api/state`）和根因（新 provider）看起来毫不相关。
    """

    class BareBones:
        """公开契约齐全，一个下划线开头的东西都没有。"""

        model = "bare"
        voice = "plain"
        available = True

        def describe(self):
            return {"endpoint": "wss://example.test", "wire": "ws"}

    asr_rows = {row["item"]: row for row in _stack_with(asr=BareBones()).readiness()}
    tts_rows = {row["item"]: row for row in _stack_with(tts=BareBones()).readiness()}

    assert "bare" in asr_rows["asr"]["detail"]
    assert "example.test" in asr_rows["asr"]["detail"]
    assert "bare" in tts_rows["tts"]["detail"]
