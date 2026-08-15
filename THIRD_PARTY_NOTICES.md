# Third-Party Notices

> Status: pre-registration (2026-07-22). Entries are added as components are selected in `docs/research/selection-matrix.md` and `docs/adr/001-voice-stack-selection.md`. Nothing listed here is bundled yet unless marked **adopted**.

## Runtime components (candidates)

| Component | Version pinned | License (SPDX) | Source | Status | Notes |
|---|---|---|---|---|---|
| sherpa-onnx | v1.13.4 | Apache-2.0 (code) | https://github.com/k2-fsa/sherpa-onnx | **adopted for prototype** | Per-model licenses tracked below; Windows x64 wheel installed in isolated `.venv` |
| sounddevice | 0.5.5 | MIT | https://github.com/spatialaudio/python-sounddevice | **adopted for prototype** | Local PortAudio capture; no audio upload or persistence in project code |
| silero-vad | 6.2.1 | MIT | https://github.com/snakers4/silero-vad | **adopted for prototype** | ONNX model executed through sherpa-onnx; no telemetry |
| faster-whisper | 1.2.1 | MIT | https://github.com/SYSTRAN/faster-whisper | fallback STT | CTranslate2 (MIT) dependency; Whisper weights MIT (OpenAI) |
| Tauri | v2 | MIT / Apache-2.0 | https://github.com/tauri-apps/tauri | **adopted** (window shell) | See desktop/src-tauri/Cargo.lock for the exact crate tree |
| window-vibrancy | latest | MIT / Apache-2.0 | https://github.com/tauri-apps/window-vibrancy | optional | Only if Acrylic backdrop is added; conflicts with `transparent: true` must be retested |

## Models (candidates — each must be pinned with sha256 before release)

| Model | Source | License | Status | Notes |
|---|---|---|---|---|
| sherpa-onnx-kws-zipformer-wenetspeech-3.3M | https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/ | model card per HF page; training data WenetSpeech (CC BY 4.0) | **adopted for prototype** | Custom keywords via keywords.txt, no retraining; release archive checksum still required |
| 3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx | https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/ (tag typo `recongition` is upstream's) | 3D-Speaker upstream: Apache-2.0 | **adopted** (speaker gate) | SHA-256 `1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b`; 39,593,761 bytes; embedding dim 512; downloaded 2026-08-02, executed through the existing sherpa-onnx runtime — no new dependency |
| silero_vad.onnx | local copy from `D:\program\voxcord\reference\silero-vad` (upstream package data) | MIT | **adopted for prototype** | SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`; replace local provenance with pinned upstream release URL before distribution |
| sherpa-onnx-streaming-zipformer-zh-14M | https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2 | Apache-2.0 (README) | **adopted** (`core/audio/asr.py`, 16 kHz streaming transducer) | zh streaming ASR with endpoint detection; ~74 MB archive
| SenseVoiceSmall | https://modelscope.cn/models/iic/SenseVoiceSmall | **weights license to be captured from ModelScope card** | candidate (fast ASR) | Release blocker: archive license text + screenshot |
| vits-melo-tts-zh_en | https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/ | MeloTTS upstream: MIT | **adopted** (`core/audio/tts.py` synthesis + `playback.py` sounddevice, 44.1 kHz) | zh-en bilingual voice; playback queue and barge-in not wired yet |
| kokoro-multi-lang (sherpa-onnx packaging) | same index | Kokoro weights: Apache-2.0 | TTS A/B alternative | voice-pack provenance to be rechecked |
| matcha-icefall-zh-baker | same index | baker dataset has usage restrictions | excluded from default bundle | reference only |

## Excluded on license grounds

- **piper1-gpl** (GPL-3.0, espeak-ng phonemization): conflicts with closed distribution of the EvoX plugin.
- **BBC audiowaveform** (GPL-3.0): may not be linked; offline tooling only.
- **ShaderToy / CodePen demos**: default non-commercial/restricted terms; concept reference only, no code copied.

## UI / rendering references

| Asset | License | Use | Notes |
|---|---|---|---|
| paper-design/shaders | Apache-2.0 | optional WebGL upgrade path / visual reference | Zero-dependency TS canvas shaders; verified active 2026-07-22 |
| WebGL-Fluid-Simulation (Pavel Dobryakov) | MIT | fluid advection algorithm reference | |
| siriwave (kopiro) | MIT | Gaussian-envelope sine waveform formula reference | Inline reimplementation, no dependency |
| Three.js / OGL / drei | MIT | rejected for v1 (bundle/GPU risk); allowed for v2 | |
| Blinn metaball field algorithm, gooey blur+contrast threshold, SVG feTurbulence displacement | public algorithms / W3C spec | freely implemented | no copyrighted code copied |

## Visual originality statement

The liquid-glass orb is an original implementation: original palette, motion language, and geometry. It does not copy Apple Liquid Glass/Siri assets, icons, trademarks, or pixel-level designs. Generic concepts (translucency, refraction edge, specular highlight, audio-reactive motion) are not protected subject matter.
