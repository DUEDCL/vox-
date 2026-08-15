"""Local audio providers.

Every class here is lazy: importing this package never loads a model, never
opens a microphone, and never raises when an optional runtime is missing.
``SounddeviceWakeCapture`` is the only place in the project that touches a
capture device, which keeps design red line 1 (local-first, no audio persisted
or uploaded) enforceable at a single boundary.
"""

from __future__ import annotations

from .base import ProviderStatus, ProviderUnavailable
from .capture import SounddeviceWakeCapture
from .kws import SherpaKeywordProvider
from .playback import SounddevicePlayback
from .ring import AudioRingBuffer
from .speaker import (
    EnrollmentResult,
    SpeakerStore,
    SpeakerVerificationProvider,
    VerificationResult,
    load_speaker_config,
)
from .tts import SherpaTtsProvider, TtsAudio
from .vad import SherpaVadProvider
from .voxcord import VoxCordAdapter

__all__ = [
    "AudioRingBuffer",
    "EnrollmentResult",
    "ProviderStatus",
    "ProviderUnavailable",
    "SherpaKeywordProvider",
    "SherpaTtsProvider",
    "SherpaVadProvider",
    "SounddevicePlayback",
    "SounddeviceWakeCapture",
    "SpeakerStore",
    "SpeakerVerificationProvider",
    "TtsAudio",
    "VerificationResult",
    "VoxCordAdapter",
    "load_speaker_config",
]
