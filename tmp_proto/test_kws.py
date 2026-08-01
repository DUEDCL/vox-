"""Isolated KWS verification for sherpa-onnx on Windows (.venv only)."""
import sys, time, wave
import numpy as np
import sherpa_onnx

MODEL_DIR = "models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
SUFFIX = "epoch-99-avg-1-chunk-16-left-64"

def read_wav(path):
    with wave.open(path, "rb") as f:
        assert f.getframerate() == 16000, f"need 16k, got {f.getframerate()}"
        assert f.getnchannels() == 1
        assert f.getsampwidth() == 2
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        return (data.astype(np.float32) / 32768.0)

kws = sherpa_onnx.KeywordSpotter(
    encoder=f"{MODEL_DIR}/encoder-{SUFFIX}.int8.onnx",
    decoder=f"{MODEL_DIR}/decoder-{SUFFIX}.int8.onnx",
    joiner=f"{MODEL_DIR}/joiner-{SUFFIX}.int8.onnx",
    tokens=f"{MODEL_DIR}/tokens.txt",
    keywords_file=f"{MODEL_DIR}/keywords.txt",
    num_threads=2,
    provider="cpu",
)

wav_path = sys.argv[1]
samples = read_wav(wav_path)
stream = kws.create_stream()
chunk = int(0.1 * 16000)  # 100 ms chunks, simulating realtime feed
hits = []
t0 = time.perf_counter()
for i in range(0, len(samples), chunk):
    stream.accept_waveform(16000, samples[i:i + chunk])
    while kws.is_ready(stream):
        kws.decode_stream(stream)
    result = kws.get_result(stream)
    if result:
        hits.append((i / 16000.0, result))
        kws.reset(stream)
elapsed = time.perf_counter() - t0
audio_len = len(samples) / 16000.0
print(f"audio: {audio_len:.2f}s, wall: {elapsed:.3f}s, RTF: {elapsed/audio_len:.3f}")
print(f"hits: {hits if hits else 'NONE'}")
# negative control: silence
sil = np.zeros(16000 * 3, dtype=np.float32)
s2 = kws.create_stream()
s2.accept_waveform(16000, sil)
while kws.is_ready(s2):
    kws.decode_stream(s2)
neg = kws.get_result(s2)
print(f"silence control: {neg if neg else 'clean (no false trigger)'}")
del kws
print("resource release OK")
