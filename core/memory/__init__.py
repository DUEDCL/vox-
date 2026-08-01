"""Memory: short-term turns, mid-term facts, long-term audit.

Only contracts exist at this stage. ``store.py`` (SQLite + FTS5), ``write.py``
(update and de-duplication), and ``recall.py`` (retrieval and relevance) follow
the module split from MemoryOS; injection into a task is the dispatcher's job,
not memory's.
"""

from __future__ import annotations

from .contract import MEMORY_KINDS, MEMORY_SCOPES, MemoryRecord, MemoryStore

__all__ = [
    "MEMORY_KINDS",
    "MEMORY_SCOPES",
    "MemoryRecord",
    "MemoryStore",
]
