"""Backwards-compatible re-export shell.

The provider implementations moved into ``core.audio`` so that the audio stack
can grow (speaker verification, ASR, TTS) without one module accumulating four
different abstraction levels. Existing imports keep working:

    from core.providers import SherpaKeywordProvider   # still valid
    from core.audio import SherpaKeywordProvider       # preferred
"""

from __future__ import annotations

from core.audio import (
    ProviderStatus,
    ProviderUnavailable,
    SherpaKeywordProvider,
    SherpaTtsProvider,
    SherpaVadProvider,
    SounddeviceWakeCapture,
    TtsAudio,
    VoxCordAdapter,
)

__all__ = [
    "ProviderStatus",
    "ProviderUnavailable",
    "SherpaKeywordProvider",
    "SherpaTtsProvider",
    "SherpaVadProvider",
    "SounddeviceWakeCapture",
    "TtsAudio",
    "VoxCordAdapter",
]
