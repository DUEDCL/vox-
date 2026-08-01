from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

from .base import ProviderStatus, ProviderUnavailable


class VoxCordAdapter:
    """Optional adapter around an adjacent VoxCord checkout.

    The integration is intentionally dynamic: this project remains importable and
    testable without VoxCord, while a configured checkout supplies real local
    wake/VAD/provider implementations.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.getenv("EVOX_VOXCORD_ROOT") or r"D:\program\voxcord"
        self.root = Path(configured)
        self.core_root = self.root / "packages" / "voxcord_core"
        self.core_lib = self.core_root / "lib"
        self._loaded = False

    def load(self) -> ProviderStatus:
        if not self.core_lib.is_dir():
            return ProviderStatus(False, str(self.root), {"reason": "voxcord core not found"})
        for path in (self.core_lib, self.core_root):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        try:
            importlib.import_module("voxcord_core.audio_engine.wake_word")
            importlib.import_module("voxcord_core.vad.silero_adapter")
        except Exception as exc:
            return ProviderStatus(False, str(self.root), {"reason": f"import failed: {exc}"})
        self._loaded = True
        return ProviderStatus(True, str(self.root), {"wake": "voxcord", "vad": "silero-or-rms"})

    def _require(self) -> None:
        if not self._loaded:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])

    def create_wake_providers(self) -> list[Any]:
        self._require()
        module = importlib.import_module("voxcord_core.audio_engine.wake_word")
        return module.create_wake_word_providers()

    def create_vad(self) -> Any:
        self._require()
        config_module = importlib.import_module("voxcord_core.config")
        vad_module = importlib.import_module("voxcord_core.vad.silero_adapter")
        return vad_module.SileroVADService(config_module.load_config())

    async def evaluate_vad(self, samples: list[float]) -> dict[str, Any]:
        service = self.create_vad()
        message_module = importlib.import_module("voxcord_core.core.message")
        return await service.evaluate(message_module.BusMessage("vad.evaluate", data={"samples": samples}))
