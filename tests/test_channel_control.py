"""页面上那个「打开通道 / 关闭通道」—— 它住在启动脚本里，因为只有那一层手上有 stack。

为什么单独一个文件：``scripts/run_console.py`` 此前**一行测试都没有**（没有任何测试
import 过 scripts/），而 2026-09-04 往它里面加了一个能被网页调用的入口。一个能从网页
起线程的函数不该是全仓库唯一没被断言过的那个。

这里断言的两条不变式：

1. **重复点「打开」不留下第二条长轮询线程。** 两条线程会各自收到同一批消息，于是每条
   微信消息被回答两次 —— 而那是使用者会先发现、我们最后才发现的一种缺陷。
2. **``--no-weixin`` 赢。** 它是命令行上点名的「这一轮不要通道」，一个网页不该翻它。

不打网络、不起真线程：``open_channel`` 被替换掉。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def run_console():
    """按路径加载启动脚本。``scripts/`` 不是包，所以只能这样。"""
    spec = importlib.util.spec_from_file_location(
        "vox_run_console_under_test", ROOT / "scripts" / "run_console.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    """一条通道 runner 的替身。记下它被停过没有。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _api():
    return SimpleNamespace(channel_runner=None)


def test_starting_the_channel_hands_the_runner_to_the_console(run_console, monkeypatch):
    made: list[FakeRunner] = []

    def fake_open(runtime, stack):
        del runtime, stack
        runner = FakeRunner(f"r{len(made)}")
        made.append(runner)
        return runner, object(), ""

    monkeypatch.setattr(run_console, "open_channel", fake_open)
    api = _api()
    control = run_console.make_channel_control(api, runtime=None, stack=None)

    result = control("start")

    assert result["running"] is True
    assert api.channel_runner is made[0], "控制台那一栏读的就是这个引用"


def test_starting_twice_does_not_leave_two_pollers(run_console, monkeypatch):
    """两条长轮询线程会各自收到同一批消息 —— 每条微信消息被回答两次。"""
    made: list[FakeRunner] = []

    def fake_open(runtime, stack):
        del runtime, stack
        runner = FakeRunner(f"r{len(made)}")
        made.append(runner)
        return runner, object(), ""

    monkeypatch.setattr(run_console, "open_channel", fake_open)
    api = _api()
    control = run_console.make_channel_control(api, runtime=None, stack=None)

    control("start")
    control("start")

    assert len(made) == 2
    assert made[0].stopped is True, "旧的那条必须先停掉"
    assert api.channel_runner is made[1]


def test_stopping_clears_the_reference_the_console_reads(run_console, monkeypatch):
    """停了之后 ``channel_runner`` 必须变成 None。

    留着一个已经停掉的 runner 会让页面继续显示「在跑」，而 ``weixin_send`` 会把一条消息
    交给一条不再轮询的通道 —— 那条消息去哪了没有任何读数。
    """
    runner = FakeRunner("only")
    monkeypatch.setattr(run_console, "open_channel", lambda *a: (runner, object(), ""))
    api = _api()
    control = run_console.make_channel_control(api, runtime=None, stack=None)
    control("start")

    result = control("stop")

    assert result["running"] is False
    assert runner.stopped is True
    assert api.channel_runner is None


def test_no_weixin_on_the_command_line_wins(run_console, monkeypatch):
    """命令行上点名的「这一轮不要通道」，一个网页不该翻它。"""

    def boom(*args, **kwargs):
        raise AssertionError("--no-weixin 之后不许建通道")

    monkeypatch.setattr(run_console, "open_channel", boom)
    api = _api()
    control = run_console.make_channel_control(api, runtime=None, stack=None, disabled=True)

    result = control("start")

    assert result["running"] is False
    assert "--no-weixin" in result["reason"]
    assert api.channel_runner is None


def test_an_unknown_action_is_refused(run_console):
    control = run_console.make_channel_control(_api(), runtime=None, stack=None)

    with pytest.raises(ValueError, match="未知的动作"):
        control("restart")


def test_a_channel_that_will_not_open_reports_the_reason(run_console, monkeypatch):
    """``open_channel`` 的说明就是原因 —— 页面据它显示「没起来，因为……」。"""
    monkeypatch.setattr(
        run_console, "open_channel", lambda *a: (None, None, "weixin: 配了但用不了 —— 还没绑定")
    )
    control = run_console.make_channel_control(_api(), runtime=None, stack=None)

    result = control("start")

    assert result["running"] is False
    assert "还没绑定" in result["reason"]
