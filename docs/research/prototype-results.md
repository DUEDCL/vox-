# Prototype Results

> Started: 2026-07-22

## Validation levels (legend)

Every claim below is tagged by how it was established. Do not promote a lower level to a higher one without new evidence.

- **DOC** — documented upstream claim or checked-in report from another repo; not re-run here.
- **AUTO** — automated test / script run in this workspace's `.venv` or `desktop`, deterministic, no external hardware.
- **SIM** — simulated/synthesized input (mock transport, synthesized audio, headless browser); exercises real code paths but not real devices.
- **REAL-MIC** — real microphone on this Windows host.
- **REAL-AGENT** — a real external agent process was launched, streamed real output, and completed a turn. Mock subprocesses are SIM.
- **REAL-EVOX** — real EvoX session over the live bridge.
- **REAL-WIN** — real Tauri/WebView2 window behavior (transparency, DPI, multi-monitor, RDP).

Current status: DOC/AUTO/SIM and one REAL-MIC wake are established. REAL-AGENT, REAL-EVOX and REAL-WIN acceptance are **not yet done** and remain release blockers.

## Environment of record (2026-07-28)

- OS: Windows 11 Pro (10.0.26200). Host `.venv` Python **3.12.10**.
- Python deps: `sherpa-onnx 1.13.4`, `sherpa-onnx-core 1.13.4`, `numpy 2.5.1`, `sounddevice 0.5.5`, `soundfile 0.14.0`, `pytest 9.1.1`.
- Frontend: Node **v24.11.1**, npm **11.7.0**, TypeScript 5.6.x, Vite 8.1.x, Tauri 2.x.
- Models present under `models/`: KWS `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`, `silero_vad.onnx`, MeloTTS `vits-melo-tts-zh_en`.

## Reproduction commands

| Purpose | Command | Level |
|---|---|---|
| Python suite | `.venv/Scripts/python.exe -m pytest tests -q` → 101 passed, 2 skipped | AUTO |
| Speaker gate (model-free) | `.venv/Scripts/python.exe -m pytest tests/test_speaker.py tests/test_speaker_privacy.py -q` | AUTO |
| Speaker gate (real model) | `.venv/Scripts/python.exe -m pytest tests/integration/test_speaker_model.py -q` | AUTO |
| Event + registry contracts | `.venv/Scripts/python.exe -m pytest tests/test_agent_event_schema.py -q` → 34 passed | AUTO |
| Voiceprint enrollment | `.venv/Scripts/python.exe scripts/enroll_speaker.py --name <名字>` | REAL-MIC |
| Voice smoke | `.venv/Scripts/python.exe scripts/smoke_voice.py` | SIM |
| Simulated E2E | `.venv/Scripts/python.exe scripts/e2e_simulated.py` | SIM |
| t10 stack validation | `.venv/Scripts/python.exe tmp_proto/t10_voice_stack_validation.py` | AUTO+SIM |
| TTS→VAD→KWS loop | `.venv/Scripts/python.exe tmp_proto/tts_kws_vad.py` | SIM |
| Live mic wake | `.venv/Scripts/python.exe tmp_proto/live_wake.py` | REAL-MIC |
| Orb render spike | serve `tmp_proto/orb_spike.html`, drive `window.__SPIKE__` | SIM |

## Completed

- Target workspace initially contained only the plan asset.
- Adjacent VoxCord contains the planned core, desktop shell, local wake providers, references, tests, and Windows packaging documentation.
- Its checked-in health report records 124 core tests, 14/14 desktop E2E tests, successful TypeScript checks, Windows Tauri bundle generation, local KWS readiness, and zero cloud calls for local wake tests.

## Session 2026-07-22 (plugin tools + simulated E2E)

Verified in this workspace, this session:

- `python -m pytest tests -q` → 15 passed (contract, state machine, provider adapter, session bridge, plugin tools).
- `python scripts/smoke_voice.py` → OK.
- `python scripts/e2e_simulated.py` → OK (mock transport; wake → ASR → bridge send → reply → TTS events → continuous listening → cancel → stop).
- `desktop`: `npm run build` (tsc + vite) → OK; `cargo check` on `desktop/src-tauri` → OK, zero warnings.

Implemented this session:

- Plugin tool surface: `pause`, `resume`, `wake_test` (synthetic marker), `complete_turn` (llm.delta/tts.chunk/turn.done with speaking → listening continuous-conversation transition), `devices` (optional sounddevice backend), `diagnose` (provider/bridge readiness, no credential leakage), optional `ConversationTransport` wiring in `submit_text`/`cancel`.
- Fixed an unused-import warning in `desktop/src-tauri/src/main.rs`.

Verification levels: all of the above are automated/simulated. No real microphone, no real EvoX session, no live wake model was exercised.

## Carried forward from the prior session

- Real microphone wake run in the current Windows environment.
- Live EvoX conversation bridge.
- Real streaming first-token latency.
- Separate always-on-top, skip-taskbar wake window (defined and compiling, not visually accepted).
- Fresh isolated installs of candidates other than sherpa-onnx.

## Session 2026-07-23 (isolated Sherpa provider + routines)

Verified in this workspace:

- Added `SherpaKeywordProvider` and optional `SounddeviceWakeCapture` in `core/providers.py`. Runtime imports are lazy; missing model, Sherpa, or sounddevice produces a diagnostic/unavailable result rather than breaking the plugin import path.
- Added `tests/test_sherpa_provider.py`, plugin capture lifecycle tests, and hardened bridge tests; full Python suite is now **21 passed**.
- Sherpa-ONNX `1.13.4` loads the downloaded Chinese KWS model from `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`.
- Streaming 100 ms chunks ran at **RTF 0.016** on the bundled test wav; the 3-second silence control produced no hit and native inference state released cleanly.
- `python scripts/smoke_voice.py` passed.
- `python scripts/e2e_simulated.py` passed; this remains simulated and does not use a microphone or live EvoX session.
- `desktop`: `npm run build` passed.
- `desktop/src-tauri`: `cargo check` passed after `cargo clean` removed stale build artifacts containing the old path `D:\program\vioce-wake\语音唤醒对话`.
- Added [docs/routines.md](../routines.md) with repeatable commands and the appropriate trigger for each routine.
- Installed `sounddevice 0.5.5` in `.venv`; enumerated Realtek, ToDesk virtual, DirectSound, WASAPI, and WDM-KS inputs.
- Copied the MIT Silero ONNX model from the adjacent checked-out upstream package data for isolated prototyping; SHA-256 is `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.
- Sherpa Silero VAD detected a speech segment in the bundled 8.03-second KWS wav and rejected generated silence.
- A 3-second local Realtek microphone capture completed in 3.142 seconds (`RMS 0.000044`, peak `0.001068`), produced no VAD segment in the quiet environment, saved/uploaded no audio, and released the device.
- `SounddeviceWakeCapture` and the plugin `start → pause → resume → stop` lifecycle opened and released the real microphone successfully without a wake hit in the quiet sample.
- Hardened `LocalEvoXTransport`: parsed loopback validation rejects lookalike hosts such as `localhost.evil.example`, rejects URL credentials, URL-encodes cancellation IDs, and rejects turn responses without `turn_id`.

The bundled KWS test wav produced detections for the model's test keyword file. It was not a recording of `你好问问`, so this is a runtime/pipeline verification, not yet a Chinese wake-word quality acceptance test.

## TTS download status — RESOLVED

The archive was resumed to a complete **160 MiB** file on 2026-07-23. `bzip2 -tv` passed, and it was extracted to `models/vits-melo-tts-zh_en`.

## Session 2026-07-23 (TTS → VAD → KWS closed loop)

- Silero VAD model copied from the adjacent silero-vad reference (MIT): `models/silero_vad.onnx`, SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.
- Added `SherpaVadProvider` in `core/providers.py` (Silero VAD via sherpa-onnx `VoiceActivityDetector`).
- Added `tmp_proto/tts_kws_vad.py`: synthesizes `你好问问` with the MeloTTS VITS model, resamples 44.1 kHz → 16 kHz, runs VAD, then KWS.
- Verified: TTS generation 0.72 s audio (RTF ~0.3–0.49), VAD detected one 11,328-sample speech segment, **KWS hit `你好问问`** from the synthesized audio.
- SHA-256 recorded for `tts.tar.bz2` (`e58351ed…1514`) and `model.onnx` (`bf30582e…ad86d`).
- Console mojibake (`你好问问` shown as `����`) is a Windows codepage display issue only; UTF-8 bytes are correct.
- Audio capture lifecycle wired into `VoicePlugin`: `start/resume` start the capture, `pause/stop` release it, capture start failure rolls back `running` state; `diagnose()` now reports `capture_attached`.
- Python suite: **21 passed**.

## Session 2026-07-26 (real microphone wake — VERIFIED)

- Added `tmp_proto/live_wake.py`: `SounddeviceWakeCapture` + `SherpaKeywordProvider` over the Realtek mic array (device 1), 16 kHz, threshold 0.25, audio in memory only.
- Live run: spoken `你好问问` produced a wake hit at **7.193 s** with score 1.0; capture stopped and resources released; nothing saved to disk.

## Session 2026-07-28 (t10 consolidated voice-stack validation + suite hygiene)

Environment: this workspace's isolated `.venv`, Windows, Python 3.12.10, sherpa-onnx 1.13.4, numpy 2.5.1, sounddevice 0.5.5, soundfile 0.14.0.

Suite hygiene:

- Installed `pytest 9.1.1` into `.venv` (previously missing, which blocked the documented `python -m pytest tests -q`).
- Gated the two `VoxCordAdapter` tests in `tests/test_provider_adapter.py` with `pytest.mark.skipif` when the adjacent VoxCord checkout is absent (it is not present on this host). They now **skip** instead of failing.
- `python -m pytest tests -q` → **19 passed, 2 skipped** (VoxCord optional).
- `python scripts/smoke_voice.py` → OK. `python scripts/e2e_simulated.py` → OK (17 events, 2 bridge sends, 1 cancel; mock transport).

t10 harness (`tmp_proto/t10_voice_stack_validation.py`) — release-path core sherpa-onnx, all checks passed:

| Check | Result | Evidence level |
|---|---|---|
| Windows startup + model load | KWS 0.627 s, VAD 0.053 s, both available | real local model load |
| Local Chinese wake on synth `你好问问` | hit; TTS gen 0.235 s; decode 0.026 s (RTF ~0.002) | synthesized audio, not a live mic recording |
| Resource release (8× open/close cycles) | python peak growth 0.01 MB, no leak | automated |
| Continuous run (12 s / 120 chunks silence) | 0 spurious hits, RTF 0.0186 | automated |
| Swappable conversation backend | two distinct transports drive identical turn path | automated (mock transports) |
| Interruptible TTS / barge-in | cancel during `speaking` → `turn.cancelled`, transport told to cancel pending turn, clean return to idle | automated (mock transport) |

Verification level: automated + synthesized audio. This is **not** a live-microphone or live-EvoX acceptance; those remain in the "Not yet verified" list below. The swappable-backend and barge-in checks use mock transports, confirming the plugin/turn orchestration is backend-agnostic and interruptible at the contract level.

## Session 2026-07-28 (t11 orb render-route spike)

Isolated UI prototype `tmp_proto/orb_spike.html` served over `python -m http.server 8791` and driven headlessly (Chromium via CDP, DPR 1). It carries all three candidate render routes behind a `window.__SPIKE__` API with `measure(route, ms)`, `setAmplitude`, and `glSupported()` hooks.

| Route | FPS (2 s window) | Render | Verdict |
|---|---:|---|---|
| CSS-only (degradation baseline) | 239.8 | conic-gradient core + blur, no raster canvas | **direct adopt** as degraded/static tier |
| Canvas 2D + rings (v1 main path) | 239.9 | radial glow + 3 amplitude-driven wave rings, clean, deterministic | **direct adopt** as v1 primary |
| WebGL shader (v2 option) | 240.0 | fragment-shader glow + concentric ring interference, richer | **v2 upgrade path**, GPU/driver dependent |

Additional evidence captured this run:

- WebGL: `glSupported() === true`, no compile/link error on this host; screenshot shows shader ring interference distinct from the Canvas route.
- Amplitude clamp verified: input `[0, 0.5, 1, 0.5, 0]` → clamped `[0.12, 0.5, 1, 0.5, 0.12]` (floor 0.12, ceil 1.0).
- Degradation signals all queryable: `matchMedia('(prefers-reduced-motion: reduce)')`, `navigator.hardwareConcurrency` (32), Canvas 2D context, WebGL context, `devicePixelRatio` — this is the full input set the quality-tier selector (t31) needs.
- Zero console errors across all three routes; screenshots captured for CSS, Canvas 2D, and WebGL.

Verification level: automated + headless Chromium visual capture. **Not** verified here: transparent-window compositing inside the real WebView2 runtime, true Windows DPI scaling at 125%/150%/175%, GPU load under sustained animation, and remote-desktop (RDP) software-render fallback. Those require the actual Tauri window and remain Phase 6 / t53 acceptance items. The spike confirms the plan's chosen architecture (Canvas 2D primary + CSS degradation + WebGL as v2) is technically sound at the render-route level before committing the production renderer files (t28–t35).

## Session 2026-08-02 (P1 speaker gate — measured on real speech)

Model: `models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`, 39,593,761 bytes, SHA-256 `1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b`, embedding **dim 512**. Loaded through the already-installed sherpa-onnx 1.13.4 — **zero new Python dependency**, confirmed by return value (`load()` → `available=True`), not by absence of an exception.

### Cosine separation on the 7 bundled KWS wavs — AUTO

The recordings in `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/test_wavs/` are real human speech (3.55–8.03 s each). No speaker labels ship with them; the grouping below was **derived from** the score matrix, then used to state the separation:

| Pair | Score | Pair | Score |
|---|---:|---|---:|
| 0–1 | 0.777 | 0–3 | −0.074 |
| 0–2 | 0.813 | 0–4 | 0.027 |
| 1–2 | **0.866** | 1–5 | −0.105 |
| 3–4 | **0.736** | 2–6 | −0.039 |
| 5–6 | 0.755 | 3–5 | 0.270 |
| | | 4–6 | 0.346 |
| | | 4–5 | **0.370** |

Three clusters: {0,1,2}, {3,4}, {5,6}. Within-cluster minimum **0.736**; cross-cluster maximum **0.370**. The shipped default `threshold = 0.5` sits inside that gap with ≈0.24 margin below and ≈0.13 above. Enrolling wav 0 then admits 1 and 2 and refuses 3, 4, 5, 6 — asserted in `tests/integration/test_speaker_model.py`.

**Evidence level is AUTO, not REAL-MIC.** The audio is real speech but recorded and pre-existing. What this establishes: the model discriminates, and 0.5 is a defensible default. What it does **not** establish: own-voice pass rate on this host's microphone, another person's rejection rate, or replay behaviour. All three still need P10 (release blocker #8).

### Negative result: synthetic audio cannot test discrimination — AUTO

Enrolled a 120 Hz harmonic stack (7 partials + noise), verified a 240 Hz one — an octave apart, about as different as two synthetic signals get. Result: **accepted, score 0.767**. The model is trained on speech and reads both as the same non-voice. Any discrimination test built on generated tones would pass vacuously; `test_synthetic_tones_cannot_stand_in_for_speech` pins this so the trap stays documented rather than rediscovered.

### KWS exposes no confidence — finding, corrects FR-1.8

`sherpa_onnx.lib._sherpa_onnx.KeywordResult` carries only `keyword`, `timestamps`, `tokens`, and `KeywordSpotter.get_result()` returns a bare `str`. There is **no per-hit confidence in this binding at all**. The previous code reported `1.0`, which read like a measurement and was not one — including in the 2026-07-26 REAL-MIC entry above, where "score 1.0" was that constant, not a detector output. `feed()` now returns `(keyword, None)`, and the number that reaches `wake.detected` is the speaker cosine similarity, which *is* measured. See ADR 002.

### Implemented and verified this session

- `core/audio/ring.py` — 3 s in-memory ring buffer (~192 KB @16 kHz float32). Red line 1's literal enforcement point: an AST test asserts the module imports nothing beyond `numpy`/`typing` and references no filesystem, socket, or subprocess name.
- `core/audio/capture.py` — gate wired at the KWS hit (ADR 002 option A). Preconditions checked **before** the device opens; every fail-closed branch raises.
- `core/audio/speaker.py` — `load_speaker_config()` + `from_config()` over `config/speaker.toml` via stdlib `tomllib`. Paths deliberately stay on environment variables so a checked-in config can never point at somebody's enrollment data.
- `evox_plugin/plugin.py` — `wake_rejected()` (no state transition, no reply) and `_diagnose_speaker()` (names and counts only, never a vector; explicit `warnings` when the gate is off).
- `scripts/enroll_speaker.py` — interactive Chinese-prompted enrollment, append-only, audio never written anywhere.
- Suite: **67 passed, 2 skipped** (was 43 at the end of P0; 19 privacy/fail-closed tests plus 5 model-gated integration tests added).

Fail-closed paths asserted individually: model missing → `start()` raises; nobody enrolled → raises; no verifier attached but verification required → raises; verifier throws mid-decision → rejection, not a pass; score below threshold → silent rejection with no state change. A full gate cycle inside a `tmp_path` CWD leaves the directory **empty**.

## Session 2026-08-02 (P2 platform event contract — AUTO)

Two contracts, one envelope. Measured on the committed files:

| Fact | Measured value |
|---|---|
| `contracts/voice-events.schema.json` | 575 bytes, SHA-256 `4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5`, 9 types, version `"1"` |
| `contracts/agent-events.schema.json` | 880 bytes, 12 types (`task.*` 4 / `agent.*` 2 / `tool.*` 4 / `memory.*` 2) |
| `contracts/agents.schema.json` | 1,294 bytes, registry shape for `config/agents.toml` |
| `core/events.py` | 138 lines (was 93): `+ AGENT_SCHEMA_PATH`, `CONTRACT_PATHS`, `contract_for()`, `validate_any_event()` |
| `core/agents/schema.py` | 146 lines, hand-rolled subset validator |
| `tests/test_agent_event_schema.py` | 274 lines, **34 passed** |
| Suite | **101 passed, 2 skipped** in 13.04 s, 103 collected (was 67 at the end of P1) |
| New runtime dependency | **none** — `import jsonschema` in the `.venv` raises `ImportError`, checked, so both validators stay hand-rolled |

Three properties are now enforced rather than intended:

- **NFR-5.8 is a digest, not an intention.** `test_voice_contract_is_byte_identical` fails on any in-place edit of the voice contract and its assertion message says where platform events belong instead.
- **The two envelopes are interchangeable.** Same `required` set, same property set, same `version` const, both `additionalProperties: false` — and the two `type` enums are **disjoint**, which is what keeps `contract_for()` from being ambiguous. Both are asserted; a type declared by two contracts raises rather than silently first-matching.
- **The registry schema cannot over-declare.** `test_the_schema_stays_inside_the_validator_subset` walks every key in `agents.schema.json` and fails on any keyword the validator does not implement, because a declared-but-inert constraint reads as protection. `kind`'s enum is asserted equal to `AGENT_KINDS` for the same reason: a config that validates and then fails at adapter construction is the worst of the two failure modes.

Not established by this session: any platform event has an actual producer. All 12 types are declared and validated; `memory.*` gets one in P3, `tool.*` in P4, `task.*`/`agent.*` in P6. `config/agents.toml` does not exist yet either — only its shape does.

## Not yet verified

- Speaker gate on this host's microphone: own-voice pass rate, another person's rejection, recorded-replay behaviour (the gate does **not** claim replay resistance — ADR 002 「局限」).
- Spoken real-microphone Silero endpointing acceptance (device opening and quiet-input rejection are verified).
- Live EvoX conversation bridge.
- Real streaming first-token latency.
- Separate always-on-top, skip-taskbar wake window (defined and compiling, not visually accepted).

## Blockers

1. Build a separate-window visual spike before copying audio components.
2. Define and verify the live EvoX session bridge.

License note (2026-07-26): the user confirmed VoxCord is their own independent project, so the distribution-license blocker is resolved and selective reuse is permitted.
