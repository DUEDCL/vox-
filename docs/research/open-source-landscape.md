# Open-Source Voice Landscape

> Search date: 2026-07-22 (second pass, with live registry verification)

## Evidence quality

Version/license facts marked **[verified]** were fetched live on 2026-07-22 from PyPI JSON API or GitHub API. Facts marked **[analysis]** come from architectural analysis and prior knowledge and must be re-checked before pinning a version. GitHub HTML page fetches fail in the application fetch route, so issue-level claims remain leads.

## Candidates

| Candidate | Role | Latest (verified) | License | Windows | Chinese | Decision |
|---|---|---|---|---|---|---|
| sherpa-onnx | KWS / streaming ASR / TTS runtime | **v1.13.4** [verified, GitHub tags API] | Apache-2.0 (code); per-model licenses vary | Prebuilt win-x64 binaries + wheels; release assets refreshed 2026-07-09 [verified] | Official zh KWS (zipformer-wenetspeech, custom keywords.txt), streaming zh-en ASR, MeloTTS/Matcha/Kokoro-multi-lang TTS models | **Core reuse** (single runtime for KWS+VAD-adjacent+ASR+TTS) |
| silero-vad | VAD | **6.2.1** (2026-02-24) [verified, PyPI] | MIT | Pure Python / ONNX, OS-independent | Language-agnostic, 6000+ languages | **Reuse** (endpointing + barge-in trigger); sherpa-onnx also ships a VAD as fallback |
| openWakeWord | Wake word | v0.6.0 (2024, low maintenance) [analysis] | Apache-2.0 | ONNX backend works | No official zh models; custom training via TTS synthesis | Optional alternative only — sherpa KWS preferred |
| faster-whisper | Offline STT | **1.2.1** (2025-10-31) [verified, PyPI] | MIT (code + OpenAI weights) | Yes (CPU out-of-box; GPU needs CUDA 12 + cuDNN 9 DLLs) | large-v3 / turbo first-tier zh | Fallback STT when license certainty beats latency |
| SenseVoiceSmall (ModelScope/FunASR) | Fast multilingual ASR | 2024-07 release [analysis] | Code MIT; **weights license must be confirmed on ModelScope card** | Via FunASR or sherpa-onnx ONNX | zh flagship, ~15x faster than Whisper-Large, emotion/event tags | Candidate via sherpa-onnx runtime; license check before release |
| Piper / piper1-gpl | TTS | rhasspy/piper archived; OHF-Voice fork | Original MIT; **piper1-gpl is GPL-3.0** (espeak-ng); voices per-model CC-BY-4.0 | win binaries exist | zh_CN-huayan only, weakest quality | **Excluded** (GPL-3.0 + weak zh + archived upstream) |
| Kokoro-82M (hexgrad) | TTS | repo archived 2025; community forks active | Apache-2.0 (code + weights) [analysis] | Pure PyTorch / kokoro-onnx | zh via misaki G2P, noticeably weaker than en | Optional, preferably through sherpa-onnx's kokoro-multi-lang |
| sherpa-onnx TTS models | TTS | ships with runtime | vits-melo-tts-zh_en (MeloTTS, MIT); matcha-icefall-zh-baker (dataset restricted); kokoro-multi-lang | Same as runtime | Best local zh options | **Primary TTS: vits-melo-tts-zh_en**; callback streaming fits `tts.chunk` events |
| OpenVoiceOS (ovos-core) | Full assistant | 1.x active [analysis] | Apache-2.0 | **No real Windows support** (ALSA/D-Bus/systemd) | plugin-dependent | Reference only (solver plugin design) |
| Wyoming / wyoming-satellite | Satellite protocol | wyoming ~1.7 [analysis] | MIT | Satellite binary is Linux-only; **protocol is trivially implementable on Windows** | transport-agnostic | Protocol reference for event design; not a base |
| Rhasspy 2.5 | Full assistant | 2.5.11 (2021, frozen) | MIT | No (Docker/WSL only) | — | Excluded (unmaintained) |
| Pipecat | Realtime orchestration | **v1.6.0** (2026-07-21) [verified, GitHub API] | BSD-2-Clause | Core is cross-platform pure Python (LocalAudioTransport) | via local STT/TTS plugins | Watch-list only — would double the orchestration layer; re-evaluate if VoxCord barge-in/latency fails release blockers |
| LiveKit Agents | Realtime orchestration | 1.x active [analysis] | Apache-2.0 | Officially weak/WSL2; requires LiveKit SFU even for local | via plugins | **Excluded** (SFU round-trip + Windows support) |
| Tauri 2 + WebView2 | Overlay window | v2 (in repo, cargo check passes) | MIT/Apache-2.0 | First-class | — | **Adopted** (transparent always-on-top wake window) |
| paper-design/shaders | Liquid-glass WebGL/canvas shaders | active, pushed 2026-07-22 [verified, GitHub API] | **Apache-2.0** [verified] | Browser/WebView2 | — | High-quality visual reference / optional WebGL upgrade path (zero-dependency, TS) |
| WebGL-Fluid-Simulation (Pavel Dobryakov) | Fluid advection | stable | MIT | Browser | — | Algorithm reference for in-orb fluid perturbation |
| siriwave (kopiro) | Waveform | stable, low activity | MIT | Browser | — | Algorithm reference (Gaussian-envelope sines); inline, no dependency |
| Three.js / OGL / drei | WebGL stack | active | MIT | Browser | — | Rejected for v1 (bundle size + WebView2 GPU risk); kept as v2 upgrade path |
| BBC audiowaveform | Waveform tool | — | **GPL-3.0** | — | — | Excluded from product code |
| ShaderToy / CodePen demos | Visual ideas | — | Default BY-NC / restricted | — | — | Concept reference only, never copy code |

## Key findings (2026-07-22)

1. **One-runtime stack confirmed**: sherpa-onnx v1.13.4 covers KWS (zh custom keywords), streaming zh ASR, SenseVoice, VAD, and streaming-callback TTS on Windows x64 — all local, no keys, Apache-2.0 code. This minimizes dependency surface versus mixing openWakeWord + whisper + piper.
2. **VAD**: silero-vad 6.2.1 (MIT, <1 ms per 30 ms chunk) is the safest endpointing/barge-in trigger; sherpa-onnx's bundled silero VAD model is the fallback to keep one runtime.
3. **TTS decision**: piper excluded on GPL-3.0 fork + weak Chinese; primary = sherpa-onnx `vits-melo-tts-zh_en` (MIT); kokoro-multi-lang as A/B alternative inside the same runtime.
4. **SenseVoiceSmall** remains the latency-optimal ASR but its ModelScope weight license must be captured (screenshot) before release — blocker retained in ADR.
5. **UI**: v1 renderer is self-built Canvas 2D + CSS (no runtime deps). paper-design/shaders (Apache-2.0, zero-dep TS) is the verified-upgrade path if Canvas 2D quality proves insufficient; Three.js stack stays rejected for v1.
6. **EvoX-native assets**: none found (see `evox-community.md`); session bridge will target the local `evox-sessions` MCP channel.

## Privacy / security behavior

- sherpa-onnx, silero-vad, faster-whisper, SenseVoice, Kokoro: fully offline inference, no telemetry, no keys (documented in each project README).
- No candidate uploads continuous microphone streams by default; wake/VAD/STT all run in-process.
- Model downloads happen once at setup from GitHub Releases / Hugging Face / ModelScope; binaries must be pinned by version + sha256 in `THIRD_PARTY_NOTICES.md`.

## Sources (verified live 2026-07-22)

- sherpa-onnx tags: https://api.github.com/repos/k2-fsa/sherpa-onnx/tags (v1.13.4 latest)
- sherpa-onnx release activity: https://github.com/k2-fsa/sherpa-onnx/releases (assets refreshed 2026-07-09)
- silero-vad: https://pypi.org/pypi/silero-vad/json (6.2.1, 2026-02-24, MIT)
- faster-whisper: https://pypi.org/pypi/faster-whisper/json (1.2.1, 2025-10-31, MIT)
- pipecat: https://api.github.com/repos/pipecat-ai/pipecat/releases/latest (v1.6.0, 2026-07-21)
- paper-design/shaders: https://api.github.com/repos/paper-design/shaders (Apache-2.0, pushed 2026-07-22)

## Sources (leads, re-verify before pinning)

- [sherpa-onnx KWS models](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html) · [TTS models](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/index.html)
- [openWakeWord](https://github.com/dscripka/openWakeWord) · [Silero VAD](https://github.com/snakers4/silero-vad)
- [SenseVoiceSmall](https://modelscope.cn/models/iic/SenseVoiceSmall) · [FunASR](https://github.com/modelscope/FunASR)
- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) · [piper voices](https://github.com/rhasspy/piper/blob/master/VOICES.md) · [piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)
- [ovos-core](https://github.com/OpenVoiceOS/ovos-core) · [wyoming](https://github.com/OHF-voice/wyoming) · [wyoming-satellite](https://github.com/rhasspy/wyoming-satellite) · [rhasspy](https://github.com/rhasspy/rhasspy)
- [LiveKit Agents](https://github.com/livekit/agents) · [window-vibrancy](https://github.com/tauri-apps/window-vibrancy)
