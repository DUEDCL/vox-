"""Audio playback through the default output device.

Synthesis lives in ``tts.py``; this module owns the device half, for the same
reason KWS/VAD are split from capture: model inference and device I/O are two
different things to test. A fake playback backend is what lets the turn
orchestrator be tested without a speaker.

``sounddevice`` is imported lazily inside the methods, so importing this module
never opens an audio device.
"""

from __future__ import annotations

from typing import Any


class SounddevicePlayback:
    """Play float32 audio on the default output device."""

    def play(self, samples: Any, sample_rate: int, *, blocking: bool = True) -> None:
        """Play one buffer. ``blocking`` waits for it to finish."""
        import sounddevice as sd  # noqa: PLC0415 - lazy: importing opens nothing

        sd.play(samples, sample_rate)
        if blocking:
            sd.wait()

    def stop(self) -> None:
        """Interrupt in-flight playback. Safe to call when nothing is playing."""
        import sounddevice as sd  # noqa: PLC0415

        sd.stop()


__all__ = ["SounddevicePlayback"]
