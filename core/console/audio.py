"""Decoding the browser's recording. In memory, then gone.

The console records voiceprint samples in the page and posts them here. The path
is deliberately the plainest one available: the browser takes float PCM from
``AudioContext``, downsamples to 16 kHz itself, and writes a WAV header. So this
module needs no codec, no ffmpeg, and no new dependency -- just the standard
library's ``wave`` module over a ``BytesIO``.

Red line 1 applies in full. Nothing here opens a file, and the decoded array is
handed straight to ``SpeakerVerificationProvider.enroll``, which keeps only the
embedding. The bytes are never written, never logged, and never included in an
event or an error message.
"""

from __future__ import annotations

import base64
import binascii
import io
import wave

import numpy as np

#: A generous ceiling for one enrollment phrase. 16 kHz mono 16-bit is 32 kB/s, so
#: this is about 60 seconds -- far more than the 3 seconds the console asks for,
#: and small enough that a malformed or hostile post cannot exhaust memory.
MAX_WAV_BYTES = 2_000_000

EXPECTED_RATE = 16000


class AudioDecodeError(ValueError):
    """Audio that is not what the voiceprint model needs, said plainly."""


def decode_wav_base64(payload: str, *, expect_rate: int = EXPECTED_RATE) -> np.ndarray:
    """One base64 WAV to float32 samples in [-1, 1].

    Every rejection names the actual value found. A recording that silently
    resampled or silently took one channel of two would produce a voiceprint that
    does not match the microphone it will later be verified against, and the
    symptom would appear weeks later as "the gate stopped recognising me".
    """
    if not isinstance(payload, str) or not payload.strip():
        raise AudioDecodeError("no audio was submitted")
    # Strip a data URL prefix if the page sent one.
    if "," in payload[:64] and payload.lstrip().startswith("data:"):
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioDecodeError(f"audio is not valid base64: {type(exc).__name__}") from exc
    if len(raw) > MAX_WAV_BYTES:
        raise AudioDecodeError(f"audio is too long ({len(raw)} bytes, limit {MAX_WAV_BYTES})")
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError(f"audio is not a readable WAV: {exc}") from exc

    if channels != 1:
        raise AudioDecodeError(f"audio must be mono, got {channels} channels")
    if width != 2:
        raise AudioDecodeError(f"audio must be 16-bit PCM, got {width * 8}-bit")
    if rate != expect_rate:
        raise AudioDecodeError(f"audio must be {expect_rate} Hz, got {rate} Hz")
    if not frames:
        raise AudioDecodeError("audio is empty")

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return samples


def quality(samples: np.ndarray, sample_rate: int = EXPECTED_RATE) -> dict[str, float]:
    """RMS, peak, clipping ratio and duration -- the same numbers the gate uses.

    Reported back to the page so a bad phrase can be re-recorded immediately
    instead of being discovered at verification time. These are the input-side
    heuristics from the 2026-08-24 hardening, not an anti-spoof measure.

    ``peak`` 是 2026-08-30 加的，理由是它诊断力最强：实机那次「唤醒全被拒」里每一段的
    peak 都是 **1.000**（削波），而 `clip_ratio` 只有 0.01% —— 不到质量门的 5%，所以那
    件事在读数里完全看不见。峰值贴轨说明增益偏高，说话人特征已经被削掉一部分。
    """
    if samples.size == 0:
        return {"duration_s": 0.0, "rms": 0.0, "clip_ratio": 0.0, "peak": 0.0}
    return {
        "duration_s": round(float(samples.size) / sample_rate, 3),
        "rms": round(float(np.sqrt(np.mean(np.square(samples)))), 5),
        "clip_ratio": round(float(np.mean(np.abs(samples) >= 0.999)), 5),
        "peak": round(float(np.max(np.abs(samples))), 5),
    }


__all__ = ["MAX_WAV_BYTES", "AudioDecodeError", "decode_wav_base64", "quality"]
