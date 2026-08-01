import asyncio

import pytest

from core.providers import VoxCordAdapter


def _voxcord_available() -> bool:
    return VoxCordAdapter().load().available


requires_voxcord = pytest.mark.skipif(
    not _voxcord_available(),
    reason="adjacent VoxCord checkout is not present on this host (optional dependency)",
)


@requires_voxcord
def test_adjacent_voxcord_adapter_loads():
    status = VoxCordAdapter().load()
    assert status.available, status.details
    assert status.details["wake"] == "voxcord"


@requires_voxcord
def test_voxcord_vad_fallback_contract():
    result = asyncio.run(VoxCordAdapter().evaluate_vad([0.0] * 1600))
    assert result["speech"] is False
    assert 0 <= result["confidence"] <= 1
