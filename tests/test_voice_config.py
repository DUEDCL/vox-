"""``config/voice.toml``: defaults, strictness, and path resolution.

The strictness tests carry most of the weight. This loader refuses unknown keys
while ``load_speaker_config`` ignores them, and that difference is a decision:
a misspelled ``keywords_threshold`` that silently keeps the default is a setting
that looks applied and is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.audio.config import (
    DEFAULT_KWS_DIR,
    VoiceConfigError,
    default_voice_config,
    load_voice_config,
    model_paths,
    repo_root,
    resolve_device,
    resolve_keywords_file,
)

SHIPPED = repo_root() / "config" / "voice.toml"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "voice.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_config_file_loads():
    """The file in the repository must satisfy the loader that reads it.

    Without this test the schema and the shipped file can drift apart, and the
    first person to notice would be a user whose platform refuses to start.
    """
    assert SHIPPED.is_file()
    config = load_voice_config(SHIPPED)
    assert config["wake.keywords_threshold"] == 0.25
    assert config["input.sample_rate"] == 16000
    assert config["orb.enabled"] is True


def test_a_missing_file_yields_the_shipped_defaults(tmp_path):
    config = load_voice_config(tmp_path / "nope.toml")
    assert config == default_voice_config()
    assert config["tts.enabled"] is True


def test_unknown_section_is_refused(tmp_path):
    path = write(tmp_path, "[wakeword]\nkeywords_threshold = 0.3\n")
    with pytest.raises(VoiceConfigError, match=r"unknown config section: \[wakeword\]"):
        load_voice_config(path)


def test_unknown_key_is_refused(tmp_path):
    path = write(tmp_path, "[wake]\nkeyword_threshold = 0.3\n")
    with pytest.raises(VoiceConfigError, match="unknown config key: wake.keyword_threshold"):
        load_voice_config(path)


def test_a_section_that_is_not_a_table_is_refused(tmp_path):
    path = write(tmp_path, "wake = 3\n")
    with pytest.raises(VoiceConfigError, match=r"\[wake\] must be a table"):
        load_voice_config(path)


def test_an_integer_is_not_accepted_where_a_boolean_belongs(tmp_path):
    """``enabled = 1`` must fail. In Python ``True`` is an ``int``, so a naive
    isinstance check would let this through and read as "on"."""
    path = write(tmp_path, "[tts]\nenabled = 1\n")
    with pytest.raises(VoiceConfigError, match="tts.enabled must be a boolean"):
        load_voice_config(path)


def test_a_boolean_is_not_accepted_where_an_integer_belongs(tmp_path):
    path = write(tmp_path, "[wake]\nnum_threads = true\n")
    with pytest.raises(VoiceConfigError, match="wake.num_threads must be an integer"):
        load_voice_config(path)


def test_a_string_is_not_accepted_where_a_number_belongs(tmp_path):
    path = write(tmp_path, '[wake]\nkeywords_threshold = "0.3"\n')
    with pytest.raises(VoiceConfigError, match="wake.keywords_threshold must be a number"):
        load_voice_config(path)


def test_an_integer_is_widened_to_a_float(tmp_path):
    """``speed = 1`` is a reasonable thing to write and means 1.0."""
    path = write(tmp_path, "[tts]\nspeed = 1\n")
    config = load_voice_config(path)
    assert config["tts.speed"] == 1.0
    assert isinstance(config["tts.speed"], float)


def test_a_number_is_not_accepted_where_a_string_belongs(tmp_path):
    path = write(tmp_path, "[input]\ndevice = 3\n")
    with pytest.raises(VoiceConfigError, match="input.device must be a string"):
        load_voice_config(path)


def test_broken_toml_names_the_file_rather_than_falling_back(tmp_path):
    path = write(tmp_path, "[wake\n")
    with pytest.raises(VoiceConfigError, match="voice config is unreadable"):
        load_voice_config(path)


def test_partial_config_keeps_defaults_for_everything_else(tmp_path):
    path = write(tmp_path, "[tts]\nenabled = false\n")
    config = load_voice_config(path)
    assert config["tts.enabled"] is False
    assert config["asr.enabled"] is True
    assert config["wake.keywords_threshold"] == 0.25


def test_model_paths_default_under_models(monkeypatch):
    for name in ("VOX_KWS_MODEL_DIR", "VOX_ASR_MODEL_DIR", "VOX_TTS_MODEL_DIR", "VOX_VAD_MODEL"):
        monkeypatch.delenv(name, raising=False)
    paths = model_paths()
    assert paths["kws_dir"] == repo_root() / "models" / DEFAULT_KWS_DIR
    assert paths["vad_model"].name == "silero_vad.onnx"


def test_model_paths_follow_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("VOX_KWS_MODEL_DIR", str(tmp_path / "kws"))
    monkeypatch.setenv("VOX_TTS_MODEL_DIR", str(tmp_path / "tts"))
    paths = model_paths()
    assert paths["kws_dir"] == tmp_path / "kws"
    assert paths["tts_dir"] == tmp_path / "tts"


def test_config_carries_the_resolved_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("VOX_ASR_MODEL_DIR", str(tmp_path / "asr"))
    config = load_voice_config(tmp_path / "absent.toml")
    assert config["asr_dir"] == str(tmp_path / "asr")


def test_env_var_selects_the_config_file(monkeypatch, tmp_path):
    path = write(tmp_path, "[orb]\nenabled = false\n")
    monkeypatch.setenv("VOX_VOICE_CONFIG", str(path))
    assert load_voice_config()["orb.enabled"] is False


def test_empty_keywords_file_falls_back_to_the_conventional_path(monkeypatch, tmp_path):
    """``wake.keywords_file`` 留空时先看约定路径，那个文件不在才回落到模型自带的。

    走约定而不是让控制台去写 ``wake.keywords_file``：「读哪个文件」和「阈值是多少」不是
    一类设置 —— 前者是个文件系统入口，放进可编辑白名单等于让一个网页决定进程去读哪个
    路径。代价是多一条规则，换来的是手改和界面改落在同一个文件上。
    """
    custom = tmp_path / "keywords.txt"
    monkeypatch.setenv("VOX_KEYWORDS_FILE", str(custom))

    # 约定路径上没有文件 -> None，也就是「用模型自带那份」
    assert resolve_keywords_file({"wake.keywords_file": "   "}) is None
    assert resolve_keywords_file({}) is None

    # 文件在了 -> 用它，不需要动配置
    custom.write_text("n ǐ h ǎo w èn w èn @你好问问\n", encoding="utf-8")
    assert resolve_keywords_file({}) == custom
    assert resolve_keywords_file({"wake.keywords_file": ""}) == custom


def test_an_explicit_keywords_file_still_beats_the_convention(monkeypatch, tmp_path):
    """点名了就用点名的那个：约定是为了少配一项，不是为了盖掉已经配好的。"""
    custom = tmp_path / "keywords.txt"
    custom.write_text("x @x\n", encoding="utf-8")
    monkeypatch.setenv("VOX_KEYWORDS_FILE", str(custom))

    explicit = tmp_path / "elsewhere.txt"
    assert resolve_keywords_file({"wake.keywords_file": str(explicit)}) == explicit


def test_relative_keywords_file_resolves_against_the_repository():
    """Not the process working directory: the same config has to work whether Vox
    was started from the repo, a shortcut, or a service manager."""
    resolved = resolve_keywords_file({"wake.keywords_file": "config/kw.txt"})
    assert resolved == repo_root() / "config" / "kw.txt"


def test_absolute_keywords_file_is_left_alone(tmp_path):
    absolute = tmp_path / "kw.txt"
    assert resolve_keywords_file({"wake.keywords_file": str(absolute)}) == absolute


def test_device_is_an_index_a_name_or_nothing():
    assert resolve_device({"input.device": ""}) is None
    assert resolve_device({"input.device": "3"}) == 3
    assert resolve_device({"input.device": "USB Microphone"}) == "USB Microphone"


# --------------------------------- 按名字选设备（2026-09-01 的索引漂移）


class _FakeSd:
    """两只输入设备，各自在 MME 与 WASAPI 下重复出现 —— Windows 的真实形状。"""

    def __init__(self) -> None:
        self._devices = [
            {"name": "麦克风阵列 (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 0},
            {"name": "耳机 (沉麟的耳机)", "max_input_channels": 1, "hostapi": 0},
            {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "hostapi": 0},
            {"name": "麦克风阵列 (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 1},
            {"name": "耳机 (沉麟的耳机)", "max_input_channels": 1, "hostapi": 1},
        ]
        self._apis = [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def query_devices(self, device=None, kind=None):
        if device is None:
            return list(self._devices)
        return self._devices[int(device)]

    def query_hostapis(self, index=None):
        if index is None:
            return list(self._apis)
        return self._apis[int(index)]


@pytest.fixture
def fake_sd(monkeypatch):
    import sys

    fake = _FakeSd()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake


def test_a_name_resolves_to_one_index_preferring_wasapi(fake_sd):
    """**索引会漂，名字不会。**

    2026-08-29 记下 `device = "2"` 时索引 2 是耳机；2026-09-01 同一个索引指向麦克风阵列，
    而耳机成了索引 1。移位之后不报错 —— 流照常打开、回调照常触发，只是唤醒率变差。
    这就是「配置与现实分岔」最安静的一种形式。

    WASAPI 优先是有理由的：这台机器上耳机只有 WASAPI 那一条报 16 kHz 原生采样率，
    其余三条都是 44.1 kHz，要多一层重采样。
    """
    from core.audio.config import resolve_device

    assert resolve_device({"input.device": "耳机"}) == 4
    assert resolve_device({"input.device": "麦克风阵列"}) == 3


def test_an_output_only_device_is_never_chosen(fake_sd):
    """扬声器也叫「Realtek」。按名字选设备时它必须被排除，否则会打开一个没有输入通道
    的设备 —— 而那的症状是「全零输入」，和一只被静音的麦克风一模一样。"""
    from core.audio.config import resolve_device

    assert resolve_device({"input.device": "扬声器"}) == "扬声器"


def test_an_unmatched_name_is_handed_back_untouched(fake_sd):
    """没匹配上就原样返回，让 sounddevice 报它自己的错（那条错里带候选清单）。

    `open_voice_stack` 会在 `start()` 之前把这件事变成一条警告 —— PortAudio 的原话
    读起来像「设备 -1 查询失败」，而真实情况是「你配的那只麦克风现在不在」。
    """
    from core.audio.config import resolve_device

    assert resolve_device({"input.device": "不存在的设备"}) == "不存在的设备"


def test_a_digit_is_still_an_index(fake_sd):
    """数字仍然原样当索引用：已经按索引配好的机器不该因为这次改动改变行为。"""
    from core.audio.config import resolve_device

    assert resolve_device({"input.device": "2"}) == 2
    assert resolve_device({"input.device": ""}) is None


def test_the_described_device_names_what_was_actually_opened(fake_sd):
    """就绪清单必须报**解析后的真实名字**。

    只报「2」的话，索引漂移这件事在任何一处读数里都是不可见的 —— 上一版配置注释写着
    「[2] 耳机」，而实际打开的是麦克风阵列，两者都「看起来正常」。
    """
    from core.audio.config import describe_device

    assert describe_device(4) == "4 = 耳机 (沉麟的耳机)（Windows WASAPI）"
    assert "系统默认" in describe_device(None)


def test_the_shipped_config_does_not_pin_an_index():
    """出厂配置不许再按索引写死设备。

    这一条是**防回归**：把它改回一个数字就等于把「插拔一个设备就换了麦克风」这个坑
    重新挖开，而那个坑不报错。
    """
    from core.audio.config import load_voice_config

    assert not str(load_voice_config()["input.device"]).strip().isdigit()
