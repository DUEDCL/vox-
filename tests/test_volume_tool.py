"""`system.volume` —— 「声音大一点」。

朗读期间最自然的一句话，而那正是使用者手不在键盘上的时刻。这里的每一条钉的都是「一句话
被理解成正确的那个动作」：50 是一半不是最大、`"false"` 是假不是真、报的是重读之后的值。

Evidence level: AUTO（假 backend，不碰 COM）。真机那一次在提交信息里（REAL-WIN）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import open_tools
from core.tools.contract import ToolRequest
from core.tools.volume import SystemVolumeTool, _fraction, _truthy


@dataclass
class FakeEndpoint:
    name: str = "扬声器 (Realtek(R) Audio)"
    level: float = 0.5
    muted: bool = False
    default: bool = True


class FakeMixer:
    """一个记得自己被怎么调的假混音器。``granular`` 模拟只支持有级音量的驱动。"""

    def __init__(self, *, level: float = 0.5, muted: bool = False, granular: bool = False,
                 boom: bool = False) -> None:
        self.state = FakeEndpoint(level=level, muted=muted)
        self.granular = granular
        self.boom = boom
        self.calls: list[tuple[str, object]] = []

    def output_level(self) -> FakeEndpoint:
        if self.boom:
            raise RuntimeError("Activate 回 E_NOINTERFACE")
        self.calls.append(("read", None))
        return replace(self.state)

    def set_output_level(self, value: float) -> FakeEndpoint:
        if self.boom:
            raise RuntimeError("Activate 回 E_NOINTERFACE")
        self.calls.append(("set", value))
        # 有级驱动：只落在 0 / 0.5 / 1。真机上见过只有三挡的设备。
        self.state.level = round(value * 2) / 2 if self.granular else value
        if value > 0:
            self.state.muted = False
        return replace(self.state)

    def set_output_muted(self, muted: bool) -> FakeEndpoint:
        self.calls.append(("mute", muted))
        self.state.muted = bool(muted)
        return replace(self.state)


def _run(mixer, **arguments):
    tool = SystemVolumeTool({}, backend=mixer)
    return tool.run(ToolRequest(tool="system.volume", arguments=arguments, origin="agent"))


def test_no_arguments_reads_rather_than_changes():
    """「现在多大声」和「大一点」是两句话。空参数不许动音量 —— 一个会顺手改设置的查询
    是最让人不敢用的那种工具。"""
    mixer = FakeMixer(level=0.42)

    result = _run(mixer)

    assert result.ok
    assert "42%" in result.output
    assert [name for name, _ in mixer.calls] == ["read"]


@pytest.mark.parametrize("given, want", [(0.5, 0.5), (50, 0.5), (1, 1.0), (100, 1.0), (0, 0.0)])
def test_a_level_is_read_as_a_fraction_or_a_percentage(given, want):
    """模型两种写法都会写。**把 50 当成 5000% 再钳到 1.0 会让「调到一半」变成「调到最大」**
    —— 一个当场吓人的结果。判据是绝对值大于 1：0.5 只可能是一半，50 只可能是百分之五十。"""
    mixer = FakeMixer()

    _run(mixer, level=given)

    assert ("set", want) in mixer.calls


@pytest.mark.parametrize("delta, want", [(0.1, 0.6), (-0.1, 0.4), (-1, 0.0), (0.9, 1.0)])
def test_a_delta_moves_from_wherever_it_is_now(delta, want):
    """「大一点」没有数字，所以它必须先读再设 —— 而结果要钳在 0–1，不是外推。"""
    mixer = FakeMixer(level=0.5)

    _run(mixer, delta=delta)

    assert ("set", pytest.approx(want)) in mixer.calls


def test_an_explicit_level_wins_over_a_delta():
    """两个都给时取明确那个：一个数字比一个相对量更接近使用者的意思。"""
    mixer = FakeMixer(level=0.5)

    _run(mixer, level=0.2, delta=0.5)

    assert ("set", 0.2) in mixer.calls


def test_the_answer_reports_what_was_read_back_not_what_was_asked_for():
    """有些驱动只支持有级的音量。把请求值原样念回去等于报一个没发生的事 ——
    而这个工具的回答会被念出来。"""
    mixer = FakeMixer(level=0.0, granular=True)

    result = _run(mixer, level=0.4)

    assert "50%" in result.output, "落到了 0.5 那一挡，不是请求的 40%"
    assert result.audit["level"] == 50


def test_muting_is_separate_from_setting_a_level():
    """一个静音的设备把音量调到 100 仍然不出声，而使用者说「静音」时不希望他原来的音量
    被忘掉。所以 mute 走自己那条路，一个 level 都不写。"""
    mixer = FakeMixer(level=0.7)

    result = _run(mixer, mute=True)

    assert mixer.state.muted and mixer.state.level == 0.7
    assert [name for name, _ in mixer.calls] == ["mute"]
    assert "静音" in result.output


@pytest.mark.parametrize("given, want", [("false", False), ("no", False), ("0", False),
                                         ("否", False), ("true", True), ("是", True),
                                         (True, True), (False, False)])
def test_a_boolean_written_as_a_string_is_read_correctly(given, want):
    """**`bool("false")` 是 `True`。** 模型写 JSON 时偶尔把布尔写成字符串，而那会让
    「取消静音」变成「静音」—— 一个当场听得出来但完全说不通的结果。同一个坑在
    `shell.run` 的 `confirmed` 上抓过一次（`"no"` 是个真值字符串）。"""
    assert _truthy(given) is want


def test_a_level_that_is_not_a_number_is_ignored_rather_than_guessed():
    """认不出来就当没给 —— 「设成『大声』」应当退化成一次读，不是一次乱调。"""
    assert _fraction("大声") is None
    assert _fraction(None) is None
    assert _fraction(True) is None, "布尔不是音量：{'level': true} 不该变成 100%"


def test_a_com_failure_becomes_a_readable_refusal_not_a_crash():
    result = _run(FakeMixer(boom=True), delta=0.1)

    assert not result.ok
    assert "RuntimeError" in (result.error or "")
    assert result.audit["decision"] == "failed"


def test_the_device_name_goes_to_the_audit_not_to_the_answer():
    """使用者问的是「多大声」，不是「哪只喇叭」。"""
    result = _run(FakeMixer())

    assert "Realtek" not in result.output
    assert "Realtek" in str(result.audit["device"])


@pytest.mark.skipif(sys.platform != "win32", reason="音量只在 Windows 上有实现")
def test_the_tool_is_registered_out_of_the_box_on_windows():
    assert "system.volume" in open_tools({}, mcp=False).tools


@pytest.mark.skipif(sys.platform != "win32", reason="音量只在 Windows 上有实现")
def test_the_config_can_switch_it_off():
    """关掉就该退回「这个工具不存在」，而不是留一个每次都被门拒掉的名字。"""
    assert "system.volume" not in open_tools({"system": {"enabled": False}}, mcp=False).tools
