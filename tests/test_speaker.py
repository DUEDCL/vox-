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
