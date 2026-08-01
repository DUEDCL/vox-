"""Memory contracts.

Storage is SQLite + FTS5: one file, no external service, in-process, full-text
search out of the box. No vector database -- that would mean either a cloud
embedding call or another 200 MB+ local model, and neither pays for itself here.

Design red line 1 is enforced by the type system as far as it can be: a
``MemoryRecord`` holds ``text`` and nothing else that could carry a waveform.
Audio never enters memory. Recognised text passes a secret-shaped-content filter
before it is written, so a spoken token or key does not get persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: ``short`` is the live session's turns, ``mid`` is cross-session facts and
#: preferences (also mirrored to memory/facts/*.md for human readability),
#: ``long`` is tool audit history and dispatch statistics.
MEMORY_SCOPES = frozenset({"short", "mid", "long"})

#: ``turn`` a conversation turn, ``fact`` a durable statement about the user or
#: project, ``audit`` a record of a tool execution.
MEMORY_KINDS = frozenset({"turn", "fact", "audit"})


@dataclass(frozen=True)
class MemoryRecord:
    """One stored item. Text only, by construction."""

    id: str
    scope: str
    kind: str
    text: str
    session_id: str | None = None
    created_at: str = ""
    tags: tuple[str, ...] = ()


@runtime_checkable
class MemoryStore(Protocol):
    """Persistence and retrieval over a single SQLite file."""

    def write(self, record: MemoryRecord) -> str:
        """Persist one record and return its id. Must de-duplicate near-identical
        facts within a scope rather than accumulating restatements."""

    def recall(
        self, query: str, *, scope: str | None = None, limit: int = 8
    ) -> tuple[MemoryRecord, ...]:
        """Full-text search, most relevant first. Returns an empty tuple rather
        than raising when nothing matches."""

    def forget(self, record_id: str) -> bool:
        """Delete one record. Returns whether it existed."""

    def close(self) -> None:
        """Release the SQLite handle. Must be idempotent."""
