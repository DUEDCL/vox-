"""SQLite + FTS5 storage for the three memory layers.

One file, no server, no vector index (ADR 004). The schema is deliberately flat:
one ``records`` table for all three scopes plus one FTS5 index over a *derived*
token column.

Why a derived column rather than indexing ``text`` directly: FTS5's ``unicode61``
tokenizer treats an unbroken run of Chinese characters as a single token, so
``MATCH '中文'`` finds nothing inside "用户喜欢用中文交流". Since Chinese is this
platform's primary language, an index that cannot find Chinese would be an index
in name only. ``index_tokens`` therefore splits CJK runs into characters plus
overlapping bigrams and lower-cases latin words; queries go through the same
function, so the two sides always agree.

Red line 1 is enforced by construction here: every column is TEXT or INTEGER and
``write`` refuses a non-``str`` ``text``, so there is no path by which audio can
enter the database.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from .contract import MEMORY_KINDS, MEMORY_SCOPES, MemoryRecord

SCHEMA_VERSION = 1

#: Scopes whose writes are de-duplicated. Facts are statements that should exist
#: once; turns and audit rows are time series, and collapsing two identical
#: utterances or two identical tool calls would destroy the record of when each
#: happened.
DEDUP_SCOPES = frozenset({"mid"})

_RUN = re.compile(r"[A-Za-z0-9_]+|[㐀-鿿぀-ヿ가-힯]+")
_LATIN = re.compile(r"[A-Za-z0-9_]+")
_SPACE = re.compile(r"\s+")
_TRAILING = "。.!！?？,，、;；:：\"'“”‘’ "


def index_tokens(text: str) -> tuple[str, ...]:
    """Tokens written into the FTS index: CJK characters *and* bigrams.

    Indexing both means a one-character query and a longer phrase query both
    have something to match; the extra index size is a few bytes per character.
    """
    out: list[str] = []
    for run in _RUN.findall(text or ""):
        if _LATIN.fullmatch(run):
            out.append(run.casefold())
            continue
        out.extend(run)
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tuple(out)


def query_tokens(text: str) -> tuple[str, ...]:
    """Tokens a query is decomposed into: bigrams only for multi-character CJK.

    Dropping single characters from the query side is what keeps precision: a
    query for 「偏好」 should not be satisfied by a record that merely contains
    「好」 somewhere.
    """
    out: list[str] = []
    for run in _RUN.findall(text or ""):
        if _LATIN.fullmatch(run):
            out.append(run.casefold())
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tuple(out)


def normalise(text: str) -> str:
    """Collapse the differences that should not make two facts distinct."""
    return _SPACE.sub(" ", (text or "").strip()).strip(_TRAILING).casefold()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_CONFIG_NAME = "memory.toml"


def load_memory_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``config/memory.toml``; a missing file yields the shipped defaults.

    Paths resolve against the repository root so the same relative default works
    from any working directory. Like the speaker config, the fallback is the safe
    one -- defaults that keep the database inside the workspace.
    """
    root = Path(__file__).resolve().parents[2]
    config_path = Path(
        path or os.getenv("EVOX_MEMORY_CONFIG", root / "config" / DEFAULT_CONFIG_NAME)
    )
    defaults: dict[str, Any] = {
        "db_path": "memory/memory.db",
        "facts_dir": "memory/facts",
        "recall_limit": 8,
        "short_keep": 200,
    }
    if config_path.is_file():
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise MemoryStoreError(f"memory config is unreadable: {exc}") from exc
        for key, value in (raw.get("memory") or {}).items():
            if key in defaults:
                defaults[key] = value
    for key, env in (("db_path", "EVOX_MEMORY_DB"), ("facts_dir", "EVOX_MEMORY_FACTS")):
        override = os.getenv(env)
        if override:
            defaults[key] = override
    for key in ("db_path", "facts_dir"):
        value = Path(str(defaults[key]))
        defaults[key] = str(value if value.is_absolute() else root / value)
    return defaults


def new_id() -> str:
    return uuid4().hex


def join_tags(tags: Sequence[str]) -> str:
    cleaned: list[str] = []
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        if _SPACE.search(tag):
            raise ValueError(f"tag must not contain whitespace: {tag!r}")
        cleaned.append(tag)
    return " ".join(cleaned)


def split_tags(raw: str | None) -> tuple[str, ...]:
    return tuple((raw or "").split())


class MemoryStoreError(RuntimeError):
    """A memory operation could not be completed."""


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS records (
        row_id      INTEGER PRIMARY KEY,
        id          TEXT    NOT NULL UNIQUE,
        scope       TEXT    NOT NULL,
        kind        TEXT    NOT NULL,
        text        TEXT    NOT NULL,
        session_id  TEXT,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        tags        TEXT    NOT NULL DEFAULT '',
        fingerprint TEXT    NOT NULL,
        source      TEXT,
        hits        INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS records_scope_idx ON records (scope, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS records_session_idx ON records (session_id, created_at)",
    # Partial unique index: the de-duplication rule is a storage guarantee, not
    # a convention the writer is trusted to remember.
    "CREATE UNIQUE INDEX IF NOT EXISTS records_dedup_idx "
    "ON records (scope, kind, fingerprint) WHERE scope = 'mid'",
    "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(tokens)",
)


class SqliteMemoryStore:
    """The concrete ``MemoryStore`` (see ``contract.py``) on one SQLite file.

    Opening is lazy in the same sense as the audio providers: constructing this
    object touches nothing, so importing the package cannot create a database.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        #: Whether the most recent ``write`` reused an existing record.
        self.last_write_deduplicated = False

    # -- connection ----------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.db_path != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
            self._migrate(conn)
        return self._conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Create or upgrade the schema.

        The version row exists before the first schema change, not after it:
        a single-file store with no recorded version is a store that can only be
        migrated by guessing.
        """
        with conn:
            for statement in SCHEMA:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] > SCHEMA_VERSION:
                raise MemoryStoreError(
                    f"memory database is at schema version {row['version']}, "
                    f"this build understands {SCHEMA_VERSION}"
                )

    @property
    def schema_version(self) -> int:
        row = self.connection.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else 0

    def close(self) -> None:
        """Idempotent, as the contract requires."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- writing -------------------------------------------------------------

    def write(self, record: MemoryRecord, *, source: str | None = None) -> str:
        """Store one record and return its id.

        For a de-duplicated scope an equivalent existing record is *reused*: its
        ``hits`` counter and ``updated_at`` advance, its tags absorb the new
        ones, and the id that comes back is the original's. The caller can tell
        the two cases apart with ``last_write_deduplicated``.
        """
        if record.scope not in MEMORY_SCOPES:
            raise ValueError(f"unknown memory scope: {record.scope!r}")
        if record.kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {record.kind!r}")
        if not isinstance(record.text, str):
            # Red line 1's enforcement point: bytes are the shape audio would
            # arrive in, and there is no column that would accept them.
            raise TypeError(
                f"memory stores text only; got {type(record.text).__name__}"
            )
        if not record.text.strip():
            raise ValueError("memory record text must not be empty")

        digest = fingerprint(record.text)
        conn = self.connection
        self.last_write_deduplicated = False
        if record.scope in DEDUP_SCOPES:
            existing = conn.execute(
                "SELECT row_id, id, tags FROM records "
                "WHERE scope = ? AND kind = ? AND fingerprint = ?",
                (record.scope, record.kind, digest),
            ).fetchone()
            if existing is not None:
                self.last_write_deduplicated = True
                merged = join_tags(
                    sorted(set(split_tags(existing["tags"])) | set(record.tags))
                )
                with conn:
                    conn.execute(
                        "UPDATE records SET hits = hits + 1, updated_at = ?, tags = ? "
                        "WHERE row_id = ?",
                        (now_iso(), merged, existing["row_id"]),
                    )
                return str(existing["id"])

        record_id = record.id or new_id()
        created = record.created_at or now_iso()
        with conn:
            cursor = conn.execute(
                "INSERT INTO records (id, scope, kind, text, session_id, created_at, "
                "updated_at, tags, fingerprint, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    record.scope,
                    record.kind,
                    record.text,
                    record.session_id,
                    created,
                    created,
                    join_tags(record.tags),
                    digest,
                    source,
                ),
            )
            conn.execute(
                "INSERT INTO records_fts (rowid, tokens) VALUES (?, ?)",
                (cursor.lastrowid, " ".join(index_tokens(record.text))),
            )
        return record_id

    def update_text(self, record_id: str, text: str) -> bool:
        """Replace one record's text and reindex it. Used by the fact sync."""
        if not isinstance(text, str):
            raise TypeError(f"memory stores text only; got {type(text).__name__}")
        conn = self.connection
        row = conn.execute(
            "SELECT row_id FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return False
        with conn:
            conn.execute(
                "UPDATE records SET text = ?, fingerprint = ?, updated_at = ? WHERE row_id = ?",
                (text, fingerprint(text), now_iso(), row["row_id"]),
            )
            conn.execute("DELETE FROM records_fts WHERE rowid = ?", (row["row_id"],))
            conn.execute(
                "INSERT INTO records_fts (rowid, tokens) VALUES (?, ?)",
                (row["row_id"], " ".join(index_tokens(text))),
            )
        return True

    def forget(self, record_id: str) -> bool:
        conn = self.connection
        row = conn.execute(
            "SELECT row_id FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return False
        with conn:
            conn.execute("DELETE FROM records_fts WHERE rowid = ?", (row["row_id"],))
            conn.execute("DELETE FROM records WHERE row_id = ?", (row["row_id"],))
        return True

    # -- reading -------------------------------------------------------------

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            scope=row["scope"],
            kind=row["kind"],
            text=row["text"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            tags=split_tags(row["tags"]),
        )

    def get(self, record_id: str) -> MemoryRecord | None:
        row = self.connection.execute(
            "SELECT * FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        return self._record(row) if row else None

    def source_of(self, record_id: str) -> str | None:
        """The human-readable file a record mirrors to, if it has one."""
        row = self.connection.execute(
            "SELECT source FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        return row["source"] if row else None

    def set_source(self, record_id: str, source: str) -> bool:
        conn = self.connection
        with conn:
            cursor = conn.execute(
                "UPDATE records SET source = ? WHERE id = ?", (source, record_id)
            )
        return cursor.rowcount > 0

    def sources(self, *, scope: str, kind: str) -> dict[str, str | None]:
        rows = self.connection.execute(
            "SELECT id, source FROM records WHERE scope = ? AND kind = ?", (scope, kind)
        ).fetchall()
        return {row["id"]: row["source"] for row in rows}

    def list_records(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        newest_first: bool = True,
    ) -> tuple[MemoryRecord, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("scope", scope), ("kind", kind), ("session_id", session_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if newest_first else "ASC"
        params.append(int(limit))
        rows = self.connection.execute(
            f"SELECT * FROM records {where} ORDER BY created_at {order}, row_id {order} LIMIT ?",
            params,
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def search(
        self,
        match_expression: str,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 8,
    ) -> tuple[MemoryRecord, ...]:
        """Raw FTS5 search, ranked by bm25 (lower is better), then recency.

        An unparseable match expression yields no results rather than an
        exception: a recall driven by whatever the user just said must not be
        able to crash the turn.
        """
        if not match_expression.strip():
            return ()
        clauses = ["records_fts MATCH ?"]
        params: list[Any] = [match_expression]
        for column, value in (("r.scope", scope), ("r.kind", kind)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.append(int(limit))
        sql = (
            "SELECT r.* FROM records_fts JOIN records r ON r.row_id = records_fts.rowid "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY bm25(records_fts), r.created_at DESC LIMIT ?"
        )
        try:
            rows = self.connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return ()
        return tuple(self._record(row) for row in rows)

    def recall(
        self, query: str, *, scope: str | None = None, limit: int = 8
    ) -> tuple[MemoryRecord, ...]:
        """The contract's retrieval entry point.

        Delegated to ``recall.py`` so relevance strategy lives in one place; the
        import is local because that module reads this one.
        """
        from .recall import recall_records

        return recall_records(self, query, scope=scope, limit=limit)

    def count(self, *, scope: str | None = None, kind: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("scope", scope), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.connection.execute(
            f"SELECT COUNT(*) AS n FROM records {where}", params
        ).fetchone()
        return int(row["n"])

    def prune(self, *, scope: str, keep: int, session_id: str | None = None) -> int:
        """Drop all but the newest ``keep`` records in a scope. Returns how many went.

        The short layer is a window on the current conversation, not an archive;
        without a bound it would grow for as long as the process runs.
        """
        clause = "scope = ?"
        params: list[Any] = [scope]
        if session_id is not None:
            clause += " AND session_id = ?"
            params.append(session_id)
        rows = self.connection.execute(
            f"SELECT id FROM records WHERE {clause} "
            "ORDER BY created_at DESC, row_id DESC LIMIT -1 OFFSET ?",
            (*params, int(keep)),
        ).fetchall()
        for row in rows:
            self.forget(row["id"])
        return len(rows)

    def describe(self) -> dict[str, Any]:
        """Counts only. Memory holds what the user said; this reports no text."""
        if self._conn is None and self.db_path != ":memory:" and not Path(self.db_path).exists():
            return {"path": self.db_path, "exists": False, "records": 0, "by_scope": {}}
        return {
            "path": self.db_path,
            "exists": True,
            "schema_version": self.schema_version,
            "records": self.count(),
            "by_scope": {scope: self.count(scope=scope) for scope in sorted(MEMORY_SCOPES)},
            "by_kind": {kind: self.count(kind=kind) for kind in sorted(MEMORY_KINDS)},
        }
