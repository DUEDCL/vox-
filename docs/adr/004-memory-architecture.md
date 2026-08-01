# ADR 004: Memory Architecture

## Status

Accepted 2026-08-02. Contracts written (AUTO); `store.py` / `write.py` / `recall.py` are P3.

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

## Verified (as of 2026-08-02)

- `core/memory/contract.py` defines `MEMORY_SCOPES` / `MEMORY_KINDS` / `MemoryRecord` / `MemoryStore` and imports with no side effects (AUTO).
- Full suite green with the package present: 43 passed, 2 skipped (AUTO).

## Required before release (blockers)

- AUTO: write / recall / de-duplication / audit — four test groups.
- AUTO: assertion that no audio buffer can reach a memory write.
- AUTO: secret-pattern filter drops a key-shaped string before insertion.
- AUTO: SQLite remains a single file; schema migration path defined before the first schema change ships.
- REAL: facts survive a process restart, and a fact corrected by hand in `memory/facts/*.md` is reflected in the next recall.
