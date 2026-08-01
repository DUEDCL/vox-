# ADR 001: Voice Stack Selection

## Status

Accepted for prototype implementation; not release-approved. Updated 2026-07-28 with t10/t11 prototype evidence (see `docs/research/prototype-results.md`).

## Decision

### Voice core (release path)

Use **sherpa-onnx 1.13.4** as the publish-path local voice runtime, behind the project's versioned provider/event contracts. One runtime dependency boundary covers local Chinese KWS (wake), Silero VAD (endpointing/barge-in), streaming ASR, and VITS TTS. Reuse adjacent VoxCord selectively for architecture and test/reference material (the user confirmed it is their own project, so the distribution-license blocker is resolved). Keep wake, VAD, ASR, TTS, and the EvoX session bridge replaceable behind the provider contracts in `core/providers.py`; no third-party SDK type leaks into the public event schema.

t10 evidence (AUTO+SIM, this Windows `.venv`): KWS loads in 0.63 s, VAD in 0.05 s; synthesized `你好问问` produces a wake hit (decode RTF ~0.002); 8 open/close cycles leak no python memory (0.01 MB peak growth); 12 s of streamed silence yields zero spurious hits at RTF 0.019; two distinct mock transports drive an identical turn path; a barge-in `cancel` during `speaking` emits `turn.cancelled`, tells the transport to cancel the pending turn, and returns cleanly to idle.

### EvoX bridge

Use `LocalEvoXTransport` — an authenticated localhost HTTP bridge (`core/session_bridge.py`) behind the `ConversationTransport` protocol. Rationale: EvoX exposes no stable in-process conversation endpoint to this runtime, and the transport must stay swappable. Security posture: bearer token required; plain HTTP accepted only for validated loopback hosts (lookalike hosts such as `localhost.evil.example` rejected); URL credentials rejected; cancellation IDs URL-encoded; responses without `turn_id` rejected. A mock `ConversationTransport` keeps automated tests deterministic without a live EvoX session.

### UI render route

Adopt the layered approach: **Canvas 2D fluid renderer (v1 primary) + CSS glass compositing + static-CSS degradation**, behind a `Renderer` interface so a WebGL shader can replace the Canvas implementation in v2 without touching the app coordinator. Do not ship Three.js or any large render framework in v1 (transparent-WebView2 GPU/bundle risk).

t11 evidence (SIM, headless Chromium, DPR 1): all three routes render at the display's full cadence (~240 fps in a 2 s window); WebGL compiles/links with no error on this host; amplitude input clamps to `[0.12, 1.0]`; degradation signals (`prefers-reduced-motion`, `hardwareConcurrency`, Canvas/WebGL context, `devicePixelRatio`) are all queryable, giving the quality-tier selector its full input set; zero console errors; distinct screenshots captured per route.

### Fallback implementations

- Wake: `openWakeWord` only if sherpa KWS fails a real-mic quality bar.
- STT: `faster-whisper` / SenseVoiceSmall (via sherpa-onnx) as alternate ASR.
- TTS: Kokoro-82M via sherpa-onnx wrapping if MeloTTS quality is insufficient.
- UI: WebGL shader route (paper-design/shaders, Apache-2.0, as reference) is the v2 visual upgrade; static CSS is the guaranteed low-end / reduced-motion / Canvas-unavailable degradation tier.

The current Tauri shell remains the independent desktop layer. No third-party complete assistant is adopted as the base.

## Rationale

The available EvoX/EvoMap entry point yielded no verifiable native voice asset (`docs/research/evox-community.md`). Live candidate research found no complete Windows assistant that already combines local Chinese wake, an independent always-on-top UI, continuous conversation, and an EvoX session bridge (`docs/research/open-source-landscape.md`, `selection-matrix.md`). Sherpa-onnx is the strongest runtime candidate because its verified Windows package and model family cover local KWS and the planned ASR/TTS paths in one dependency boundary.

OpenVoiceOS, Pipecat, Voice Satellite, openWakeWord, LiveKit Agents, Rhasspy, and Piper remain references, fallbacks, or exclusions rather than the product base (see the selection matrix for per-candidate reasons and evidence).

## Verified (as of 2026-07-28)

- Windows startup, model loading, local Chinese wake on synthesized audio, resource release, continuous run, swappable backend, interruptible-TTS orchestration (t10).
- One real-microphone spoken `你好问问` wake hit at 7.19 s (REAL-MIC, 2026-07-26).
- Render-route feasibility, FPS, amplitude clamp, and degradation-signal availability for Canvas 2D / CSS / WebGL (t11).
- Python suite green (19 passed, 2 skipped — VoxCord optional), smoke + simulated E2E pass.

## Required before release (blockers)

- Real-microphone Chinese wake **quality** acceptance (quiet / far-field / noisy / repeated), not just a single hit.
- Real-microphone Silero endpointing acceptance.
- Live **EvoX session bridge** (REAL-EVOX): text send, incremental replies, TTS, continuous follow-ups, cancel, timeout, reconnect.
- Real streaming first-token latency.
- Independent **transparent always-on-top window** (REAL-WIN): transparency compositing in the real WebView2 runtime, DPI at 125/150/175%, multi-monitor placement, taskbar avoidance, focus, click-through, tray persistence, RDP software-render fallback.
- Sustained-run resource profile (≥30 min CPU/memory/FPS across idle/listening/thinking/speaking).
- Keep wake, ASR, LLM, and TTS providers replaceable (contract-enforced; no SDK type leakage into the event schema).
