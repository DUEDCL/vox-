# ADR 005: Task Dispatch Model

## Status

Accepted 2026-08-02. Contracts written (AUTO). The 12 platform event types this ADR relies on were declared in P2 (`contracts/agent-events.schema.json`), with the envelope-merge and enum-disjointness properties under test; their producers are still ahead — `core/tools/` is P4, `core/memory/` is P3, `core/dispatch/` is P6.

## Decision

One dispatcher decides, per utterance, whether the platform does the work itself or hands it to an agent.

### Intent classification uses rules, not a model

Keyword and regex matching routes "读一下 X 文件" / "搜一下 Y" / "运行 Z" straight to a local tool and executes it directly — millisecond latency instead of seconds, and no agent involved. Only unmatched utterances go to agent routing.

Rejected: a classifier model in front of every utterance. It adds latency to the fast path it is supposed to accelerate, adds a model to the dependency set, and cannot be tested deterministically. Rule hits are fully AUTO-testable; that is the deciding factor.

### Routing scores five dimensions

Capability match → cost → expected latency → historical success rate → current load. Descriptors declare the first three (`config/agents.toml`); the last two come from the long-term memory layer (ADR 004).

A **circuit breaker** takes an agent out of rotation after N consecutive failures and skips it for a cooldown period. Without it, a broken agent keeps winning routes on its stale statistics and every turn pays its timeout.

### Three dispatch modes; voice defaults to single or race

| Mode | Use | Aggregation |
|---|---|---|
| `single` | default — route picks the one best agent | pass-through |
| `race` | first-token latency matters | first usable increment wins, cancel the rest |
| `fanout` | cross-validation | best-of / merge / mark disagreements |

**`fanout` is never the voice default.** Aggregating across agents means waiting for the slowest one, so first-token latency degrades to the worst member of the set — in a spoken interface that is the difference between a conversation and a form submission. `fanout` is available only on an explicit request for multiple opinions.

### The state machine is not extended

`VoiceState`'s six states do not change, and no sub-state is added for dispatch or for speaker verification. `thinking` already means "waiting for a backend"; parallel dispatch happens *inside* `thinking`. Dispatch progress is expressed through independent `task.*` events on the new `contracts/agent-events.schema.json`, whose envelope shape matches the voice contract so the two streams merge at the transport boundary.

`contracts/voice-events.schema.json` stays **byte-identical** at version `"1"`.

The cost is that the state machine does not reflect dispatch detail. The benefit is that three existing contract tests keep passing untouched, and any consumer written against version `"1"` keeps working. For a state machine that four other components already depend on, that trade is worth taking.

### Every tool call passes one policy gate

`ToolPolicy.check(request)` returns `None` to allow or a refusing `ToolResult` to deny. Both origins — `voice` and `agent` — go through it, so an agent cannot reach a tool by a route the user's own voice could not.

| Tool | Gate |
|---|---|
| `fs.read` | sandbox-root allowlist (workspace only by default); refuse `.env` / `*.pem` / `id_rsa*` / `credentials.json` / `*secret*`; per-file size cap |
| `web.search` | domain blocklist; keep title / URL / snippet only, never inject full page text |
| `shell.run` | **disabled by default** + command allowlist + dangerous-pattern block + per-call UI confirmation + audit log |
| `memory.recall` / `memory.write` | ADR 004's filters |

`shell.run` is the largest attack surface in the whole plan: "say a sentence, execute a command on this machine". The speaker gate (ADR 002) removes the other-people's-voices branch of that surface, but misrecognition and replay remain, so four layers stack on top and none is optional:

1. `enabled = false` in `config/tools.toml` — the user must turn it on deliberately.
2. Non-allowlisted commands are **refused, not queried**. Prompting would train reflexive confirmation, which is worse than a flat refusal.
3. Allowlisted commands still require the pending command shown on the orb plus an explicit confirming action.
4. Hard block on `rm -rf`, `git push --force`, `reset --hard`, `format`, `del /s`; execution is audit-logged and does not inherit sensitive environment variables.

## Rationale

Dispatch is where the platform stops being a voice frontend for one backend. The two design pressures pull in opposite directions: routing quality wants more information and more candidates; voice wants an answer starting to be spoken now. Every decision above resolves that tension the same way — the fast, deterministic, single-agent path is the default, and the expensive paths are opt-in.

Routing dimensions are taken from `dabit3/agent-router`; the circuit breaker and confidence gating from `reaatech/agent-mesh`, which the former lacks. `phodal/routa` supplied the workspace-first coordination framing. `AgensFlow`'s online policy learning (arXiv 2605.27466) was reviewed and **rejected**: it needs training and a reward signal, which a single-machine project has no way to supply. Rules plus running statistics are sufficient. **Verification level for these repository claims: 社区来源** (see ADR 003's note on GitHub being unreadable here).

## Verified (as of 2026-08-02)

- `core/dispatch/contract.py` and `core/tools/contract.py` define their modes, kinds, score, plan, intent, request, and result types and import with no side effects (AUTO).
- `contracts/voice-events.schema.json` unmodified; the nine event types are still read from the contract at runtime by `core/events.py` rather than mirrored in Python (AUTO, `tests/test_events.py`).
- Full suite green: 43 passed, 2 skipped (AUTO).

## Required before release (blockers)

- AUTO, one test per line: sandbox escape refused; sensitive filename refused; `shell.run` off by default; non-allowlisted command refused; each dangerous pattern blocked.
- AUTO: five-dimension scoring, circuit-breaker open/close, all three modes, rule-based intent classification.
- SIM: two mock agents produce behaviourally identical turns through the dispatcher (red line 2 at the dispatch layer).
- REAL-WIN: `shell.run` confirmation flow accepted on the real orb, including the refusal path.
- REAL: misrecognition-triggers-tool-execution attack surface tested and documented.
- Concurrency cap on dispatch, given 413 MB of models plus 37 MB speaker plus concurrent agent subprocesses on one host.
