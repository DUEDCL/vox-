# ADR 003: Agent Integration Protocol

## Status

Accepted 2026-08-02. Contracts written (AUTO); the registry contract and its validator landed in P2 (`contracts/agents.schema.json` + `core/agents/schema.py`, 16 rejection paths covered). Adapters are P5 (`cli`, `evox`) and P7 (`acp`, `http`); `config/agents.toml` itself lands with them.

## Decision

Keep the platform's own contract layer and treat every agent — local CLI, ACP process, remote HTTP service — as an implementation behind it.

### The contract

`ConversationTransport` stays untouched for backward compatibility (its three bridge tests do not change). Above it:

```python
class AgentAdapter(Protocol):
    def describe(self) -> AgentDescriptor: ...          # capability / cost / latency, for routing
    def stream(self, task: Task) -> Iterator[AgentChunk]: ...
    def cancel(self, turn_id: str) -> None: ...
```

`AgentChunk` has three kinds: `text` (incremental output), `tool_call` (the agent asking the platform to run one of its tools), `done` (elapsed time and token counts, fed back into the router's success statistics).

`AgentDescriptor`, `Task`, and `AgentChunk` are frozen dataclasses whose fields are restricted to `str` / `int` / `float` / `frozenset` / `tuple` / `Mapping`. **No agent SDK type may appear in them, or in any event payload.** That is red line 2's enforcement point, and it is enforced by construction rather than by review.

### Four transports, prioritised by coverage

| Path | Adapter | Covers | Phase |
|---|---|---|---|
| headless CLI subprocess | `cli.py` | `claude -p`, `codex exec`, `opencode run` — nearly every coding-agent CLI | **P5** |
| existing EvoX bridge | `evox.py` | wraps `LocalEvoXTransport` as an `AgentAdapter` | **P5** |
| ACP (JSON-RPC 2.0 over stdio) | `acp.py` | any ACP-compatible agent | P7 |
| OpenAI-compatible HTTP | `http.py` / `openclaw.py` | OpenClaw Gateway, remote agents, aggregators | P7 |

CLI goes first because it is the widest net with the least protocol risk: a subprocess with a prompt argument and stdout parsing works against agents that have committed to no protocol at all. ACP follows as the standard path — implement the handshake once and every conforming agent connects without further adapter code.

Registration is declarative, through a user-editable `config/agents.toml` (`capabilities` / `cost` / `latency_ms` / `timeout_s`), validated by `contracts/agents.schema.json`.

### OpenClaw is a backend, not the foundation

OpenClaw is architecturally the closest prior art: it separates interface layer from assistant runtime and owns sessions / memory / tools / sandboxing / routing / channel integrations — a near one-to-one match with this platform's own boundaries. It is adopted as **a reference and one HTTP backend on `:18789`**, not as the base.

Rejected: building this project as an OpenClaw channel plugin. That would put a long-running Node.js process on the required-dependency path, hand architectural ownership to an upstream project, and pull in its default cloud channel integrations (Discord / Telegram) — three separate collisions with red line 1 and red line 2. The mixed route keeps its ecosystem reachable while the contracts stay locally owned.

### Not inventing a protocol

`AgentAdapter` is deliberately thin — three methods and three chunk kinds. It is an internal seam, not a wire format. Everything that crosses a process boundary uses something that already exists (a CLI's own arguments, ACP's JSON-RPC, or OpenAI-compatible HTTP).

## Rationale

The prototype's `ConversationTransport` has two synchronous methods and `send` blocks returning a `dict`. That shape cannot carry streaming increments, parallel dispatch, or agent-initiated tool calls — all three of which the platform needs. The entire technical difficulty of platformisation is concentrated at this one seam, which is why it gets its own ADR.

Streaming is not optional here: in a voice interface, first-token latency is the perceived latency. A blocking `send` means TTS cannot start until the whole answer exists.

`tool_call` as a chunk kind is what makes the platform's tool surface reusable by agents instead of duplicated inside each of them — and it means every agent-triggered tool call passes the same `ToolPolicy` gate as a voice-triggered one (ADR 005 covers the gate).

Reference material for the CLI difference-flattening list: `RobertTLange/headless-cli` has already catalogued the axes (prompts / models / reasoning effort / working directory / output mode / sessions). ACP: Zed-led, Apache-2.0, JetBrains co-launched a public registry in 2026-01. `NousResearch/hermes-agent` issue #569 tracks its own ACP server mode, so the named target agents converge on this path independently.

**Verification level for the repository judgements above: 社区来源 (community sources).** `github.com`, `api.github.com`, and `raw.githubusercontent.com` are all blocked for WebFetch in this environment, so no first-hand README content could be read. Star counts, licences, and last-commit dates are unconfirmed. The two exceptions the user personally tested — `kk43994/kkclaw` and `wassgha/opendex` — carry firsthand user evidence instead.

## Verified (as of 2026-08-02)

- `core/agents/contract.py` imports without spawning a subprocess or opening a socket (AUTO).
- Type surface contains only primitives and immutable containers — no SDK types (by construction; a `get_type_hints`-based assertion test is P5 work).
- Existing bridge behaviour unchanged: full suite 43 passed, 2 skipped (AUTO).

## Verified (P5, as of 2026-08-05)

- Type surface now asserted, not merely intended (`tests/test_agent_contract.py`, added at the end of P5 when the claim was found to be documented but untested): resolved annotations for `AgentDescriptor` / `Task` / `AgentChunk` are walked to their leaves, admitting only `str` / `int` / `float` / `bool` / `frozenset` / `tuple` / `Mapping` / `None`. The walk is recursive rather than a name whitelist — a reverse assertion proves it rejects `frozenset[FakeSdkClient]`, which an outermost-container check would pass. `Any` is permitted in exactly one field, `AgentChunk.arguments`, whose shape the invoked tool defines and `core/tools/policy.py` validates. The contract module's own imports are pinned to `__future__` / `dataclasses` / `typing` via AST, not substring search — its docstring names `subprocess` while stating the rule (AUTO).
- Building an adapter spawns nothing: `open_agents()` over the shipped config produces adapters without a subprocess or socket (AUTO).
- `cli.py` streaming parse, timeout, cancel, missing command, and non-zero exit — all five arrive as a terminating `done` chunk carrying `error`, never as an exception. Exactly one `done` per stream, with an agent's self-reported `done` folded in rather than forwarded (SIM, mock subprocess).
- Abandoning a stream kills the process: the generator's `finally` reaps it, so a discarded `race` loser leaves nothing behind (SIM).
- Subprocess environment is scrubbed of credential-shaped variables by the same `scrubbed_env()` the shell tool uses; passing a key requires naming the variable in `env_passthrough` (AUTO).
- Windows batch shim (`claude.cmd`) runs through a command-line string, and an argument containing `"` or `%` is **refused rather than escaped** — the two parsers (C runtime vs `cmd.exe`) cannot both be satisfied by one escaping (AUTO).
- `evox.py` wraps rather than reimplements `LocalEvoXTransport`, so all five bridge checks (bearer token required, cleartext HTTP loopback-only, credentials-in-URL rejected, `turn_id` encoded) still run per turn and cannot be weakened from the adapter side (AUTO).
- Cancel timing modelled honestly: a cancel arriving mid-request cannot reach the server, because the bridge assigns the turn id and only reveals it when `send` returns. It is recorded and re-issued the moment the id exists; the turn ends `cancelled` and the server learns one round-trip later (AUTO).
- A `kind` with no adapter (`acp` / `http`) errors with its phase named when enabled, instead of quietly doing nothing (AUTO).
- An entry whose command is absent from PATH is kept, not dropped; `check()` reports availability (AUTO).
- 59 passed (`contract` 14 + `cli` 28 + `evox` 17); full suite 359 passed, 3 skipped (AUTO).

**Not verified.** Every `cli.py` test drives a mock subprocess, which is SIM. No real external agent has completed a turn — that stays open as the REAL-AGENT blocker below.

## Required before release (blockers)

- REAL-AGENT: at least `claude` and `opencode` each complete one real turn end to end through `cli.py`. **Still open** — P5's coverage is mock-subprocess SIM.
- ~~SIM: `cli.py` streaming parse, timeout, and cancel against a mock subprocess.~~ Closed in P5.
- ~~SIM: ACP handshake / streaming / cancel against a mock JSON-RPC peer.~~ Closed in P7 (`tests/test_agent_acp.py`, mock JSON-RPC peer) — real ACP/HTTP turns stay REAL-AGENT (P9).
- ~~`evox.py` wrapping proves behaviourally identical to the pre-wrapping `LocalEvoXTransport` path.~~ Closed in P5 (AUTO). The bridge against a *real* EvoX server remains ADR 001's REAL-EVOX blocker.
- ~~`config/agents.toml` schema validation rejects a malformed descriptor rather than starting with it.~~ Closed in P5.
- ~~Session-bridge security posture from ADR 001 preserved verbatim once `session_bridge.py` becomes an `evox.py` implementation detail.~~ Closed in P5 — wrapping is what preserves it.
