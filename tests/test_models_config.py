"""``config/models.toml``: the loader's refusals, and edits that keep the comments.

Three properties are worth a test each, because each one is a promise the file's
own header makes:

- a value shaped like a credential is **refused whole**, not redacted
- plain HTTP endpoints are loopback-only, and a URL may not carry credentials
- an edit preserves every comment, and a rejected edit leaves the file untouched

Evidence level: AUTO. Nothing here reaches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_edit import ConfigEditError, set_section
from core.models_config import (
    FIELDS,
    KINDS,
    ModelsConfigError,
    check_field,
    load_models_config,
    looks_like_secret,
    models_config_path,
    url_problem,
    write_profile_kind,
)

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "models.toml"

#: 写入与拒绝那些测试用的**自带**样本，不是仓库里那份出厂文件。
#:
#: **这个常量是 2026-09-01 一次全绿变 11 红的正解。** 在它之前 ``models`` fixture 复制的是
#: `config/models.toml`，而那个文件是**给人改的** —— 控制台上「模型配置」那一栏写的就是它。
#: 使用者把 `[profiles.local.llm]` 从 `provider = "ollama"` 改成了 `custom`，于是：
#: 断言 `provider == "ollama"` 的那条直接红，而另外 10 条靠
#: ``.replace('provider = "ollama"', ...)`` 注入坏值的测试变成了**空替换** ——
#: 文件没被改坏，`pytest.raises` 于是 DID NOT RAISE。
#:
#: 也就是说：一个人正常使用这个产品，就会让 11 个测试变红，而红的原因和被测代码无关。
#: 判据和 `.claude/CLAUDE.md` 里那条「基线必须在干净 shell 里记」是同一条 —— 一个结果
#: 取决于可变输入的测试等于没有测试。出厂文件仍然被测，但只测**不变式**（见下面那两条）。
FIXTURE = """\
# 出厂的两套方案。这个文件的注释是内容的一部分：写入必须逐条保住它们。
active = "local"

# ---------------------------------------------------------------- 本机
[profiles.local]
label = "全本机（离线）"

[profiles.local.asr]
provider = "sherpa-local"
model = "zipformer-zh-14M"

[profiles.local.tts]
provider = "sherpa-local"
model = "vits-zh-single"

# 本机 LLM 走 Ollama：base/proto 由预设给，所以这里只写 provider 与 model。
[profiles.local.llm]
provider = "ollama"
model = "qwen2.5:7b"

# ---------------------------------------------------------------- 云端 LLM
[profiles.cloud-llm]
label = "本机语音 + 云端 LLM"

[profiles.cloud-llm.asr]
provider = "sherpa-local"
model = "zipformer-zh-14M"

[profiles.cloud-llm.tts]
provider = "sherpa-local"
model = "vits-zh-single"

[profiles.cloud-llm.llm]
provider = "deepseek"
model = "deepseek-chat"
key_env = "VOX_LLM_KEY"
"""


@pytest.fixture
def models(tmp_path):
    """A writable sample file, comments and all. See ``FIXTURE``."""
    target = tmp_path / "models.toml"
    target.write_text(FIXTURE, encoding="utf-8")
    return target


# ----------------------------------------------------------------------- loader


def test_the_shipped_file_holds_up_as_a_config():
    """出厂文件加载得动，``active`` 指向一个真的存在的 profile。

    **只断言不变式，一个具体值都不钉。** 这个文件是给人改的：控制台上「新建方案」、
    「换模型」、「换端点」都是正常动作，而 provider/model/base 改成什么是使用者的事。
    钉住其中任何一个值，等于让一个正常使用产品的人看到红色的测试 —— 那不是回归的信号。
    真正的回归是「文件被改成了加载不动的样子」，那就是这一条在测的。
    """
    config = load_models_config(SHIPPED)
    assert config["active"], "active 是空的：没有任何一套方案生效"
    assert config["active"] in config["profiles"]
    for name, profile in config["profiles"].items():
        assert isinstance(profile.get("label", ""), str), name
        for kind in KINDS:
            assert set(profile.get(kind, {})) <= set(FIELDS), f"{name}.{kind}"


def test_a_missing_file_is_an_empty_registry_not_an_error(tmp_path):
    config = load_models_config(tmp_path / "nope.toml")
    assert config == {"active": "", "profiles": {}}


def test_the_shipped_file_carries_no_key_only_key_env():
    """The one property that must hold for every profile in the repository."""
    for profile in load_models_config(SHIPPED)["profiles"].values():
        for kind in KINDS:
            section = profile.get(kind, {})
            assert "key" not in section and "token" not in section
            assert not looks_like_secret(section.get("key_env", ""))


def test_an_active_that_names_no_profile_is_refused(models):
    models.write_text(
        models.read_text(encoding="utf-8").replace('active = "local"', 'active = "ghost"'),
        encoding="utf-8",
    )
    with pytest.raises(ModelsConfigError, match="names no profile"):
        load_models_config(models)


@pytest.mark.parametrize(
    "line, message",
    [
        ("provider2 = 'x'", "unknown model key"),
        ("proto = 'grpc'", "must be one of"),
        ("key_env = 'sk-live-0123456789abcdef'", "密钥"),
        ("base = 'http://evil.example.com/v1'", "回环"),
        ("base = 'https://user:pass@example.com/v1'", "凭据"),
    ],
)
def test_a_bad_field_is_refused_by_name(models, line, message):
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            'provider = "ollama"', f'provider = "ollama"\n{line}'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelsConfigError, match=message):
        load_models_config(models)


def test_an_unknown_top_level_key_is_refused(models):
    # Above the first table header, or TOML would read it as part of that table.
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            'active = "local"', 'active = "local"\nprovider = "oops"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelsConfigError, match="unknown top-level key"):
        load_models_config(models)


def test_an_unknown_key_inside_a_profile_is_refused(models):
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            '[profiles.local]\nlabel', '[profiles.local]\nvision = "yes"\nlabel'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelsConfigError, match="unknown key in"):
        load_models_config(models)


def test_the_env_override_is_honoured(models, monkeypatch):
    monkeypatch.setenv("VOX_MODELS_CONFIG", str(models))
    assert models_config_path() == models


# ------------------------------------------------------------- credential shapes


@pytest.mark.parametrize(
    "value",
    [
        "sk-abc123def456ghi789",
        "sk_live_0123456789",
        "ghp_0123456789abcdefghij",
        "xoxb-123-456-abcdef",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA0123456789abcdefg",
        "Bearer abcdef123456",
        "AbC123dEf456GhI789jKl012",  # a mixed-case blob with no separators
    ],
)
def test_credential_shapes_are_caught(value):
    assert looks_like_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "deepseek",
        "deepseek-chat",
        "qwen2.5:7b",
        "claude-opus-4-20250514",
        "Qwen2.5-72B-Instruct",
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "VOX_LLM_KEY",
        "openai",
    ],
)
def test_real_settings_are_not_mistaken_for_credentials(value):
    """A false positive here refuses a legitimate edit, so the rule stays narrow."""
    assert not looks_like_secret(value)


def test_a_secret_is_refused_whole_rather_than_redacted():
    """Redacting would leave the value in the file's history; the point is to
    refuse the write so the operator knows to use an environment variable."""
    with pytest.raises(ModelsConfigError, match="环境变量名"):
        check_field("profiles.x.llm", "key_env", "sk-live-abcdefghijklmnop")


# -------------------------------------------------------------------- endpoints


@pytest.mark.parametrize(
    "url",
    ["https://api.openai.com/v1", "http://127.0.0.1:11434/v1", "http://localhost:8080/v1",
     "http://[::1]:11434/v1"],
)
def test_acceptable_endpoints(url):
    assert url_problem(url) is None


@pytest.mark.parametrize(
    "url, message",
    [
        ("http://api.example.com/v1", "回环"),
        ("https://user:pass@api.example.com/v1", "凭据"),
        ("ftp://api.example.com/v1", "http(s)"),
        ("api.example.com/v1", "http(s)"),
        ("", "http(s)"),
    ],
)
def test_refused_endpoints(url, message):
    assert message in (url_problem(url) or "")


# ----------------------------------------------------------------------- writing


def test_an_existing_key_is_changed_and_every_comment_survives(models):
    before = models.read_text(encoding="utf-8")
    comments = [line for line in before.splitlines() if line.startswith("#")]
    changed = write_profile_kind("local", "llm", {"model": "qwen3:8b"}, path=models)
    after = models.read_text(encoding="utf-8")
    assert changed["profiles.local.llm.model"]["to"] == '"qwen3:8b"'
    assert [line for line in after.splitlines() if line.startswith("#")] == comments
    assert load_models_config(models)["profiles"]["local"]["llm"]["model"] == "qwen3:8b"


def test_a_key_the_file_does_not_have_yet_is_inserted_into_its_own_table(models):
    write_profile_kind(
        "local", "llm", {"base": "http://127.0.0.1:11434/v1", "proto": "ollama"}, path=models
    )
    llm = load_models_config(models)["profiles"]["local"]["llm"]
    assert llm["base"] == "http://127.0.0.1:11434/v1"
    assert llm["proto"] == "ollama"
    # Inserted inside [profiles.local.llm], not leaked into the next table.
    assert load_models_config(models)["profiles"]["cloud-llm"]["llm"].get("base", "") == ""


def test_a_new_profile_is_created_with_its_label(models):
    write_profile_kind(
        "cloudall",
        "llm",
        {"provider": "custom", "model": "x-1", "base": "https://api.example.com/v1",
         "proto": "openai", "key_env": "VOX_LLM_KEY"},
        path=models,
        label="全云端",
    )
    profiles = load_models_config(models)["profiles"]
    assert profiles["cloudall"]["label"] == "全云端"
    assert profiles["cloudall"]["llm"]["provider"] == "custom"
    # The two shipped profiles are untouched.
    assert profiles["local"]["llm"]["model"] == "qwen2.5:7b"


def test_a_preset_value_is_not_copied_into_the_file(models):
    """The endpoint lives in the provider table. A copy here would pin today's
    value where tomorrow's correction cannot reach it."""
    changed = write_profile_kind(
        "local", "llm",
        {"provider": "ollama", "model": "qwen2.5:7b", "base": "http://127.0.0.1:11434/v1",
         "proto": "ollama"},
        path=models,
        preset={"base": "http://127.0.0.1:11434/v1", "proto": "ollama", "key_env": ""},
    )
    assert "profiles.local.llm.base" not in changed
    assert "profiles.local.llm.proto" not in changed
    llm = load_models_config(models)["profiles"]["local"]["llm"]
    assert "base" not in llm and "proto" not in llm


def test_a_value_that_differs_from_the_preset_is_kept(models):
    """A deliberate override is not a redundant copy."""
    write_profile_kind(
        "local", "llm", {"base": "http://127.0.0.1:9999/v1"}, path=models,
        preset={"base": "http://127.0.0.1:11434/v1", "proto": "ollama", "key_env": ""},
    )
    assert load_models_config(models)["profiles"]["local"]["llm"]["base"] == "http://127.0.0.1:9999/v1"


def test_a_key_the_file_already_has_stays_in_sync_with_the_preset(models):
    """Switching back to a preset must update the line, not leave a stale override
    that the page no longer shows."""
    write_profile_kind("cloud-llm", "llm", {"key_env": "VOX_OTHER_KEY"}, path=models)
    changed = write_profile_kind(
        "cloud-llm", "llm", {"key_env": "VOX_LLM_KEY"}, path=models,
        preset={"base": "", "proto": "openai", "key_env": "VOX_LLM_KEY"},
    )
    assert changed["profiles.cloud-llm.llm.key_env"]["to"] == '"VOX_LLM_KEY"'
    assert load_models_config(models)["profiles"]["cloud-llm"]["llm"]["key_env"] == "VOX_LLM_KEY"


def test_a_write_where_everything_is_redundant_changes_nothing(models):
    before = models.read_text(encoding="utf-8")
    assert write_profile_kind(
        "local", "llm", {"proto": "ollama"}, path=models,
        preset={"base": "", "proto": "ollama", "key_env": ""},
    ) == {}
    assert models.read_text(encoding="utf-8") == before


def test_an_empty_value_is_dropped_rather_than_written(models):
    """``base = ""`` would turn "use the preset's endpoint" into "no endpoint"."""
    write_profile_kind("local", "llm", {"model": "qwen3:8b", "base": "  "}, path=models)
    assert "base" not in load_models_config(models)["profiles"]["local"]["llm"]


def test_a_write_with_nothing_left_to_write_is_refused(models):
    with pytest.raises(ModelsConfigError, match="没有要写的字段"):
        write_profile_kind("local", "llm", {"base": ""}, path=models)


@pytest.mark.parametrize("profile", ["../etc", "a.b", "has space", "", "x" * 64, "[oops]"])
def test_a_profile_name_that_could_escape_its_table_is_refused(models, profile):
    before = models.read_text(encoding="utf-8")
    with pytest.raises(ModelsConfigError):
        write_profile_kind(profile, "llm", {"model": "x"}, path=models)
    assert models.read_text(encoding="utf-8") == before


def test_an_unknown_kind_is_refused(models):
    with pytest.raises(ModelsConfigError, match="kind must be"):
        write_profile_kind("local", "vision", {"model": "x"}, path=models)


def test_a_rejected_write_leaves_the_file_byte_for_byte(models):
    before = models.read_text(encoding="utf-8")
    with pytest.raises(ModelsConfigError):
        write_profile_kind("local", "llm", {"base": "http://evil.example.com/v1"}, path=models)
    with pytest.raises(ModelsConfigError):
        write_profile_kind("local", "llm", {"key_env": "sk-live-abcdefghijklmnop"}, path=models)
    assert models.read_text(encoding="utf-8") == before
    assert not list(models.parent.glob("*.tmp"))


def test_a_write_that_would_break_the_file_is_caught_by_the_loader(models):
    """``set_section`` validates with the real loader, so a value that parses as
    TOML but not as a models config never lands."""
    with pytest.raises(ConfigEditError, match="rejected"):
        set_section(models, "profiles.local.llm", {"proto": "grpc"},
                    validate=lambda path: load_models_config(path))
    assert load_models_config(models)["profiles"]["local"]["llm"].get("proto", "") == ""


def test_set_section_refuses_an_array_of_tables(models):
    with pytest.raises(ConfigEditError, match="array of tables"):
        set_section(models, "agents[0]", {"enabled": True})


def test_every_writable_field_is_one_the_loader_accepts(models):
    """``FIELDS`` is the console's allow-list and the loader's schema at once --
    a field that could be written but not read would be a silent trap."""
    write_profile_kind(
        "probe",
        "asr",
        {name: ("openai" if name == "proto" else
                "https://api.example.com/v1" if name == "base" else
                "VOX_ASR_KEY" if name == "key_env" else "custom" if name == "provider" else "m-1")
         for name in FIELDS},
        path=models,
        label="每个字段",
    )
    section = load_models_config(models)["profiles"]["probe"]["asr"]
    assert set(section) == set(FIELDS)
