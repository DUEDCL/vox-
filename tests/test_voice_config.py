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
