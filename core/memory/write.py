"""Writing, de-duplication, the secret filter, and the human-readable mirror.

Three things happen on the way in, in this order:

1. **The secret filter.** Text that looks like a credential is refused outright,
   not redacted. Redaction can only remove what the pattern matched, and a
   multi-line PEM block is exactly the case where a pattern matches the header
   and stores the body. Refusing the whole record cannot half-succeed.
2. **De-duplication.** Enforced in the store by a partial unique index, and only
   for facts -- see ``store.DEDUP_SCOPES``.
3. **The mirror.** Mid-layer facts are also written to ``memory/facts/*.md``.
   Those files are the human-readable source of truth (ADR 004): they can be
   edited by hand, and ``sync_facts`` folds the edits back into the index.

``memory.written`` is emitted here. Its payload carries ids and counts only --
never the text, because an event travels into every log and transport that
forwards the platform stream.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Sequence

from core.events import AGENT_SCHEMA_PATH, build_event, validate_event

from .contract import MemoryRecord
from .store import SqliteMemoryStore, new_id, now_iso

#: Credential shapes, each with the name that goes into the refusal reason.
#: Deliberately narrow: a filter that fires on ordinary speech would be turned
#: off within a day, and a filter that is off protects nothing.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{12,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "assigned credential",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|auth)\b"
            r"\s*[=:]\s*\S{6,}"
        ),
    ),
    (
        "assigned credential (zh)",
        re.compile(r"(?:密码|密钥|口令|令牌|密语)\s*(?:是|为|=|:|：)\s*\S{6,}"),
    ),
)

_SLUG_RUN = re.compile(r"[A-Za-z0-9]+|[㐀-鿿぀-ヿ가-힯]")


def looks_like_secret(text: str) -> str | None:
    """The name of the first credential shape found, or ``None``."""
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            return name
    return None


def fact_slug(text: str, record_id: str) -> str:
    """A readable, collision-free filename stem for one fact."""
    parts = _SLUG_RUN.findall(text or "")
    stem = "".join(parts)[:24].casefold() or "fact"
    return f"{stem}-{record_id[:8]}"


def parse_fact_file(raw: str) -> tuple[dict[str, str], str]:
    """Split a fact file into its front matter and its body.

    A hand-written file with no front matter at all is valid: an empty mapping
    comes back and the caller assigns an id. Requiring boilerplate before a note
    counts would defeat the point of the files being human-editable.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw.strip()
    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :]).strip()
        key, _, value = line.partition(":")
        if _:
            meta[key.strip()] = value.strip()
    # Unterminated front matter: treat the whole file as body rather than
    # silently dropping it.
    return {}, raw.strip()


def render_fact_file(record: MemoryRecord) -> str:
    tags = ", ".join(record.tags)
    return (
        "---\n"
        f"id: {record.id}\n"
        f"created_at: {record.created_at}\n"
        f"tags: {tags}\n"
        "---\n\n"
        f"{record.text.strip()}\n"
    )


class MemoryWriter:
    """Write side of the memory layer: the three layers, the filter, the mirror."""

    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        facts_dir: str | Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        session_id: str | None = None,
        short_keep: int = 200,
    ) -> None:
        self.store = store
        self.facts_dir = Path(facts_dir) if facts_dir is not None else None
        self.on_event = on_event
        self.session_id = session_id
        self.short_keep = short_keep
        self.refusals = 0
        #: Reason for the most recent refusal. The offending text is *not* kept.
        self.last_refusal: dict[str, Any] | None = None

    def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = validate_event(build_event(event_type, payload), AGENT_SCHEMA_PATH)
        if self.on_event is not None:
            self.on_event(event)
        return event

    def _refuse(self, scope: str, kind: str, reason: str) -> None:
        self.refusals += 1
        self.last_refusal = {"scope": scope, "kind": kind, "reason": reason}

    def _write(
        self,
        *,
        scope: str,
        kind: str,
        text: str,
        session_id: str | None,
        tags: Sequence[str],
        record_id: str | None = None,
        source: str | None = None,
    ) -> str | None:
        """Filter, then store. ``None`` means the filter refused the record."""
        if not isinstance(text, str):
            raise TypeError(f"memory stores text only; got {type(text).__name__}")
        secret = looks_like_secret(text)
        if secret is not None:
            self._refuse(scope, kind, f"looks like a {secret}")
            return None
        record = MemoryRecord(
            id=record_id or new_id(),
            scope=scope,
            kind=kind,
            text=text,
            session_id=session_id if session_id is not None else self.session_id,
            created_at=now_iso(),
            tags=tuple(tags),
        )
        stored_id = self.store.write(record, source=source)
        self._emit(
            "memory.written",
            {
                "id": stored_id,
                "scope": scope,
                "kind": kind,
                "deduplicated": self.store.last_write_deduplicated,
                "tags": list(record.tags),
            },
        )
        return stored_id

    # -- the three layers ----------------------------------------------------

    def write_turn(
        self,
        text: str,
        *,
        role: str = "user",
        session_id: str | None = None,
        tags: Sequence[str] = (),
    ) -> str | None:
        """Short layer. This is the ``asr.final`` path, so the filter matters most here.

        A recognised utterance is the one piece of memory the user never chose to
        write down, which is why the credential filter sits in front of it
        (FR-12.6) rather than only in front of facts.

        The short layer self-trims after every accepted write: it is a window on
        the current conversation, not an archive, so a long session must not grow
        it without bound. Trimming lives here rather than behind a separate
        caller so the two cannot drift apart.
        """
        stored = self._write(
            scope="short",
            kind="turn",
            text=text,
            session_id=session_id,
            tags=(f"role:{role}", *tags),
        )
        if stored is not None:
            self.prune_turns(session_id=session_id)
        return stored

    def write_fact(
        self,
        text: str,
        *,
        tags: Sequence[str] = (),
        session_id: str | None = None,
    ) -> str | None:
        """Mid layer: a cross-session statement, mirrored to a Markdown file."""
        record_id = new_id()
        source = None
        if self.facts_dir is not None:
            source = f"{fact_slug(text, record_id)}.md"
        stored = self._write(
            scope="mid",
            kind="fact",
            text=text,
            session_id=session_id,
            tags=tags,
            record_id=record_id,
            source=source,
        )
        if stored is not None and self.facts_dir is not None:
            self.mirror_fact(stored)
        return stored

    def write_audit(
        self,
        text: str,
        *,
        tags: Sequence[str] = (),
        session_id: str | None = None,
    ) -> str | None:
        """Long layer: what was executed and how it went. Never de-duplicated."""
        return self._write(
            scope="long", kind="audit", text=text, session_id=session_id, tags=tags
        )

    def record_agent_outcome(
        self,
        agent: str,
        ok: bool,
        *,
        latency_ms: int | None = None,
        note: str = "",
    ) -> str | None:
        """One audit row in the shape ``MemoryRecaller.success_rate`` reads.

        Producer and consumer live next to each other on purpose: the routing
        statistic is carried by tags, and a tag renamed on one side only would
        silently reduce every agent's history to zero observations.
        """
        tags = [f"agent:{agent}", "outcome:ok" if ok else "outcome:fail"]
        if latency_ms is not None:
            tags.append(f"latency_ms:{int(latency_ms)}")
        text = note or f"agent {agent} {'completed' if ok else 'failed'} a turn"
        return self.write_audit(text, tags=tags)

    def prune_turns(self, *, session_id: str | None = None, keep: int | None = None) -> int:
        return self.store.prune(
            scope="short", keep=self.short_keep if keep is None else keep, session_id=session_id
        )

    # -- the human-readable mirror -------------------------------------------

    def mirror_fact(self, record_id: str) -> Path | None:
        """Write one fact out to ``memory/facts/<slug>.md``."""
        if self.facts_dir is None:
            return None
        record = self.store.get(record_id)
        if record is None:
            return None
        source = self.store.source_of(record_id) or f"{fact_slug(record.text, record_id)}.md"
        path = self.facts_dir / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_fact_file(record), encoding="utf-8")
        if self.store.source_of(record_id) != source:
            self.store.set_source(record_id, source)
        return path

    def sync_facts(self, *, prune: bool = False) -> dict[str, int]:
        """Fold the Markdown files back into the index.

        The files win. They are the layer a person can open and correct, so a
        hand edit has to survive into the next recall -- that is the property
        that makes them the source of truth rather than a report.

        ``prune`` additionally forgets indexed facts whose file is gone, which is
        how a deletion propagates. It is off by default because a facts directory
        that has not been created yet would otherwise wipe the index.
        """
        counts = {"scanned": 0, "created": 0, "updated": 0, "unchanged": 0, "refused": 0, "pruned": 0}
        if self.facts_dir is None or not self.facts_dir.is_dir():
            return counts

        seen: set[str] = set()
        for path in sorted(self.facts_dir.glob("*.md")):
            counts["scanned"] += 1
            meta, body = parse_fact_file(path.read_text(encoding="utf-8"))
            if not body:
                continue
            secret = looks_like_secret(body)
            if secret is not None:
                # A hand-edited file is not a trusted input either.
                self._refuse("mid", "fact", f"looks like a {secret}")
                counts["refused"] += 1
                continue
            record_id = meta.get("id") or ""
            existing = self.store.get(record_id) if record_id else None
            if existing is not None:
                seen.add(existing.id)
                if existing.text.strip() == body:
                    counts["unchanged"] += 1
                    continue
                self.store.update_text(existing.id, body)
                counts["updated"] += 1
                continue
            tags = tuple(
                tag.strip() for tag in meta.get("tags", "").replace(",", " ").split() if tag.strip()
            )
            stored = self._write(
                scope="mid",
                kind="fact",
                text=body,
                session_id=None,
                tags=tags,
                record_id=record_id or None,
                source=path.name,
            )
            if stored is None:
                counts["refused"] += 1
                continue
            seen.add(stored)
            counts["created"] += 1
            if not meta.get("id"):
                # Give the file the id it now has, so the next edit updates this
                # record instead of creating a second one.
                self.mirror_fact(stored)

        if prune:
            for record_id, source in self.store.sources(scope="mid", kind="fact").items():
                if record_id in seen or not source:
                    continue
                if not (self.facts_dir / source).is_file():
                    self.store.forget(record_id)
                    counts["pruned"] += 1
        return counts

    def describe(self) -> dict[str, Any]:
        """Counts and paths. No stored text, and no refused text either."""
        described = self.store.describe()
        described.update(
            {
                "facts_dir": str(self.facts_dir) if self.facts_dir else None,
                "refusals": self.refusals,
                "last_refusal": self.last_refusal,
                "session_id": self.session_id,
            }
        )
        return described
