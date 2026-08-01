"""Synthesize the Chinese wake phrase, then run VAD and KWS on the result."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import sherpa_onnx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.providers import SherpaKeywordProvider, SherpaVadProvider
TTS_DIR = ROOT / "models" / "vits-melo-tts-zh_en"
KWS_DIR = ROOT / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
VAD_MODEL = ROOT / "models" / "silero_vad.onnx"
OUTPUT = ROOT / "tmp_proto" / "tts_nihao.wav"


def synthesize(text: str) -> tuple[np.ndarray, int, float]:
    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=str(TTS_DIR / "model.onnx"),
        lexicon=str(TTS_DIR / "lexicon.txt"),
        tokens=str(TTS_DIR / "tokens.txt"),
        dict_dir=str(TTS_DIR / "dict"),
    )
    model = sherpa_onnx.OfflineTtsModelConfig(vits=vits, num_threads=2, provider="cpu")
    rules = ",".join(str(TTS_DIR / name) for name in ("date.fst", "number.fst", "phone.fst", "new_heteronym.fst"))
    tts = sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(model=model, rule_fsts=rules, max_num_sentences=1)
    )
    started = time.perf_counter()
    audio = tts.generate(text)
    elapsed = time.perf_counter() - started
    samples = np.asarray(audio.samples, dtype=np.float32)
    sf.write(OUTPUT, samples, audio.sample_rate)
    return samples, audio.sample_rate, elapsed


def resample(samples: np.ndarray, source_rate: int, target_rate: int = 16000) -> np.ndarray:
    target_length = round(len(samples) * target_rate / source_rate)
    source_time = np.arange(len(samples), dtype=np.float64) / source_rate
    target_time = np.arange(target_length, dtype=np.float64) / target_rate
    return np.interp(target_time, source_time, samples).astype(np.float32)


def main() -> None:
    text = "你好问问"
    samples, source_rate, elapsed = synthesize(text)
    audio = resample(samples, source_rate)

    vad = SherpaVadProvider(VAD_MODEL, min_speech_duration=0.1, min_silence_duration=0.2)
    vad_status = vad.load()
    segments = []
    for offset in range(0, len(audio), 512):
        segments.extend(vad.feed(audio[offset : offset + 512])["segments"])
    for _ in range(20):
        segments.extend(vad.feed(np.zeros(512, dtype=np.float32))["segments"])
    segments.extend(vad.flush()["segments"])

    kws = SherpaKeywordProvider(KWS_DIR)
    kws_status = kws.load()
    stream = kws.create_stream()
    hits: list[str] = []
    for offset in range(0, len(audio), 1600):
        hits.extend(kws.feed(stream, audio[offset : offset + 1600]))
    for _ in range(10):
        hits.extend(kws.feed(stream, np.zeros(1600, dtype=np.float32)))

    result = {
        "text": text,
        "tts_sample_rate": source_rate,
        "tts_samples": len(samples),
        "tts_seconds": len(samples) / source_rate,
        "tts_generation_seconds": elapsed,
        "tts_rtf": elapsed / (len(samples) / source_rate),
        "vad_available": vad_status.available,
        "vad_segments": segments,
        "kws_available": kws_status.available,
        "kws_hits": hits,
        "hit": text in hits,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    vad.close()
    kws.close()
    if text not in hits:
        raise SystemExit("TTS audio did not trigger the expected keyword")


if __name__ == "__main__":
    main()
