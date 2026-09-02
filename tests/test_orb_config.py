"""唤醒球的外观要能在页面上改 —— 三项此前只能靠环境变量传。

使用者点名的那条硬要求：「web 界面应该可以对于 vox 进行全范围的配置修改并重启生效」。
在这之前控制台的「唤醒球」那一栏只会**生成一行 `VOX_ORB_SIZE=140 VOX_ORB_RENDERER=bot`
让人自己复制到启动环境里**，页面上自己写着「config/orb.toml 还没建」。一项配置只能靠
环境变量传，在他的使用路径里等于不存在。

所以这里钉三件事：
1. `[orb]` 的三项能写进 `config/voice.toml` 并读回来；
2. **越界与拼错在写的时候就被拒**，而不是存下去再在启动时悄悄变成别的值；
3. 翻译成环境变量时，**手动设过的仍然赢**（调参那条路不能被关掉）。

Evidence level: AUTO（临时文件里的配置，不起球、不打网络）。
"""

from __future__ import annotations

import pytest

from core.audio.config import (
    ORB_RENDERERS,
    ORB_SIZE_MAX,
    ORB_SIZE_MIN,
    VoiceConfigError,
    load_voice_config,
    orb_environment,
)

BASE = """
[orb]
enabled = true
visible = true
hide_after_s = 10.0
renderer = "seq"
size = 140
show_text = false
"""


def write(tmp_path, body: str):
    path = tmp_path / "voice.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_three_appearance_keys_round_trip(tmp_path):
    path = write(tmp_path, BASE.replace('"seq"', '"bot"').replace("140", "240").replace("show_text = false", "show_text = true"))

    config = load_voice_config(path)

    assert config["orb.renderer"] == "bot"
    assert config["orb.size"] == 240
    assert config["orb.show_text"] is True


def test_the_defaults_are_the_shipped_orb(tmp_path):
    """出厂行为一个字节都不变：AE 雪碧图、140px、不出文字。"""
    config = load_voice_config(tmp_path / "does-not-exist.toml")

    assert config["orb.renderer"] == "seq"
    assert config["orb.size"] == 140
    assert config["orb.show_text"] is False


@pytest.mark.parametrize("size", [10, 95, 421, 900])
def test_a_size_outside_the_box_range_is_refused_not_clamped(tmp_path, size):
    """**类型对而值离谱是另一种「看起来配了但其实没生效」。** `orb.size = 900` 是个合法
    整数，可球只能在 96–420 之间 —— 存下去再在启动时被钳成 420，等于页面上显示的数字
    和实际生效的数字不是一个。所以报错，而且报出范围。"""
    with pytest.raises(VoiceConfigError) as caught:
        load_voice_config(write(tmp_path, BASE.replace("size = 140", f"size = {size}")))

    assert "96" in str(caught.value) and "420" in str(caught.value)


def test_a_renderer_that_does_not_exist_is_refused(tmp_path):
    """拼错的渲染层名字必须报错。Rust 侧只认 `bot` 这一个值，所以一个拼错的名字在那边
    落到旧的那一层上 —— 使用者看到的是「我选了新的但它没变」。"""
    with pytest.raises(VoiceConfigError) as caught:
        load_voice_config(write(tmp_path, BASE.replace('"seq"', '"blob"')))

    for name in ORB_RENDERERS:
        assert name in str(caught.value)


def test_the_default_appearance_sets_no_environment_variables():
    """默认值**不写进 env**：这样一个手动设了 `VOX_ORB_SIZE=240` 的 shell 仍然赢。
    调参那条路（`VOX_ORB_SIZE=240 python scripts/run_console.py`）不能被配置文件关掉。"""
    env, warnings = orb_environment(
        {"orb.renderer": "seq", "orb.size": 140, "orb.show_text": False}
    )

    assert env == {}
    assert warnings == []


def test_a_non_default_appearance_becomes_three_variables():
    env, warnings = orb_environment({"orb.renderer": "bot", "orb.size": 240, "orb.show_text": True})

    assert env == {
        "VOX_ORB_RENDERER": "bot",
        "VOX_ORB_SIZE": "240",
        "VOX_SHOW_TEXT": "1",
    }
    assert warnings == []


def test_a_hand_built_config_is_clamped_and_reported_rather_than_crashing():
    """加载器让文件里不可能有坏值，但**程序里拼出来的字典**还是可能有（测试就是这么拼的）。
    那种情况下钳制 + 报警告 —— 一个越界的数字不该让球起不来。"""
    env, warnings = orb_environment({"orb.renderer": "nope", "orb.size": 9000})

    assert env["VOX_ORB_SIZE"] == str(ORB_SIZE_MAX)
    assert "VOX_ORB_RENDERER" not in env, "认不出的渲染层要落回 seq，而 seq 不写 env"
    assert len(warnings) == 2


def test_the_console_refuses_a_bad_value_before_writing(tmp_path):
    """页面上填 900 必须**当场**被拒，而不是写进文件再在下一次启动时变成 420。

    这一条走的是控制台真正用的那条路：`/api/config` 写之前用 voice.toml 自己的加载器
    试一遍（`routes.py::_validator`），所以校验只有一份。
    """
    from core.console import ConsoleApi

    write(tmp_path, BASE)
    api = ConsoleApi(runtime=None, stack=None, config_dir=tmp_path)

    ok = api.config_update("voice.toml", {"orb.renderer": "bot", "orb.size": 240})
    assert set(ok["changed"]) == {"orb.renderer", "orb.size"}
    assert ok["restart_required"] is True

    with pytest.raises(Exception) as caught:
        api.config_update("voice.toml", {"orb.size": 900})
    assert "96" in str(caught.value)
    # 被拒的那一次不许留下痕迹。
    assert load_voice_config(tmp_path / "voice.toml")["orb.size"] == 240


def test_the_size_floor_and_ceiling_match_the_rust_side():
    """这两个数在 `desktop/src-tauri/src/main.rs` 里也写着（`(96..=420)`）。
    两处必须一致 —— Python 放过一个值而 Rust 忽略它，症状是「存了但没变」。"""
    assert (ORB_SIZE_MIN, ORB_SIZE_MAX) == (96, 420)
