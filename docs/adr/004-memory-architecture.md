# ADR 004: Memory Architecture

## Status

Accepted 2026-08-02. Implemented 2026-08-02: `store.py` / `write.py` / `recall.py` shipped in P3 (AUTO).

## Decision

**SQLite with FTS5**, one file, no external service, no vector database.

### Module split

Four responsibilities, kept in separate modules (the split is taken from MemoryOS's Storage / Updating / Retrieval / Generation):

| Module | Owns |
|---|---|
| `store.py` | SQLite schema, FTS5 index, transactions |
| `write.py` | insertion, update, de-duplication |
| `recall.py` | query, ranking, relevance |
| — | injection into a task is the **dispatcher's** job, not memory's |

Keeping injection out of `core/memory/` matters: memory answers "what do I know", the dispatcher decides "what does this task need". Merging the two would make memory depend on task shape and stop it being independently testable.

### Three layers

| Layer | Content | Lifetime |
|---|---|---|
| short | turns in the current session | session |
| mid | facts and preferences across sessions | durable, **also written to `memory/facts/*.md`** |
| long | tool-execution audit and dispatch statistics | durable, feeds the router's success-rate dimension |

The mid layer's Markdown files are the human-readable source of truth; SQLite is the index over them. A memory system whose contents can only be read through its own query API is a memory system you cannot audit or correct by hand.

### Red line 1 enforcement

- **Only text is stored. Audio never enters memory** — not raw, not embedded, not summarised from a waveform.
- `asr.final` text passes a secret-shaped-content filter before insertion; anything matching key/token patterns is not written.

Both carry assertion tests.

## Rationale

SQLite + FTS5 satisfies every constraint that actually applies here: single file, zero external services, in-process, full-text search available with no extra dependency, and trivially backed up by copying one file. It is the `engram` route, and it fits red line 1 without qualification.

**No vector retrieval.** Embeddings mean either a cloud embedding API — an outright red line 1 violation — or another 200 MB+ of local model on top of the 413 MB already present, for a corpus that is one person's facts and preferences. FTS5 keyword search over a corpus that size is adequate; the cost is not.

**No 12-layer memory architecture and no knowledge graph.** Reviewed via `blessonism/openclaw-memory-architecture`; the machinery is disproportionate to a single-user local assistant. What is adopted from it is the one idea that transfers: *memory is files, and the agent rebuilds itself from those files on each start*. That is exactly what the Markdown mid layer does.

MemoryOS supplies the module boundaries (EMNLP 2025 Oral, arXiv 2506.06326). `liuhao6741/openclaw-memory` supplies the Markdown-as-truth precedent. **Verification level: 社区来源** for the repository claims — GitHub content is unreadable in this environment (see ADR 003's note); the MemoryOS paper reference is the one 官方 item.

The long layer exists for a specific consumer, not for completeness: the router's success-rate dimension (ADR 005) needs historical per-agent outcomes, and tool audit needs a durable trail for after-the-fact tracing of anything `shell.run` did. Both are memory writes, so they live here rather than in a second log system.

### Amendment 2026-08-02: FTS5 cannot search Chinese on its own

Measured before the implementation was written, in this `.venv` (SQLite 3.49.1, `ENABLE_FTS5`): a row containing 「用户喜欢用中文交流 and english too」 is found by `MATCH 'english'` and **not** by `MATCH '中文'`. `unicode61`, the default tokenizer, treats an unbroken CJK run as a single token, so only the whole run matches. ICU is not compiled in, and adding a tokenizer extension would be a new native dependency.

Resolution: the FTS table indexes a **derived token column**, not the text. `index_tokens()` emits each CJK character plus every overlapping bigram; `query_tokens()` emits bigrams only, dropping single CJK characters so that 「偏好」 cannot match a record that merely contains 「好」. Recall runs in two stages — strict (AND over all query tokens), and a bm25-ranked loose stage (OR) only when the strict stage finds nothing.

The cost is index size (roughly 2× the character count in tokens); the alternative was an index that cannot find the language the user actually speaks.

De-duplication applies to the mid layer only (`DEDUP_SCOPES = {"mid"}`), and the rule lives in the schema as a partial unique index on `(scope, kind, fingerprint) WHERE scope = 'mid'` rather than in the writer. Turns and audit rows are time series: two identical utterances are two events, and collapsing them would destroy the history the long layer exists to keep.

The credential filter **refuses the whole record** instead of redacting a match. A multi-line private-key block is the deciding case: a pattern matches the header, and redaction would store the body.

## Verified (as of 2026-08-02)

- `core/memory/contract.py` defines `MEMORY_SCOPES` / `MEMORY_KINDS` / `MemoryRecord` / `MemoryStore` and imports with no side effects (AUTO).
- `SqliteMemoryStore` conforms to the `MemoryStore` protocol; constructing one opens no file (AUTO).
- Every column of `records` is `TEXT` or `INTEGER` — **no BLOB exists**, so audio has no column to land in; `MemoryRecord` declares no `bytes` field; `store.write()` and `writer.write_turn()` raise `TypeError` on bytes (AUTO, red line 1).
- Chinese recall works: 「中文」 finds 「用户喜欢用中文交流」, and 「完全无关的查询」 finds nothing (AUTO).
- FTS operator text arriving from a microphone (`NOT 中文`, `中文 OR *`, `"unbalanced`, `中文 AND (`, empty) never raises (AUTO).
- 9 credential-shaped samples are refused whole with `store.count() == 0`, emit no event, and leave no copy of the text in `last_refusal` or `describe()`; 5 ordinary sentences including 「我的密码忘了怎么办」 and 「token 是什么意思」 are **not** refused (AUTO, FR-12.6).
- `memory.written` / `memory.recalled` validate against `contracts/agent-events.schema.json`, are absent from the voice contract, and carry ids/counts/tags but no text (AUTO).
- The Markdown mirror round-trips: a fact writes a file, a hand edit to that file shows up in the next recall, a plain file dropped into `memory/facts/` is indexed with its id written back, a credential in a hand-edited file is refused, and a deleted file is removed from the index only under `prune=True` (AUTO).
- `success_rate()` reports `rate: None` with zero observations rather than 0.0, so an untried agent does not lose every route to one that failed once (AUTO).
- The store is one file plus SQLite's own journals; a future `schema_version` is refused rather than assumed compatible (AUTO).
- Wired into the voice path: `submit_text` writes the user turn, `complete_turn` writes the assistant turn, both tagged `role:*`; memory is opt-in (no attach, no database file); a writer that raises cannot break a turn; `diagnose()["memory"]` reports counts and paths with no remembered text (AUTO).
- `/memory/` is gitignored **with a leading slash**: an unanchored `memory/` also matches `core/memory/`, which would have hidden the entire implementation from git. A test parses `.gitignore` and asserts both directions (AUTO).
- `pytest tests/test_memory.py -q` → 62 passed. Full suite 169 passed, 2 skipped (AUTO).

## Required before release (blockers)

- REAL: facts survive a process restart, and a fact corrected by hand in `memory/facts/*.md` is reflected in the next recall **in a live session** (the round trip is covered at AUTO; the restart is not).
- REAL: recall quality on a real corpus of the user's own facts — the bigram index is verified to find things, not verified to rank them usefully.
- AUTO: schema migration path exercised once a second `SCHEMA_VERSION` exists; today only the refusal of a future version is tested.
- AUTO: `prune_turns` is called by something. `short_keep = 200` is configured and implemented, but no caller trims the short layer yet, so the session window grows without bound in a long-running process.
