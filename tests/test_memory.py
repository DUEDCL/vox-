"""Memory: writing, recall, de-duplication, audit, and the two red-line filters.

All AUTO. Everything runs on a temporary SQLite file or an in-memory database, so
none of it needs a model, a microphone, or an agent.

The two assertions that carry a red line are ``test_write_refuses_bytes`` (audio
has no path into the database) and the credential-filter group (a recognised
utterance is the one memory the user never chose to write down).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import get_type_hints

import pytest

from core.events import AGENT_SCHEMA_PATH, VOICE_SCHEMA_PATH, allowed_types, validate_event
from core.memory import (
    SECRET_PATTERNS,
    MemoryRecaller,
    MemoryRecord,
    MemoryStore,
    MemoryWriter,
    SqliteMemoryStore,
    fingerprint,
    index_tokens,
    load_memory_config,
    looks_like_secret,
    match_expression,
    open_memory,
    parse_fact_file,
    query_tokens,
)


@pytest.fixture()
def memory(tmp_path):
    """Store, writer and recaller over one temporary database plus facts dir."""
    store = SqliteMemoryStore(tmp_path / "memory.db")
    events: list[dict] = []
    writer = MemoryWriter(
        store, facts_dir=tmp_path / "facts", on_event=events.append, session_id="s1"
    )
    recaller = MemoryRecaller(store, on_event=events.append)
    yield store, writer, recaller, events
    store.close()


# -- store and schema --------------------------------------------------------


def test_store_satisfies_the_contract_protocol(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    assert isinstance(store, MemoryStore)


def test_constructing_a_store_touches_nothing(tmp_path):
    """Lazy like the audio providers: import and construction open no file."""
    path = tmp_path / "m.db"
    SqliteMemoryStore(path)
    assert not path.exists()


def test_schema_version_is_recorded_before_the_first_migration(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    assert store.schema_version == 1
    store.close()


def test_close_is_idempotent(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    store.connection
    store.close()
    store.close()
    assert store.schema_version == 1  # reopens on demand
    store.close()


def test_no_column_can_hold_audio(tmp_path):
    """Red line 1 by construction: every column is TEXT or INTEGER, none is BLOB."""
    store = SqliteMemoryStore(tmp_path / "m.db")
    types = {
        row[1]: row[2]
        for row in store.connection.execute("PRAGMA table_info(records)").fetchall()
    }
    assert types
    assert set(types.values()) <= {"TEXT", "INTEGER"}
    store.close()


def test_record_type_declares_no_binary_field():
    hints = {name: str(hint) for name, hint in get_type_hints(MemoryRecord).items()}
    assert "text" in hints
    assert all("bytes" not in hint for hint in hints.values())


def test_unknown_scope_and_kind_are_rejected(memory):
    store, _writer, _recaller, _events = memory
    with pytest.raises(ValueError):
        store.write(MemoryRecord(id="a", scope="eternal", kind="fact", text="x"))
    with pytest.raises(ValueError):
        store.write(MemoryRecord(id="a", scope="mid", kind="poem", text="x"))


def test_empty_text_is_rejected(memory):
    store, _writer, _recaller, _events = memory
    with pytest.raises(ValueError):
        store.write(MemoryRecord(id="a", scope="mid", kind="fact", text="   "))


def test_write_refuses_bytes(memory):
    """Audio would arrive as bytes; there is no code path that accepts it."""
    store, writer, _recaller, _events = memory
    with pytest.raises(TypeError):
        store.write(MemoryRecord(id="a", scope="short", kind="turn", text=b"\x00\x01"))
    with pytest.raises(TypeError):
        writer.write_turn(b"\x00\x01")


def test_forget_removes_the_record_and_its_index(memory):
    store, writer, recaller, _events = memory
    record_id = writer.write_fact("用户在用 Windows 11")

    assert store.forget(record_id) is True
    assert store.get(record_id) is None
    assert recaller.facts("Windows") == ()
    assert store.forget(record_id) is False


# -- writing the three layers ------------------------------------------------


def test_the_three_layers_land_in_their_own_scopes(memory):
    store, writer, _recaller, _events = memory
    writer.write_turn("今天天气怎么样")
    writer.write_fact("用户偏好中文回答")
    writer.write_audit("fs.read allowed docs/readme.md")

    described = store.describe()
    assert described["by_scope"] == {"short": 1, "mid": 1, "long": 1}
    assert described["by_kind"] == {"turn": 1, "fact": 1, "audit": 1}


def test_a_turn_carries_its_role_and_session(memory):
    store, writer, _recaller, _events = memory
    record_id = writer.write_turn("你好", role="user")

    record = store.get(record_id)
    assert record is not None
    assert record.session_id == "s1"
    assert "role:user" in record.tags


def test_memory_written_is_a_platform_event_not_a_voice_one(memory):
    _store, writer, _recaller, events = memory
    writer.write_fact("用户偏好中文回答")

    written = [event for event in events if event["type"] == "memory.written"]
    assert len(written) == 1
    validate_event(written[0], AGENT_SCHEMA_PATH)
    assert "memory.written" in allowed_types(AGENT_SCHEMA_PATH)
    assert "memory.written" not in allowed_types(VOICE_SCHEMA_PATH)


def test_memory_events_never_carry_the_remembered_text(memory):
    """An event fans out to every log and transport; the text stays in the store."""
    _store, writer, recaller, events = memory
    secret_ish = "用户的猫叫毛毛"
    writer.write_fact(secret_ish)
    recaller.facts(secret_ish)

    assert events
    for event in events:
        assert secret_ish not in repr(event["payload"])
        assert "text" not in event["payload"]


def test_recall_emits_counts_only(memory):
    _store, writer, recaller, events = memory
    writer.write_fact("用户住在北京")
    events.clear()

    recaller.facts("北京")
    recalled = [event for event in events if event["type"] == "memory.recalled"]
    assert len(recalled) == 1
    assert recalled[0]["payload"]["hits"] == 1
    assert recalled[0]["payload"]["scope"] == "mid"


def test_prune_keeps_only_the_newest_turns(memory):
    store, writer, _recaller, _events = memory
    for index in range(6):
        writer.write_turn(f"第 {index} 句话")

    dropped = writer.prune_turns(keep=2)
    assert dropped == 4
    assert store.count(scope="short") == 2


def test_write_turn_self_trims_the_short_layer(tmp_path):
    """The short layer is a window, not an archive: it must not grow unbounded."""
    store = SqliteMemoryStore(tmp_path / "m.db")
    writer = MemoryWriter(store, short_keep=3)
    try:
        for index in range(5):
            writer.write_turn(f"第 {index} 句话")

        assert store.count(scope="short") == 3
        remaining = [record.text for record in store.list_records(scope="short", kind="turn")]
        assert remaining == ["第 4 句话", "第 3 句话", "第 2 句话"]
    finally:
        store.close()


# -- de-duplication ----------------------------------------------------------


def test_the_same_fact_written_twice_stays_one_record(memory):
    store, writer, _recaller, _events = memory
    first = writer.write_fact("用户偏好中文回答")
    second = writer.write_fact("用户偏好中文回答")

    assert first == second
    assert store.count(scope="mid") == 1


@pytest.mark.parametrize(
    "variant",
    [
        "用户偏好中文回答。",
        "  用户偏好中文回答  ",
        "用户偏好中文回答!",
    ],
)
def test_near_identical_facts_are_deduplicated(memory, variant):
    """Trailing punctuation and whitespace must not create a second copy."""
    store, writer, _recaller, _events = memory
    writer.write_fact("用户偏好中文回答")
    writer.write_fact(variant)

    assert store.count(scope="mid") == 1


def test_deduplication_is_case_insensitive_for_latin(memory):
    store, writer, _recaller, _events = memory
    writer.write_fact("User prefers Chinese")
    writer.write_fact("user prefers chinese")

    assert store.count(scope="mid") == 1


def test_a_deduplicated_write_absorbs_the_new_tags(memory):
    store, writer, _recaller, _events = memory
    record_id = writer.write_fact("用户偏好中文回答", tags=("preference",))
    writer.write_fact("用户偏好中文回答", tags=("language",))

    record = store.get(record_id)
    assert record is not None
    assert set(record.tags) == {"preference", "language"}
    assert store.last_write_deduplicated is True


def test_turns_and_audit_rows_are_not_deduplicated(memory):
    """Two identical utterances are two events; collapsing them loses the history."""
    store, writer, _recaller, _events = memory
    writer.write_turn("再说一遍")
    writer.write_turn("再说一遍")
    writer.write_audit("shell.run refused: rm -rf")
    writer.write_audit("shell.run refused: rm -rf")

    assert store.count(scope="short") == 2
    assert store.count(scope="long") == 2


def test_fingerprint_ignores_only_the_intended_differences():
    assert fingerprint("用户偏好中文") == fingerprint("  用户偏好中文。 ")
    assert fingerprint("A b") == fingerprint("a  B")
    assert fingerprint("用户偏好中文") != fingerprint("用户偏好英文")


# -- recall ------------------------------------------------------------------


def test_chinese_is_searchable_at_all(memory):
    """The reason the index has a derived token column: FTS5 alone cannot do this."""
    _store, writer, recaller, _events = memory
    writer.write_fact("用户喜欢用中文交流")

    assert [record.text for record in recaller.facts("中文")] == ["用户喜欢用中文交流"]


def test_recall_is_precise_enough_to_return_nothing(memory):
    _store, writer, recaller, _events = memory
    writer.write_fact("用户住在北京")

    assert recaller.recall("完全无关的查询") == ()


def test_latin_recall_ignores_case(memory):
    _store, writer, recaller, _events = memory
    writer.write_fact("The user runs Windows 11")

    assert len(recaller.facts("windows")) == 1


def test_recall_can_be_limited_and_scoped(memory):
    _store, writer, recaller, _events = memory
    writer.write_turn("北京今天下雨")
    writer.write_fact("用户住在北京")

    assert len(recaller.recall("北京")) == 2
    assert len(recaller.recall("北京", limit=1)) == 1
    assert [r.scope for r in recaller.recall("北京", scope="mid")] == ["mid"]


def test_recall_survives_fts_syntax_in_an_utterance(memory):
    """A query comes from a microphone; ``NOT`` and ``*`` arrive as plain words."""
    _store, writer, recaller, _events = memory
    writer.write_fact("用户偏好中文回答")

    for query in ["NOT 中文", "中文 OR *", '"unbalanced', "中文 AND (", ""]:
        recaller.recall(query)  # must not raise


def test_recent_turns_come_back_in_time_order(memory):
    _store, writer, recaller, _events = memory
    for index in range(3):
        writer.write_turn(f"第 {index} 句")

    texts = [record.text for record in recaller.recent_turns(limit=2)]
    assert texts == ["第 1 句", "第 2 句"]


def test_strict_match_wins_over_the_loose_fallback(memory):
    """Both stages work, and the strict one is preferred when it finds anything."""
    _store, writer, recaller, _events = memory
    writer.write_fact("用户偏好中文回答")
    writer.write_fact("今天天气很好")

    strict = recaller.facts("偏好")
    assert [record.text for record in strict] == ["用户偏好中文回答"]
    assert match_expression("偏好", require_all=True) == '"偏好"'
    assert " OR " in match_expression("偏好中文", require_all=False)


def test_index_and_query_tokenizers_agree_on_chinese():
    assert "中文" in index_tokens("用户喜欢用中文交流")
    assert "中" in index_tokens("用户喜欢用中文交流")
    # Single characters are dropped from the query side, which is what keeps
    # 「偏好」 from matching a record that merely contains 「好」.
    assert query_tokens("偏好") == ("偏好",)
    assert query_tokens("hello WORLD") == ("hello", "world")


# -- the long layer feeding the router ---------------------------------------


def test_success_rate_has_no_opinion_without_observations(memory):
    _store, _writer, recaller, _events = memory
    assert recaller.success_rate("claude")["rate"] is None


def test_success_rate_is_computed_from_audit_tags(memory):
    _store, writer, recaller, _events = memory
    writer.record_agent_outcome("claude", True, latency_ms=820)
    writer.record_agent_outcome("claude", True)
    writer.record_agent_outcome("claude", False)
    writer.record_agent_outcome("opencode", False)

    claude = recaller.success_rate("claude")
    assert (claude["ok"], claude["failed"], claude["total"]) == (2, 1, 3)
    assert claude["rate"] == pytest.approx(2 / 3)
    assert recaller.success_rate("opencode")["rate"] == 0.0


# -- the credential filter (FR-12.6 / NFR-2.10) ------------------------------

CREDENTIAL_SAMPLES = [
    "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz012345",
    "token ghp_abcdefghijklmnopqrstuvwxyz0123",
    "AKIAIOSFODNN7EXAMPLE 是我的 access key id",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    "-----BEGIN RSA PRIVATE KEY-----",
    "password = hunter2hunter2",
    "api_key: 0123456789abcdef",
    "我的密钥是 abcdef123456",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQabcdefgh",
]


@pytest.mark.parametrize("sample", CREDENTIAL_SAMPLES)
def test_credential_shaped_text_never_reaches_the_database(memory, sample):
    """Refused whole, not redacted: a partial match would store the remainder."""
    store, writer, _recaller, _events = memory

    assert looks_like_secret(sample) is not None
    assert writer.write_turn(sample) is None
    assert writer.write_fact(sample) is None
    assert store.count() == 0


def test_a_refusal_emits_no_event_and_keeps_no_text(memory):
    _store, writer, _recaller, events = memory
    writer.write_turn("我的 key 是 sk-abcdefghijklmnopqrstuvwxyz012345")

    assert events == []
    assert writer.refusals == 1
    assert writer.last_refusal is not None
    assert "sk-" not in repr(writer.last_refusal)
    assert "sk-" not in repr(writer.describe())


@pytest.mark.parametrize(
    "sample",
    [
        "用户偏好中文回答",
        "读一下 README.md 的第一段",
        "我的密码忘了怎么办",
        "token 是什么意思",
        "帮我看看 docs/testing.md",
    ],
)
def test_ordinary_speech_is_not_mistaken_for_a_credential(sample):
    """A filter that fires on normal sentences would be switched off within a day."""
    assert looks_like_secret(sample) is None


def test_every_declared_pattern_has_a_name():
    assert len(SECRET_PATTERNS) >= 8
    assert all(name and pattern for name, pattern in SECRET_PATTERNS)


# -- the human-readable mirror -----------------------------------------------


def test_a_fact_is_mirrored_to_a_markdown_file(tmp_path, memory):
    _store, writer, _recaller, _events = memory
    record_id = writer.write_fact("用户住在北京", tags=("profile",))

    files = list((tmp_path / "facts").glob("*.md"))
    assert len(files) == 1
    meta, body = parse_fact_file(files[0].read_text(encoding="utf-8"))
    assert meta["id"] == record_id
    assert meta["tags"] == "profile"
    assert body == "用户住在北京"


def test_a_hand_edited_fact_shows_up_in_the_next_recall(tmp_path, memory):
    """The files are the source of truth; SQLite is the index over them."""
    _store, writer, recaller, _events = memory
    writer.write_fact("用户住在北京")
    path = next((tmp_path / "facts").glob("*.md"))

    path.write_text(
        path.read_text(encoding="utf-8").replace("北京", "上海"), encoding="utf-8"
    )
    counts = writer.sync_facts()

    assert counts["updated"] == 1
    assert [record.text for record in recaller.facts("上海")] == ["用户住在上海"]
    assert recaller.facts("北京") == ()


def test_a_plain_file_dropped_into_the_facts_dir_gets_indexed(tmp_path, memory):
    """No front matter required -- boilerplate would defeat hand-editability."""
    _store, writer, recaller, _events = memory
    facts = tmp_path / "facts"
    facts.mkdir(parents=True, exist_ok=True)
    (facts / "note.md").write_text("每天早上要看邮件", encoding="utf-8")

    counts = writer.sync_facts()

    assert counts["created"] == 1
    assert len(recaller.facts("邮件")) == 1
    # The id is written back, so the next edit updates rather than duplicates.
    meta, _body = parse_fact_file((facts / "note.md").read_text(encoding="utf-8"))
    assert meta["id"]
    assert writer.sync_facts() == {
        "scanned": 1,
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "refused": 0,
        "pruned": 0,
    }


def test_a_credential_in_a_hand_edited_file_is_refused(tmp_path, memory):
    store, writer, _recaller, _events = memory
    facts = tmp_path / "facts"
    facts.mkdir(parents=True, exist_ok=True)
    (facts / "bad.md").write_text("api_key = 0123456789abcdef", encoding="utf-8")

    counts = writer.sync_facts()

    assert counts["refused"] == 1
    assert store.count(scope="mid") == 0


def test_deleting_a_fact_file_removes_it_from_the_index_only_when_pruning(tmp_path, memory):
    store, writer, _recaller, _events = memory
    writer.write_fact("用户住在北京")
    next((tmp_path / "facts").glob("*.md")).unlink()

    assert writer.sync_facts()["pruned"] == 0
    assert store.count(scope="mid") == 1

    assert writer.sync_facts(prune=True)["pruned"] == 1
    assert store.count(scope="mid") == 0


def test_sync_is_a_no_op_without_a_facts_directory(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.db")
    writer = MemoryWriter(store, facts_dir=tmp_path / "absent")

    assert writer.sync_facts() == {
        "scanned": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "refused": 0,
        "pruned": 0,
    }
    store.close()


def test_unterminated_front_matter_is_kept_as_body():
    meta, body = parse_fact_file("---\nid: x\n用户住在北京")
    assert meta == {}
    assert "用户住在北京" in body


# -- configuration -----------------------------------------------------------


def test_config_defaults_are_absolute_and_inside_the_workspace():
    config = load_memory_config()
    assert config["db_path"].endswith("memory.db")
    assert "facts" in config["facts_dir"]
    assert config["recall_limit"] >= 1
    assert config["short_keep"] >= 1


def test_open_memory_wires_the_three_collaborators(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOX_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("EVOX_MEMORY_FACTS", str(tmp_path / "facts"))
    store, writer, recaller = open_memory()
    try:
        writer.write_fact("用户偏好中文回答")
        assert len(recaller.facts("中文")) == 1
        assert (tmp_path / "facts").is_dir()
    finally:
        store.close()


def test_a_future_schema_version_is_refused(tmp_path):
    """Forward compatibility is not silently assumed."""
    store = SqliteMemoryStore(tmp_path / "m.db")
    store.connection
    store.connection.execute("UPDATE schema_version SET version = 99")
    store.connection.commit()
    store.close()

    from core.memory import MemoryStoreError

    with pytest.raises(MemoryStoreError, match="schema version"):
        SqliteMemoryStore(tmp_path / "m.db").connection


def test_the_database_is_a_single_file(tmp_path, memory):
    """No server, no sidecar directory -- one file plus SQLite's own journals."""
    store, writer, _recaller, _events = memory
    writer.write_fact("用户住在北京")

    assert (tmp_path / "memory.db").is_file()
    stems = {path.name for path in tmp_path.glob("memory.db*")}
    assert stems <= {"memory.db", "memory.db-wal", "memory.db-shm", "memory.db-journal"}
    assert isinstance(store.connection, sqlite3.Connection)


def test_the_memory_directory_is_gitignored_and_the_package_is_not():
    """The pattern must be anchored.

    A bare ``memory/`` line matches at any depth, so it silently swallows
    ``core/memory/*.py`` as well -- the implementation would look committed and
    not be. The leading slash is the whole point of this assertion.
    """
    root = Path(__file__).resolve().parents[1]
    lines = [line.strip() for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()]

    assert "/memory/" in lines, "the memory store holds personal data and stays out by default"
    assert "memory/" not in lines, "an unanchored memory/ would also ignore core/memory/"
