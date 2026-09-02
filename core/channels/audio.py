"""微信那一侧的音频进出我们这一侧要过的那道门。

两个方向，两个不同的问题：

* **入站**：微信的语音多是 **SILK**（腾讯自己的编码），而没有纯 Python 的 SILK 解码器。
  所以这里**不假装能解**：认得出的格式（wav / flac / ogg）解成 16 kHz 单声道给 ASR，
  认不出的如实返回 ``None``，让上层退回腾讯自带的 STT 文本。一个把 SILK 字节硬喂给
  识别器的实现会得到一段噪声的转写 —— 那比「我解不了这个格式」难查得多。
* **出站**：TTS 出来的采样率不一定是 16 kHz（本机 VITS 是 44.1 kHz），而微信只是收一个
  文件，所以出站**不重采样**，原样打包成 WAV。

重采样是线性插值。它对语音识别够用（sherpa 的前端自己会做梅尔滤波），而引一个
`scipy.signal.resample_poly` 只为了这一处不值得 —— 这个判断的依据是入站音频本来就已经
过了一次有损编码。
"""

from __future__ import annotations

import io
import wave

import numpy as np

#: 三个模型共同的输入采样率。改它等于换模型（见 `core/audio/config.py`）。
TARGET_RATE = 16_000

#: 能解的格式。**SILK 不在里面，这是事实不是遗漏。**
DECODABLE = frozenset({"wav", "wave", "flac", "ogg", "oga", "opus"})

#: 入站音频的上限。一条微信语音最长 60 秒，16 kHz 单声道 16 bit 约 1.9 MB；
#: 给到 8 MB 是留给高采样率与容器开销的余量，同时挡住「一个 200 MB 的文件」。
MAX_INBOUND_BYTES = 8 * 1024 * 1024


def to_16k_mono(raw: bytes, fmt: str = "") -> np.ndarray | None:
    """微信的音频字节 -> 16 kHz 单声道 float32。解不了返回 ``None``。

    返回 ``None`` 不是失败路径的一部分，是**正常的一种结果**：上层还有腾讯的 STT 文本
    可用。所以这里不抛。
    """
    if not raw or len(raw) > MAX_INBOUND_BYTES:
        return None
    suffix = (fmt or "").strip().lower().lstrip(".")
    if suffix and suffix not in DECODABLE:
        return None
    samples, rate = _read_any(raw)
    if samples is None or rate <= 0:
        return None
    if samples.ndim > 1:
        # 取平均而不是取第一条：微信那边的双声道是同一路声音，平均能少 3 dB 噪声。
        samples = samples.mean(axis=1)
    samples = np.asarray(samples, dtype=np.float32)
    if rate != TARGET_RATE:
        samples = _resample(samples, rate, TARGET_RATE)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 1.0:
        samples = samples / peak
    return samples.astype(np.float32, copy=False)


def _read_any(raw: bytes) -> tuple[np.ndarray | None, int]:
    """先试标准库的 wave（零依赖、最常见），再试 soundfile。"""
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
        if width == 2:
            data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 1:
            # 8 bit WAV 是无符号的。
            data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 4:
            data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            return None, 0
        if channels > 1:
            data = data.reshape(-1, channels)
        return data, int(rate)
    except Exception:  # noqa: BLE001 - 不是 WAV 就换下一条路
        pass
    try:
        import soundfile
    except Exception:  # noqa: BLE001 - 可选依赖
        return None, 0
    try:
        data, rate = soundfile.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception:  # noqa: BLE001 - soundfile 也认不出来
        return None, 0
    return np.asarray(data), int(rate)


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """线性插值重采样。语音识别够用，理由见模块头。"""
    if samples.size == 0 or source_rate == target_rate:
        return samples
    count = int(round(samples.size * target_rate / source_rate))
    if count <= 1:
        return samples[:1]
    source_index = np.linspace(0.0, samples.size - 1, num=count, dtype=np.float64)
    return np.interp(source_index, np.arange(samples.size), samples).astype(np.float32)


def to_wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """float32 -> 16 bit 单声道 WAV 字节。出站用，**不重采样**（见模块头）。

    截幅在写之前做：超过 1.0 的样本按 int16 转换会绕回成反相的尖峰，听起来是「爆音」，
    而它在任何数值断言里都是正常的。
    """
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


__all__ = ["DECODABLE", "MAX_INBOUND_BYTES", "TARGET_RATE", "to_16k_mono", "to_wav_bytes"]
