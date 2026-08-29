# Third-Party Notices

> Status: pre-registration (2026-07-22). Entries are added as components are selected in `docs/research/selection-matrix.md` and `docs/adr/001-voice-stack-selection.md`. Nothing listed here is bundled yet unless marked **adopted**.

## Runtime components (candidates)

| Component | Version pinned | License (SPDX) | Source | Status | Notes |
|---|---|---|---|---|---|
| sherpa-onnx | v1.13.4 | Apache-2.0 (code) | https://github.com/k2-fsa/sherpa-onnx | **adopted for prototype** | Per-model licenses tracked below; Windows x64 wheel installed in isolated `.venv` |
| sounddevice | 0.5.5 | MIT | https://github.com/spatialaudio/python-sounddevice | **adopted for prototype** | Local PortAudio capture; no audio upload or persistence in project code |
| pypinyin | 0.55.0 | MIT | https://github.com/mozillazg/python-pinyin | **adopted** (`core/audio/keywords.py`) | Pure-Python + data tables, no network. Build-time only: converts a Chinese keyword to the initial/final phonemes the KWS model is trained on. Imported inside the function, so a machine running a pre-made `keywords.txt` does not need it. Chosen over `sherpa_onnx.utils.text2token`, which imports `sentencepiece` unconditionally for a branch this path never takes |
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
| sherpa-onnx-streaming-zipformer-multi-zh-hans-2023-12-12 | https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models | Apache-2.0 (icefall recipe) | **adopted 2026-08-29**, replaces `zh-14M-2023-02-23` | Measured on four real recordings from this machine: CER 14.1% vs the 14M model's 21.4%; the long sentence 「检查目前运行状态是否正常」 went from 18.8% to 6.2% (exact). RTF 0.061 int8. BPE modelling (2002 tokens + `bpe.model`) — its mid-stream `get_result` raises `vector too long`, which is why `core/audio/asr.py` swallows partial-result errors and takes the final text from `finalize()` |
| kokoro-multi-lang (sherpa-onnx packaging) | same index | Kokoro weights: Apache-2.0 | TTS A/B alternative | voice-pack provenance to be rechecked |
| matcha-icefall-zh-baker | same index | baker dataset has usage restrictions | excluded from default bundle | reference only |

## Excluded on license grounds

- **piper1-gpl** (GPL-3.0, espeak-ng phonemization): conflicts with closed distribution of the EvoX plugin.
- **BBC audiowaveform** (GPL-3.0): may not be linked; offline tooling only.
- **ShaderToy / CodePen demos**: default non-commercial/restricted terms; concept reference only, no code copied.

## Optional external services (not dependencies, off by default)

Neither of these is a Python package and neither ships enabled. They are endpoints a *user* may
choose to point the platform at, so what is recorded here is the terms of that choice.

| Service | Reached by | Ships enabled | Terms note |
|---|---|---|---|
| **SearxNG** (self-hosted) | `core/tools/search_backends.py` → `SearxBackend`, `[web] searx_url` | **No** (`searx_url = ""`) | AGPL-3.0 **as a program**, but Vox neither bundles nor links it — it is a separate service the user runs and reaches over loopback HTTP. No code is copied. Zero keys, zero third parties. |
| **DuckDuckGo HTML endpoint** | `core/tools/search_backends.py` → `DuckDuckGoBackend`, `[web] allow_internet` | **No** (`allow_internet = false`) | No API key and no account, which is why it is reachable under red line 1 at all. It **is** an outbound request to a third party: enabling it means every `web.search` query leaves the machine. Automated access is governed by DuckDuckGo's terms; only title / URL / snippet are kept, page text is discarded, and the parser reports an unrecognised page as a failure rather than as "no results". |
| **MCP servers** (any) | `core/tools/mcp.py`, `config/mcp.toml` | **No** (three layers off: `[mcp] enabled`, per-server `enabled`, `require_confirmation`) | Each server is third-party code the user chooses to run as a subprocess, under its own licence. Vox implements the client side of the public [Model Context Protocol](https://modelcontextprotocol.io) wire format (JSON-RPC 2.0 over stdio); no server code is bundled. The child gets `scrubbed_env()`, so a credential reaches it only by naming the variable in `env_passthrough`. |

## UI / rendering references

| Asset | License | Use | Notes |
|---|---|---|---|
| paper-design/shaders | Apache-2.0 | optional WebGL upgrade path / visual reference | Zero-dependency TS canvas shaders; verified active 2026-07-22 |
| WebGL-Fluid-Simulation (Pavel Dobryakov) | MIT | fluid advection algorithm reference | No longer used: the fluid-blob core was replaced by the standing-wave core on 2026-08-26 |
| siriwave (kopiro) | MIT | Gaussian-envelope sine waveform formula reference | Inline reimplementation, no dependency |
| Three.js / OGL / drei | MIT | rejected for v1 (bundle/GPU risk); allowed for v2 | |
| Blinn metaball field algorithm, gooey blur+contrast threshold, SVG feTurbulence displacement | public algorithms / W3C spec | freely implemented | no copyrighted code copied |
| Lissajous curve, AM envelope, hard clipping | public mathematics | freely implemented in `desktop/src/core.ts` | closed-form functions; nothing copied |

## Visual originality statement

The standing-wave orb (`desktop/src/core.ts` + `desktop/src/style.css`, 2026-08-26) is an original
implementation: original palette, motion language, and geometry. State is encoded as **waveform
topology** — near-line, sine, Lissajous knot, AM envelope, decaying tail, clipped square — driven by
the app's own `amplitude` and `task.progress.agents` signals. Cavity shading is a single radial
gradient plus one inner stroke and one top highlight arc.

Superseded on 2026-08-26: the earlier orb carried a fluid-glass core (two counter-rotating gradient
blobs behind a double inset-shadow shell) plus eyes, blush and blink cycles, whose visual recipe was
credited in-file to `kkclaw`. That layer is now **fully removed** — no gradient-blob core, no
inset-shadow shell recipe, no facial elements. The removal also settles a bookkeeping conflict worth
recording: the old CSS header credited `kkclaw` as MIT, while `docs/handoff.md` records the reference
checkout under a *Claw Desktop Pet License* (personal use, resale prohibited), and this file never
listed it at all. The reference checkout is no longer on disk, so which licence applied could not be
re-verified — and with the recipe gone the question no longer gates distribution.

No Apple Liquid Glass/Siri assets, icons, trademarks, or pixel-level designs are copied. Generic
concepts (translucency, specular highlight, audio-reactive motion) are not protected subject matter.
