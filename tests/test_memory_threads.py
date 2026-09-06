"""The memory store under concurrent callers.

The console's HTTP handler runs on a ``ThreadingHTTPServer`` worker, the turn pump
on another thread, and the audio callback on a third. Before the lock landed, the
first query bound the connection to whichever thread ran it and every later thread
got ``sqlite3.ProgrammingError`` -- an intermittent failure whose symptom (a
profile save that syncs, then a profile delete that does not) reads like a bug in
the caller rather than a threading mistake.

Evidence level: AUTO. Real threads, real SQLite file, no mocks.
"""

from __future__ import annotations

import threading
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.memory.contract import MemoryRecord
from core.memory.store import SqliteMemoryStore, new_id


def record(text: str, *, scope: str = "mid", kind: str = "fact") -> MemoryRecord:
    return MemoryRecord(id=new_id(), scope=scope, kind=kind, text=text)


def run_in_threads(work, count: int = 8) -> list:
    """Run ``work(i)`` on ``count`` threads, returning results or exceptions."""
    results: list = [None] * count
    barrier = threading.Barrier(count)

    def target(index: int) -> None:
        # The barrier makes the threads actually overlap rather than run in turn,
        # which is what a sequential test would accidentally do.
        barrier.wait()
        try:
            results[index] = work(index)
        except Exception as exc:  # noqa: BLE001 - the assertion is about this
            results[index] = exc

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return results


def test_a_write_on_one_thread_and_a_read_on_another(tmp_path):
    """This is the exact pair that failed: first query on thread A, next on B."""
    store = SqliteMemoryStore(tmp_path / "memory.db")
    written: list[str] = []

    first = threading.Thread(target=lambda: written.append(store.write(record("第一条"))))
    first.start()
    first.join(timeout=10)

    read: list = []
    second = threading.Thread(target=lambda: read.append(store.count(scope="mid")))
    second.start()
    second.join(timeout=10)

    assert written and read == [1]
    store.close()


def test_concurrent_writes_all_land(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")

    results = run_in_threads(lambda i: store.write(record(f"事实 {i}")), count=8)

    assert not any(isinstance(item, Exception) for item in results), results
    assert store.count(scope="mid") == 8
    store.close()


def test_concurrent_reads_and_writes_do_not_raise(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.write(record("起始事实"))

    def work(index: int):
        if index % 2:
            return store.write(record(f"并发 {index}"))
        return len(store.recall("事实", scope="mid"))

    results = run_in_threads(work, count=10)

    assert not any(isinstance(item, Exception) for item in results), results
    store.close()


def test_dedup_stays_correct_under_concurrency(tmp_path):
    """The mid scope de-duplicates by fingerprint. Ten threads writing the same
    fact must produce one row, not ten -- the partial unique index enforces it, and
    the lock is what keeps the read-then-write around it from interleaving."""
    store = SqliteMemoryStore(tmp_path / "memory.db")

    results = run_in_threads(lambda i: store.write(record("用户喜欢用中文交流")), count=10)

    assert not any(isinstance(item, Exception) for item in results), results
    assert store.count(scope="mid") == 1
    assert len({str(item) for item in results}) == 1, "every writer got the same id"
    store.close()


def test_prune_and_write_can_overlap(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    for i in range(20):
        store.write(record(f"轮次 {i}", scope="short", kind="turn"))

    def work(index: int):
        if index % 3 == 0:
            return store.prune(scope="short", keep=5)
        return store.write(record(f"新轮次 {index}", scope="short", kind="turn"))

    results = run_in_threads(work, count=9)

    assert not any(isinstance(item, Exception) for item in results), results
    store.close()


def test_close_during_use_does_not_corrupt_the_lock(tmp_path):
    """A close racing a write must not leave the lock held or the object unusable."""
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.write(record("先写一条"))

    def work(index: int):
        if index == 0:
            store.close()
            return "closed"
        return store.count(scope="mid")

    run_in_threads(work, count=4)

    # Whatever the interleaving, the store reopens on the next query.
    assert store.count(scope="mid") == 1
    store.close()


def test_the_lock_is_reentrant(tmp_path):
    """``write`` calls ``connection``, which also takes the lock. A plain ``Lock``
    would deadlock on the first write; ``RLock`` is the requirement, not a detail."""
    store = SqliteMemoryStore(tmp_path / "memory.db")
    assert store.write(record("可重入"))
    store.close()
