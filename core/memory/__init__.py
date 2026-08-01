"""Memory: short-term turns, mid-term facts, long-term audit.

Three modules, following the MemoryOS split (ADR 004): ``store.py`` is SQLite +
FTS5 and owns the schema, ``write.py`` owns updating, de-duplication and the
credential filter, ``recall.py`` owns retrieval and relevance. Injecting recalled
context into a task is the dispatcher's job, not memory's.

Importing this package opens nothing. ``open_memory`` is the one call that reads
configuration, and even it defers the database connection until the first query.
"""

from __future__ import annotations

from typing import Any, Callable

from .contract import MEMORY_KINDS, MEMORY_SCOPES, MemoryRecord, MemoryStore
from .recall import MemoryRecaller, match_expression, recall_records
from .store import (
    DEDUP_SCOPES,
    SCHEMA_VERSION,
    MemoryStoreError,
    SqliteMemoryStore,
    fingerprint,
    index_tokens,
    load_memory_config,
    query_tokens,
)
from .write import (
    SECRET_PATTERNS,
    MemoryWriter,
    fact_slug,
    looks_like_secret,
    parse_fact_file,
    render_fact_file,
)


def open_memory(
    config_path: str | None = None,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    session_id: str | None = None,
) -> tuple[SqliteMemoryStore, MemoryWriter, MemoryRecaller]:
    """Build the three collaborators from ``config/memory.toml``.

    Returned as a tuple rather than hidden behind a facade because the writer and
    the recaller have different callers -- the voice path writes turns, the
    dispatcher reads facts -- and one combined object would invite either side to
    do both.
    """
    config = load_memory_config(config_path)
    store = SqliteMemoryStore(config["db_path"])
    writer = MemoryWriter(
        store,
        facts_dir=config["facts_dir"],
        on_event=on_event,
        session_id=session_id,
        short_keep=int(config["short_keep"]),
    )
    recaller = MemoryRecaller(
        store, on_event=on_event, default_limit=int(config["recall_limit"])
    )
    return store, writer, recaller


__all__ = [
    "DEDUP_SCOPES",
    "MEMORY_KINDS",
    "MEMORY_SCOPES",
    "SCHEMA_VERSION",
    "SECRET_PATTERNS",
    "MemoryRecaller",
    "MemoryRecord",
    "MemoryStore",
    "MemoryStoreError",
    "MemoryWriter",
    "SqliteMemoryStore",
    "fact_slug",
    "fingerprint",
    "index_tokens",
    "load_memory_config",
    "looks_like_secret",
    "match_expression",
    "open_memory",
    "parse_fact_file",
    "query_tokens",
    "recall_records",
    "render_fact_file",
]
