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


# ------------------------------------ 「全范围可配」这条要求本身要能被断言（2026-09-03）


def test_every_config_key_is_either_editable_or_has_a_stated_reason():
    """使用者点名的硬要求：**没有任何一项配置只能靠改文件才能改**。

    「还差哪些」靠记忆回答一定会漏，所以它是一条命令
    （`scripts/audit_config_surface.py`）加这一条断言。三类：可改、刻意不可改（带理由，
    安全边界与凭据变量名在这一类）、以及**没有理由的缺口** —— 最后这类必须是空的。

    加一个新配置键而忘了它的归属，这一条就会红。那正是它存在的意义：一项配置「只能靠改
    文件」在使用者的路径里等于不存在，而这件事不该靠谁记得。
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        import audit_config_surface as audit
    finally:
        sys.path.remove(str(root / "scripts"))

    from core.console.routes import EDITABLE

    unexplained: list[str] = []
    for file, keys in {
        "voice.toml": audit.voice_keys(),
        "tools.toml": audit.tools_keys(),
    }.items():
        editable = set(EDITABLE.get(file, ()))
        for key in keys:
            if key in editable or key in audit.WONT or key in audit.KNOWN_GAPS:
                continue
            unexplained.append(f"{file}:{key}")

    assert unexplained == [], (
        "这些键既不在控制台白名单里，也没有「为什么不放开」的理由 —— "
        "要么加进 EDITABLE，要么在 audit_config_surface.WONT 里写清理由：" + ", ".join(unexplained)
    )


def test_every_editable_key_is_actually_visible_on_the_page():
    """**「可改」的另一半是「看得见」。**

    控制台只渲染 `/api/config` 返回的键，而那个端点读的是**文件**（`editable_keys`）——
    所以一个只存在于代码默认值里的键在页面上根本不出现，而不出现等于改不了。
    2026-09-03 就有两个这样的键（`web.open_enabled` / `web.open_search_url`：在白名单里、
    出厂 `config/tools.toml` 里没写）。修法是把那一行写进出厂文件，文件本身也是文档。
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        import audit_config_surface as audit
    finally:
        sys.path.remove(str(root / "scripts"))

    invisible = audit.missing_from_shipped()

    assert invisible == [], (
        "这些键在 EDITABLE 里但出厂配置文件没写它们，所以页面上看不见 —— "
        "把那一行写进文件：" + ", ".join(invisible)
    )


def test_the_security_boundaries_are_still_not_editable():
    """反向的护栏。「全范围可配」**不覆盖安全边界**：让一个网页改 `shell.allow` 或
    `fs.roots` 等于让它决定这台机器上能跑什么、能读什么。这一条钉住那条界线。"""
    from core.console.routes import EDITABLE

    for key in (
        "shell.enabled",
        "shell.allow",
        "fs.roots",
        "fs.denied_names",
        "fs.denied_dirs",
        "apps.entries",
        "web.enabled",
    ):
        assert key not in EDITABLE["tools.toml"], key
    # 凭据「读哪个变量」同理 —— 值走 /api/secret 的白名单，变量名留在文件里。
    assert "tts.key_env" not in EDITABLE["voice.toml"]
