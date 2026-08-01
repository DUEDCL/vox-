# ADR 002: Speaker Verification as a Wake Gate

## Status

Accepted 2026-08-02 for Phase 4 implementation; not release-approved. Code-complete as of P1 — store, ring buffer, fail-closed paths and the gate itself are all wired and covered (AUTO). Real-microphone acceptance remains P10.

## Decision

Admit a wake only when the voice that spoke the keyword matches an enrolled speaker. Four sub-decisions:

### 1. Inside the existing sherpa-onnx boundary — no new dependency

Use `SpeakerEmbeddingExtractor` + `SpeakerEmbeddingManager` from the already-installed **sherpa-onnx 1.13.4**. Verified empirically in this `.venv`: the full API is present (`create_stream` / `is_ready` / `compute` / `dim`, and `add` / `verify` / `search` / `score` / `remove` / `num_speakers` / `all_speakers`). No `pip install`, and ADR 001's "one runtime dependency boundary" stands unamended.

The only external need is the model file (~37 MB), `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`, from the k2-fsa releases. It lives in `models/`, which is outside version control.

### 2. The gate fires at the KWS hit, not after the first sentence

On a keyword hit, `SounddeviceWakeCapture` verifies the audio already sitting in a **3-second in-memory ring buffer** (≈192 KB at 16 kHz float32). The rejected case therefore terminates before anything is visible or audible.

Rejected alternative: verify the first ASR sentence instead. By then an unauthorised speaker has already seen the orb and driven a turn; rejection would be after-the-fact cleanup, not admission control.

The ring buffer **never touches the filesystem and never leaves the process**. That is red line 1's enforcement point in code, and it carries a dedicated assertion test.

### 3. Fail-closed

`require_verification=True` is the default, and every failure path denies:

| Condition | Behaviour |
|---|---|
| Model file missing | `start()` raises `ProviderUnavailable` |
| Nobody enrolled | `start()` raises — a gate with no enrollment is not a gate |
| Verification required but no verifier supplied | `start()` raises |
| Embedding or scoring raises | Treated as **rejection**, not as a pass |
| Score below threshold | Rejection, with the score kept for diagnostics |

The three `start()` refusals happen **before the input device is opened**, so a refused start leaves no stream running behind it.

`verify()` never raises for an ordinary rejection: it returns `accepted=False` with a reason. A caller that only branches on `accepted` is fail-closed by construction.

`require_verification=False` restores the old "anyone can wake it" behaviour as an escape hatch, and `diagnose()` must then report it as a **warning**.

### 4. Rejection is silent

No orb, no sound, no visual feedback of any kind. Only an internal `wake.rejected` event, plus the score in logs and `diagnose()`.

Two reasons: a security control should not confirm its own existence or its decision boundary to an unauthorised party; and false triggers from a television or a passing conversation must not produce visual noise for the authorised user.

### Biometric-data handling

Enrollment vectors live in `enrollment/voiceprints.json`, which `.gitignore` excludes. `describe()` is the only sanctioned view of enrollment state and returns **names and counts only, never vectors** — enforced by test. Audio is never persisted at any point in the pipeline.

## Rationale

KWS answers "was this phrase said", never "who said it". A television, a colleague, or the next room can drive the whole pipeline. That is a usability annoyance today and a security defect the moment the platform can read files or run shell commands — which is exactly what Phase 4 adds. The gate has to land before the tool surface does, which is why it is P1 rather than sequenced with the UI work.

Speaker verification is also the first real producer for `wake.rejected`, which the contract has always defined but nothing ever emitted.

It also forced a correction in the capture layer. `feed()` used to report `score=1.0` on every hit. Checked against the installed binding: `KeywordResult` carries only `keyword` / `timestamps` / `tokens`, and `KeywordSpotter.get_result()` returns a bare `str` — **sherpa-onnx 1.13.4 does not expose per-hit confidence at all**. That `1.0` was a constant wearing the costume of a measurement, and it had already reached the 2026-07-26 REAL-MIC record. `feed()` now returns `(keyword, None)`; `None` is a checked statement, not an omission. The number that reaches `wake.detected` is the speaker cosine similarity, which *is* measured.

## Verified (as of 2026-08-02)

### Without the model present (AUTO, 30 cases)

- sherpa-onnx 1.13.4 exposes the complete speaker API on this host — API surface read directly from the installed package.
- `SpeakerStore`: round-trip, atomic write with no surviving `*.tmp`, refusal of an unsupported `version`, refusal of corrupt JSON.
- Fail-closed without the model present: missing model reports unavailable rather than raising at load; `verify()` rejects with no model; `verify()` rejects with nobody enrolled; `embed()` refuses audio under the minimum duration.
- Fail-closed at the gate: all three `start()` refusals fire and leave no open device; a verifier that raises is recorded as a rejection.
- Rejection is silent: the state machine does not move and no reply is sent — the only trace is one `wake.rejected`.
- `describe()` leaks no vector values; `remove()` deletes an enrollment and is idempotent.
- Ring buffer: window semantics, oversized chunks, and `clear()`. An **AST assertion** proves the module imports only `numpy`/`typing` and contains no `open` / `tofile` / `socket` / `Popen` identifier.
- A full gate cycle inside a `chdir`'d temp directory — 31 callbacks and one hit — writes **zero files**.

These deliberately run **without** the 37 MB model: the properties that matter most are the ones that must hold when the model is absent.

### With the model present (AUTO, 5 cases)

- Model self-check: embedding dim **512**, 39,593,761 bytes, SHA-256 `1a33…5e4b`. Cold load **0.234 s** (NFR-1.6 target < 2 s).
- Verification latency on the gate's real 1.5 s window: **41 ms median** over 12 runs (min 39.8 / max 42.7) against NFR-1.9's 300 ms target.
- Discrimination on the 7 real-speech wavs bundled with the KWS model: within-cluster minimum **0.736**, cross-cluster maximum **0.370**. The default threshold 0.5 sits in that gap with ≈0.24 of margin below and ≈0.13 above — that is the evidence behind the default, which was previously just a plausible number.
- Enroll one speaker, then 2/2 admitted and 4/4 refused with `reason` containing `below threshold`.
- **Reverse assertion**: two synthetic harmonic stacks (120 Hz vs 240 Hz) still score **0.767** against each other. Synthetic audio therefore **cannot** be used to test discrimination — the model reads both as the same kind of non-speech. The test asserts the impostor is *accepted* so that the trap stays documented instead of being rediscovered.

**This section is AUTO, not REAL-MIC.** The audio is real human speech, but it is pre-existing recordings shipped with the model — not this host's microphone. It justifies the threshold; it says nothing about the authorised speaker's pass rate, an impostor's rejection rate, or replay behaviour.

## Limitations

**No anti-spoofing.** A recording of the authorised speaker played back to the microphone will pass. This ADR does not add a replay-detection model — that is a separate model, a separate accuracy budget, and a separate acceptance burden. Mitigations in place instead: the threshold is tunable upward, `shell.run` requires explicit UI confirmation regardless of who is speaking, and every tool execution is audit-logged for after-the-fact tracing. P10 will test replay attack and record the honest result rather than claim coverage.

False rejection of the authorised speaker is the other risk. Mitigations: default threshold 0.5 now has measured backing (see above) and stays configurable, `enroll` appends samples so a weak enrollment can be strengthened without a full re-record, and `diagnose()` reports the most recent rejection score for tuning.

**Synthetic audio is useless for measuring this gate.** Any future test that reaches for generated tones to check discrimination will pass vacuously (measured: 0.767 between 120 Hz and 240 Hz). Discrimination claims need real speech, and pass/reject-rate claims need this host's microphone.

## Required before release (blockers)

- REAL-MIC: authorised speaker accepted across quiet / far-field / noisy conditions, measured false-rejection rate.
- REAL-MIC: a second person saying the same keyword produces **no orb and no output**, with `wake.rejected` and its score visible afterwards in `diagnose()`.
- REAL-MIC: replay attack executed and its outcome documented, pass or fail.
- Threshold re-tuned against **microphone-measured** score distributions. The 0.5 default now has AUTO backing from recorded speech, which is enough to ship P1 but not enough to close this blocker.
- ~~Assertion test proving no audio path writes to disk, running as part of the default suite.~~ **Done in P1** — `test_a_full_gate_cycle_writes_nothing_to_disk` plus the ring buffer's AST assertion, both in the default suite.
