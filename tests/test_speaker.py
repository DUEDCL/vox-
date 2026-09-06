"""Speaker verification: store behaviour and the fail-closed guarantees.

Everything here runs without the 37 MB speaker model, because the properties
being checked are the ones that must hold *especially* when the model is missing:
a gate that silently opens on a missing model is worse than no gate.

Model-dependent enrollment and scoring are covered separately once the model is
present (see docs/testing.md, REAL-MIC row).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from core.audio import (
    ProviderUnavailable,
    SpeakerStore,
    SpeakerVerificationProvider,
)

VECTORS = {"due": [[0.111111, 0.222222, 0.333333], [0.444444, 0.555555, 0.666666]]}


def test_store_round_trip(tmp_path):
    store = SpeakerStore(tmp_path / "voiceprints.json")
    assert store.load() == {}

    store.save(VECTORS, dim=3)
    assert store.load() == VECTORS

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["dim"] == 3


def test_store_write_is_atomic(tmp_path):
    """An interrupted save must not be able to leave a half-written store."""
    store = SpeakerStore(tmp_path / "nested" / "voiceprints.json")
    store.save(VECTORS, dim=3)

    assert store.path.is_file()
    # The temp file the save wrote through must not survive it.
    assert list(store.path.parent.glob("*.tmp")) == []


def test_store_rejects_unsupported_version(tmp_path):
    path = tmp_path / "voiceprints.json"
    path.write_text(json.dumps({"version": 99, "speakers": {}}), encoding="utf-8")

    with pytest.raises(ProviderUnavailable, match="unsupported version"):
        SpeakerStore(path).load()


def test_store_rejects_corrupt_json(tmp_path):
    path = tmp_path / "voiceprints.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ProviderUnavailable, match="unreadable"):
        SpeakerStore(path).load()


def _provider(tmp_path, **kwargs) -> SpeakerVerificationProvider:
    """A provider pointed at a model that does not exist."""
    return SpeakerVerificationProvider(
        tmp_path / "absent-model.onnx",
        store_path=tmp_path / "voiceprints.json",
        **kwargs,
    )


def test_missing_model_reports_unavailable_rather_than_raising(tmp_path):
    provider = _provider(tmp_path)
    status = provider.load()

    assert status.available is False
    assert "not found" in status.details["reason"]


def test_verify_without_a_model_rejects(tmp_path):
    """Fail-closed: no model means no match, never an accidental pass."""
    # Audible input on purpose: silence now stops at the quality gate before
    # the model check ever runs (see tests/test_speaker_hardening.py).
    audible = (np.sin(np.linspace(0, 400, 16000)) * 0.2).astype(np.float32)
    result = _provider(tmp_path).verify(audible)

    assert result.accepted is False
    assert result.speaker is None
    assert "not found" in result.reason


def test_verify_with_nobody_enrolled_rejects(tmp_path):
    """An enrollment-free gate is not a gate; it must not accept anyone."""
    provider = _provider(tmp_path)
    provider.store.save({}, dim=192)

    assert provider.verify([0.0] * 16000).accepted is False


def test_embed_refuses_audio_shorter_than_the_minimum(tmp_path):
    provider = _provider(tmp_path, min_verify_seconds=0.6)

    with pytest.raises(ProviderUnavailable):
        provider.embed([0.0] * 800)  # 50 ms


def test_enroll_rejects_an_empty_name(tmp_path):
    with pytest.raises((ValueError, ProviderUnavailable)):
        _provider(tmp_path).enroll("  ", [[0.0] * 16000])


def test_describe_never_returns_raw_vectors(tmp_path):
    """Enrollment data is biometric; describe() is the only sanctioned view."""
    provider = _provider(tmp_path)
    provider.store.save(VECTORS, dim=3)

    described = provider.describe()

    assert described["speakers"] == ["due"]
    assert described["samples_per_speaker"] == {"due": 2}
    serialised = json.dumps(described)
    for vector in VECTORS["due"]:
        for value in vector:
            assert str(value) not in serialised


def test_remove_deletes_an_enrollment(tmp_path):
    provider = _provider(tmp_path)
    provider.store.save(VECTORS, dim=3)

    assert provider.remove("due") is True
    assert provider.store.load() == {}
    assert provider.remove("due") is False


# ------------------------------------------- 门按盘上的档案判，不按启动时的快照判


def _loaded(tmp_path, *, dim: int = 3):
    """一个「已加载」的 provider：真 manager（只要一个维度，不需要模型文件），
    哨兵 extractor（`_require()` 只看它是不是 None）。"""
    import sherpa_onnx

    provider = _provider(tmp_path)
    provider._dim = dim
    provider._extractor = object()
    provider._manager = sherpa_onnx.SpeakerEmbeddingManager(dim)
    provider._restore()
    return provider


def test_an_enrollment_made_by_another_process_is_picked_up(tmp_path):
    """2026-08-30 实机那条：**脚本注册成功了，正在跑的控制台却认不出。**

    档案落在文件里，而校验比的是内存里那份 `SpeakerEmbeddingManager` —— 它只在
    `load()` 时装一次。于是 `scripts/enroll_speaker.py`（另一个进程）注册完之后，正在跑
    的进程一无所知：脚本自己的闭环校验 0.819「通过」，控制台页面也显示新档案在，而真正
    做决定的那一份是旧的。时间线是可核的：控制台 02:51:00 启动，档案文件 02:55:13 才写。
    """
    provider = _loaded(tmp_path)
    assert provider._manager.num_speakers == 0

    # 另一个进程注册了一个人。这个进程只看得见文件。
    provider.store.save({"du": [[1.0, 0.0, 0.0]]}, dim=3)

    assert provider.refresh() is True
    assert sorted(provider._manager.all_speakers) == ["du"]
    assert provider.store_reloads == 1


def test_an_unchanged_store_is_not_reloaded(tmp_path):
    """没变就不重装 —— 这一步在每次唤醒的路径上，代价必须是一次 ``stat()``。"""
    provider = _loaded(tmp_path)
    provider.store.save({"du": [[1.0, 0.0, 0.0]]}, dim=3)
    assert provider.refresh() is True

    assert provider.refresh() is False
    assert provider.refresh() is False
    assert provider.store_reloads == 1


def test_verify_itself_reloads_so_a_fresh_enrollment_takes_effect(tmp_path):
    """断言的是 ``verify()`` **自己**会重读，不是「调用方记得先 refresh」。

    调用方是音频回调（`capture._authorise`），而它不该知道档案存在哪、什么时候变。
    """
    provider = _loaded(tmp_path)
    speech = (np.sin(np.linspace(0, 400.0, 16000)) * 0.3).astype(np.float32)
    assert provider.verify(speech).reason == "no speaker enrolled"

    provider.store.save({"du": [[1.0, 0.0, 0.0]]}, dim=3)
    provider.embed = lambda samples, sample_rate=16000: [1.0, 0.0, 0.0]

    result = provider.verify(speech)

    assert result.accepted is True
    assert result.speaker == "du"


def test_a_broken_store_keeps_the_profiles_it_already_had(tmp_path):
    """重装失败要**保留旧档案**。把门清空会连本人一起挡在外面，那比多拒一次严重得多。"""
    provider = _loaded(tmp_path)
    provider.store.save({"du": [[1.0, 0.0, 0.0]]}, dim=3)
    assert provider.refresh() is True

    provider.store.path.write_text("{ 坏了", encoding="utf-8")

    assert provider.refresh() is False
    assert sorted(provider._manager.all_speakers) == ["du"], "旧档案必须还在"
    assert provider.store_reload_errors == 1


def test_describe_reports_what_the_gate_actually_holds(tmp_path):
    """`speakers` 读文件、`live_speakers` 读门。两个都报，因为它们能不一致 ——
    而不一致正是「注册了却唤不醒」的形状。"""
    provider = _loaded(tmp_path)
    provider.store.save({"du": [[1.0, 0.0, 0.0]]}, dim=3)

    described = provider.describe()

    assert described["speakers"] == ["du"]
    assert described["live_speakers"] == ["du"], "describe() 开头会先对齐"
    assert described["store_reload_errors"] == 0
