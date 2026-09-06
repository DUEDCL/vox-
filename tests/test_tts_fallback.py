"""云端合成失败之后还要能说话 —— `FallbackTts`。

这个组件是 2026-09-02 一次真机故障的产物：`VOX_TTS_KEY` 被另一份 key 覆盖，百炼回
HTTP 401，而这条路上的三层（`_open_tts` 只在 `load()` 失败时报警告、`complete_turn` 的
`except Exception: pass`、云端 provider 自己不重试）合起来的结果是**助手一句话都不出声，
而且哪里都不说为什么**。

所以这里钉的是两件事，缺一不可：**还能出声**（降级）和**说得出为什么**（留痕）。
只有第一件的话，使用者会以为云端配置生效了 —— 那正是旧立场「不降级」要防的东西。

Evidence level: AUTO（两个假 provider，不打网络、不出声）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.audio.tts_fallback import FallbackTts


@dataclass
class FakeStatus:
    available: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeTts:
    label: str
    available: bool = True
    loads: bool = True
    raises: bool = False
    reason: str = ""
    spoken: list[Any] = field(default_factory=list)
    stops: int = 0
    closes: int = 0

    def load(self) -> FakeStatus:
        return FakeStatus(self.loads, {"reason": self.reason} if not self.loads else {})

    def _maybe_raise(self) -> None:
        if self.raises:
            raise RuntimeError(f"{self.label} 挂了")

    def synthesize(self, text: str, **_kw: Any) -> str:
        self._maybe_raise()
        self.spoken.append(text)
        return f"{self.label}:{text}"

    def speak(self, text: str, **_kw: Any) -> str:
        return self.synthesize(text)

    def speak_segments(self, segments: Any, **_kw: Any) -> str:
        self._maybe_raise()
        self.spoken.extend(segments)
        return self.label

    def stop(self) -> None:
        self.stops += 1

    def is_stopped(self) -> bool:
        return False

    def close(self) -> None:
        self.closes += 1


def test_a_synthesis_failure_still_produces_speech():
    """**最要紧的一条。** 对语音助手来说「不出声」和「没听见」「崩了」「网断了」
    在使用者那一侧完全同形 —— 那是最难查的一种失败。"""
    cloud = FakeTts("cloud", raises=True)
    local = FakeTts("local")
    tts = FallbackTts(cloud, local)

    assert tts.speak_segments(["你好"]) == "local"
    assert local.spoken == ["你好"]


def test_the_switch_is_reported_not_silent():
    """只降级不留痕的话，使用者会以为云端配置生效了 —— 那正是旧立场要防的事，
    而它现在由「说出来」这一半解决，不由静音解决。"""
    said: list[str] = []
    tts = FallbackTts(FakeTts("cloud", raises=True), FakeTts("local"), on_problem=said.append)

    tts.speak("你好")

    assert len(said) == 1
    assert "云端合成" in said[0] and "本机合成" in said[0]
    assert tts.problems == said


def test_it_only_says_it_once():
    """每一轮都报一遍「换嗓子了」会把日志刷满，而这件事只发生一次。"""
    said: list[str] = []
    tts = FallbackTts(FakeTts("cloud", raises=True), FakeTts("local"), on_problem=said.append)

    for _ in range(4):
        tts.speak("你好")

    assert len(said) == 1
    assert tts.failures == 1, "只该失败一次：latch 之后主的那条路不再被碰"


def test_the_latch_stops_retrying_the_broken_path():
    """401 这类失败每轮重试一次，只是每轮多等一个往返。重启会重新试 ——
    所以修好 key 之后不需要额外操作，而这一次运行里它不再拖慢每一句回答。"""
    cloud = FakeTts("cloud", raises=True)
    tts = FallbackTts(cloud, FakeTts("local"))

    tts.speak("一")
    tts.speak("二")
    tts.speak("三")

    assert tts.latched is True
    assert cloud.spoken == [], "主的那条路 latch 之后一次都不该再被调用"


def test_a_load_failure_switches_before_the_first_word():
    """key 根本没配这类失败 `load()` 就能看出来 —— 那时候就该切，不用等第一次合成。"""
    tts = FallbackTts(FakeTts("cloud", loads=False, reason="$VOX_TTS_KEY 没设"), FakeTts("local"))

    status = tts.load()

    assert tts.latched is True
    assert status.available is True
    assert "$VOX_TTS_KEY" in tts.problems[0], "原因里必须带变量名，否则不可行动"


def test_loading_twice_does_not_re_probe_the_broken_path():
    """`load()` 会被调用两次（建栈时一次、启动脚本再确认一次）。"""
    cloud = FakeTts("cloud", loads=False, reason="x")
    tts = FallbackTts(cloud, FakeTts("local"))

    tts.load()
    tts.load()

    assert len(tts.problems) == 1


def test_both_engines_are_stopped_on_cancel():
    """切换可能发生在一次播放中间，所以停的必须是真正在响的那一个 —— 两个都停最简单。"""
    cloud, local = FakeTts("cloud"), FakeTts("local")
    tts = FallbackTts(cloud, local)

    tts.stop()
    tts.close()

    assert (cloud.stops, local.stops) == (1, 1)
    assert (cloud.closes, local.closes) == (1, 1)


def test_a_working_cloud_is_not_downgraded():
    """反向的护栏：云端好着的时候不许悄悄用本机嗓子。"""
    cloud, local = FakeTts("cloud"), FakeTts("local")
    tts = FallbackTts(cloud, local)

    tts.speak("你好")

    assert cloud.spoken == ["你好"]
    assert local.spoken == []
    assert tts.latched is False
    assert tts.describe()["degraded"] is False


def test_no_voice_at_all_says_so():
    """两边都不可用时话说清楚：这时候确实不出声，而使用者需要知道是这个原因。"""
    tts = FallbackTts(
        FakeTts("cloud", loads=False, reason="401"), FakeTts("local", available=False)
    )

    tts.load()

    assert "不出声" in tts.problems[0]


def test_a_broken_report_channel_never_changes_the_outcome():
    """报告通道是诊断，不是产品。它抛异常不能把这一句话变成不出声。"""

    def boom(_message: str) -> None:
        raise RuntimeError("logbook is gone")

    local = FakeTts("local")
    tts = FallbackTts(FakeTts("cloud", raises=True), local, on_problem=boom)

    assert tts.speak("你好") == "local:你好"
    assert local.spoken == ["你好"]
