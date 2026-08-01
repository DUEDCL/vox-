# ADR 002: Speaker Verification as a Wake Gate

## Status

Accepted 2026-08-02 for Phase 4 implementation; not release-approved. Code-complete for the store and the fail-closed paths (AUTO); the gate itself and real-microphone acceptance are P1/P10 work.

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
| Embedding or scoring raises | Treated as **rejection**, not as a pass |
| Score below threshold | Rejection, with the score kept for diagnostics |

`verify()` never raises for an ordinary rejection: it returns `accepted=False` with a reason. A caller that only branches on `accepted` is fail-closed by construction.

`require_verification=False` restores the old "anyone can wake it" behaviour as an escape hatch, and `diagnose()` must then report it as a **warning**.

### 4. Rejection is silent

No orb, no sound, no visual feedback of any kind. Only an internal `wake.rejected` event, plus the score in logs and `diagnose()`.

Two reasons: a security control should not confirm its own existence or its decision boundary to an unauthorised party; and false triggers from a television or a passing conversation must not produce visual noise for the authorised user.

### Biometric-data handling

Enrollment vectors live in `enrollment/voiceprints.json`, which `.gitignore` excludes. `describe()` is the only sanctioned view of enrollment state and returns **names and counts only, never vectors** — enforced by test. Audio is never persisted at any point in the pipeline.

## Rationale

KWS answers "was this phrase said", never "who said it". A television, a colleague, or the next room can drive the whole pipeline. That is a usability annoyance today and a security defect the moment the platform can read files or run shell commands — which is exactly what Phase 4 adds. The gate has to land before the tool surface does, which is why it is P1 rather than sequenced with the UI work.

Speaker verification is also the first real producer for `wake.rejected`, which the contract has always defined but nothing ever emitted. Related: `feed()` now returns `(keyword, score)` instead of hard-coding confidence to `1.0`, closing a long-standing honesty gap in the capture layer.

## Verified (as of 2026-08-02)

- sherpa-onnx 1.13.4 exposes the complete speaker API on this host — API surface read directly from the installed package (AUTO).
- `SpeakerStore`: round-trip, atomic write with no surviving `*.tmp`, refusal of an unsupported `version`, refusal of corrupt JSON (AUTO).
- Fail-closed without the model present: missing model reports unavailable rather than raising at load; `verify()` rejects with no model; `verify()` rejects with nobody enrolled; `embed()` refuses audio under the minimum duration (AUTO).
- `describe()` leaks no vector values; `remove()` deletes an enrollment and is idempotent (AUTO).

Everything above runs **without** the 37 MB model, deliberately: the properties that matter most are the ones that must hold when the model is absent.

## Limitations

**No anti-spoofing.** A recording of the authorised speaker played back to the microphone will pass. This ADR does not add a replay-detection model — that is a separate model, a separate accuracy budget, and a separate acceptance burden. Mitigations in place instead: the threshold is tunable upward, `shell.run` requires explicit UI confirmation regardless of who is speaking, and every tool execution is audit-logged for after-the-fact tracing. P10 will test replay attack and record the honest result rather than claim coverage.

False rejection of the authorised speaker is the other risk. Mitigations: default threshold 0.5 is configurable, `enroll` appends samples so a weak enrollment can be strengthened without a full re-record, and `diagnose()` reports recent scores for tuning.

## Required before release (blockers)

- REAL-MIC: authorised speaker accepted across quiet / far-field / noisy conditions, measured false-rejection rate.
- REAL-MIC: a second person saying the same keyword produces **no orb and no output**, with `wake.rejected` and its score visible afterwards in `diagnose()`.
- REAL-MIC: replay attack executed and its outcome documented, pass or fail.
- Threshold tuned against measured score distributions, not left at the default.
- Assertion test proving no audio path writes to disk, running as part of the default suite.
