"""Retrieval and relevance over the memory store.

Two-stage on purpose. The strict stage requires every query token, which is what
makes 「偏好」 not match a record that merely contains 「好」; the loose stage runs
only when strict found nothing and accepts any token, ranked by bm25. A recall
that returns junk is worse than one that returns nothing, but one that returns
nothing when something related exists is not much better -- the order of the two
stages is the whole compromise.

Injection into a task is *not* here. Memory answers "what do I know"; the
dispatcher decides "what does this task need" (ADR 004).
"""

from __future__ import annotations

from typing import Any, Callable

from core.events import AGENT_SCHEMA_PATH, build_event, validate_event

from .contract import MemoryRecord
from .store import SqliteMemoryStore, query_tokens


def match_expression(query: str, *, require_all: bool) -> str:
    """Build an FTS5 MATCH expression from a natural-language query.

    Tokens are double-quoted so nothing in a user's utterance can be read as
    FTS5 query syntax -- ``NOT``, ``*`` and ``(`` all arrive as literal text
    from a microphone.
    """
    tokens = query_tokens(query)
    if not tokens:
        return ""
    joiner = " AND " if require_all else " OR "
    return joiner.join(f'"{token}"' for token in dict.fromkeys(tokens))


def recall_records(
    store: SqliteMemoryStore,
    query: str,
    *,
    scope: str | None = None,
    kind: str | None = None,
    limit: int = 8,
) -> tuple[MemoryRecord, ...]:
    """Strict pass, then loose pass. Empty tuple when neither finds anything."""
    strict = store.search(
        match_expression(query, require_all=True), scope=scope, kind=kind, limit=limit
    )
    if strict:
        return strict
    return store.search(
        match_expression(query, require_all=False), scope=scope, kind=kind, limit=limit
    )


class MemoryRecaller:
    """Read side of the memory layer, with the ``memory.recalled`` producer.

    The event payload carries counts, never text. A recall query is whatever the
    user just said and a recalled fact is whatever was stored about them; putting
    either into an event would push both into every log and transport that
    forwards the platform stream.
    """

    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        default_limit: int = 8,
    ) -> None:
        self.store = store
        self.on_event = on_event
        self.default_limit = default_limit

    def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = validate_event(build_event(event_type, payload), AGENT_SCHEMA_PATH)
        if self.on_event is not None:
            self.on_event(event)
        return event

    def recall(
        self,
        query: str,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> tuple[MemoryRecord, ...]:
        limit = self.default_limit if limit is None else limit
        hits = recall_records(self.store, query, scope=scope, kind=kind, limit=limit)
        self._emit(
            "memory.recalled",
            {
                "scope": scope,
                "kind": kind,
                "terms": len(query_tokens(query)),
                "hits": len(hits),
                "limit": limit,
            },
        )
        return hits

    def facts(self, query: str, *, limit: int | None = None) -> tuple[MemoryRecord, ...]:
        """Mid-layer only -- the cross-session statements worth re-reading."""
        return self.recall(query, scope="mid", kind="fact", limit=limit)

    def recent_turns(
        self, *, session_id: str | None = None, limit: int = 12
    ) -> tuple[MemoryRecord, ...]:
        """The short layer in time order; no query, because recency *is* the query."""
        newest = self.store.list_records(
            scope="short", kind="turn", session_id=session_id, limit=limit
        )
        return tuple(reversed(newest))

    def success_rate(self, agent: str) -> dict[str, Any]:
        """Historical success rate for one agent, from the long-term audit layer.

        This is dimension four of ADR 005's five-dimension routing score. It is
        computed from tags rather than columns so the audit layer keeps holding
        text and tags only -- the same shape as every other record.
        """
        rows = self.store.list_records(scope="long", kind="audit", limit=10_000)
        ok = failed = 0
        for row in rows:
            tags = set(row.tags)
            if f"agent:{agent}" not in tags:
                continue
            if "outcome:ok" in tags:
                ok += 1
            elif "outcome:fail" in tags:
                failed += 1
        total = ok + failed
        return {
            "agent": agent,
            "ok": ok,
            "failed": failed,
            "total": total,
            # No observations means no opinion. Returning 0.0 would let an agent
            # nobody has tried yet lose every route to one that has failed once.
            "rate": None if total == 0 else ok / total,
        }
