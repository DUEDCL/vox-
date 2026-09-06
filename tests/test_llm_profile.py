"""`config/models.toml` 的当前方案要真的驱动对话后端。

这个文件存在的理由是使用者 2026-09-02 的一句观察：「vox 在进行对话时使用的是我 claude
配置的模型，并没有调用我给其配置的 api key」。他说得对 —— `config/models.toml` 那时
**只有控制台在读写**，运行时一个字都不读，于是「模型配置」那一栏是个能编辑、能保存、
什么都不影响的面板，真正答对话的是 `config/agents.toml` 里的 `relay`。

所以这里的每一条都在钉同一件事的两面：**套用了要真的套上，不套用要说出为什么**。
半套配置（只有 model 没有 base）比不配更难查，所以它整条不套用。

Evidence level: AUTO（纯字典变换，不起进程、不打网络）。
"""

from __future__ import annotations

import pytest

from core.agents.registry import LLM_AGENT, apply_llm_profile
from core.models_config import active_llm, active_profile


def _agents(**overrides):
    entry = {
        "name": LLM_AGENT,
        "kind": "http",
        "enabled": True,
        "url": "https://old.example.com/v1",
        "model": "old-model",
        "key_env": "OLD_KEY",
    }
    entry.update(overrides)
    return {"agents": [{"name": "claude", "kind": "cli", "command": "claude"}, entry]}


@pytest.fixture(autouse=True)
def _credential_in_place(monkeypatch):
    """`FULL` 指名从 `VOX_LLM_KEY` 读凭据，而 `apply_llm_profile` 现在会检查那个变量在不在位。

    不显式设它的话，每条「应当套用」的用例都会落到「凭据不在位」那条分支上 —— 而结果
    取决于跑测试的那个 shell 有没有加载 `.env`。**一个结果取决于环境变量的测试等于没有
    基线**（`core/agents/acp.py` 的 `_UTF8_ENV` 是同一个教训），所以这里把前提写明。
    """
    monkeypatch.setenv("VOX_LLM_KEY", "probe-value-not-a-real-key")


def _target(config):
    return next(entry for entry in config["agents"] if entry["name"] == LLM_AGENT)


FULL = {
    "provider": "custom",
    "model": "claude-opus-5",
    "base": "https://api.example.com/v1",
    "proto": "openai",
    "key_env": "VOX_LLM_KEY",
}


def test_the_active_profile_drives_the_chat_backend():
    """三项都盖上去：端点、模型、去读哪个环境变量。"""
    config, notes = apply_llm_profile(_agents(), FULL)

    target = _target(config)
    assert target["url"] == "https://api.example.com/v1"
    assert target["model"] == "claude-opus-5"
    assert target["key_env"] == "VOX_LLM_KEY"
    assert notes and "models.toml" in notes[0]


def test_the_note_names_what_took_effect():
    """套用了必须留一句话。**这一层存在的全部理由就是「配了但没生效」** ——
    一个静默套用的覆盖层，和一个静默不套用的覆盖层一样难查。"""
    _config, notes = apply_llm_profile(_agents(), FULL)

    assert len(notes) == 1
    for fragment in ("claude-opus-5", "https://api.example.com/v1", "VOX_LLM_KEY"):
        assert fragment in notes[0], fragment


def test_an_unchanged_profile_says_nothing():
    """已经一致时不刷日志：每次启动都报一遍「生效了」会把真的变化埋掉。"""
    same = dict(FULL, model="old-model", base="https://old.example.com/v1", key_env="OLD_KEY")

    _config, notes = apply_llm_profile(_agents(), same)

    assert notes == []


@pytest.mark.parametrize(
    "missing, must_mention",
    [({"base": ""}, "base"), ({"model": ""}, "model")],
)
def test_half_a_profile_is_not_applied(missing, must_mention):
    """只有 model 没有 base = 「新模型 + 旧端点」；反过来是「旧模型名 + 新端点」。
    两种都是半套配置，而半套配置的失败长得像「我配的模型不好用」。"""
    config, notes = apply_llm_profile(_agents(), dict(FULL, **missing))

    target = _target(config)
    assert target["url"] == "https://old.example.com/v1"
    assert target["model"] == "old-model"
    assert notes and must_mention in notes[0]


def test_a_profile_whose_credential_is_missing_is_not_applied(monkeypatch):
    """凭据不在位就整条不套用。

    套用它的后果是每一轮都 401，而 401 在语音里和「网断了」「这个模型不好用」听起来
    完全一样 —— 使用者会去换模型，而缺的是一个环境变量。这道闸是「换一个更快的端点」
    的前提：切端点必然带着切凭据，而新凭据几乎一定比配置文件晚到（配置进版本库，
    凭据在 `.env` 里）。没有它，仓库里改一行 `models.toml` 就等于把使用者的对话弄坏。
    """
    monkeypatch.delenv("VOX_LLM_KEY", raising=False)

    config, notes = apply_llm_profile(_agents(), FULL)

    target = _target(config)
    assert target["url"] == "https://old.example.com/v1"
    assert target["model"] == "old-model"
    assert target["key_env"] == "OLD_KEY"
    assert notes and "VOX_LLM_KEY" in notes[0]


def test_an_empty_key_env_still_applies(monkeypatch):
    """`key_env` 是空串时不查凭据 —— 那是「用 agents.toml 里原来那个变量名」，
    也是一个不带鉴权的本地网关的正确配置。查一个没被指名的变量等于凭空发明一道闸。"""
    monkeypatch.delenv("VOX_LLM_KEY", raising=False)

    config, notes = apply_llm_profile(_agents(), dict(FULL, key_env=""))

    assert _target(config)["model"] == "claude-opus-5"
    assert _target(config)["key_env"] == "OLD_KEY"
    assert notes


def test_a_protocol_the_adapter_cannot_speak_is_refused_not_forced():
    """`HttpAgentAdapter` 只会讲 OpenAI Chat Completions。把 anthropic 的形状硬塞给它，
    回来的是一个格式错误，而使用者会读成「我配的模型不好用」。"""
    config, notes = apply_llm_profile(_agents(), dict(FULL, proto="anthropic"))

    assert _target(config)["model"] == "old-model"
    assert notes and "anthropic" in notes[0]


def test_a_missing_target_is_reported_rather_than_created():
    """`agents.toml` 里没有那条 http 后端时不凭空造一条 —— agent 注册表是那个文件的事。
    但也不能沉默：那正是「配了没生效」。"""
    only_cli = {"agents": [{"name": "claude", "kind": "cli", "command": "claude"}]}

    config, notes = apply_llm_profile(only_cli, FULL)

    assert [entry["name"] for entry in config["agents"]] == ["claude"]
    assert notes and LLM_AGENT in notes[0]


def test_an_empty_profile_changes_nothing_and_says_nothing():
    """没配模型方案是正常状态（出厂就是），不该报警告。"""
    config, notes = apply_llm_profile(_agents(), {})

    assert _target(config)["model"] == "old-model"
    assert notes == []


def test_the_original_config_is_not_mutated():
    """覆盖层返回新字典。原地改会让「配置文件里写的是什么」和「这次跑的是什么」
    在同一个对象上分不开 —— 而控制台读的是前者。"""
    original = _agents()

    apply_llm_profile(original, FULL)

    assert _target(original)["model"] == "old-model"


def test_the_shipped_models_file_names_a_usable_llm():
    """出厂文件自己得说得通：active 指的那一套要有 llm，而且能被套用。"""
    from core.models_config import load_models_config, models_config_path

    config = load_models_config(models_config_path())
    if not config["active"]:
        pytest.skip("这台机器上没有配置模型方案")
    assert active_profile(config), "active 指向的方案不在文件里"
    llm = active_llm(config)
    if not llm:
        pytest.skip("当前方案没有配 llm")
    _config, notes = apply_llm_profile(_agents(), llm)
    assert notes, "出厂方案应当能被套用，或者说清楚为什么不能"
