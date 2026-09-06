"""Cross-process memory persistence verification.

Closes the automatable half of release blocker #10: a fact written in one
process must be recallable in a fresh process, and a hand edit to the
Markdown mirror must be visible to the next recall after ``sync_facts``.

Two real subprocesses against one SQLite file on disk -- no mocks. Audio is
never touched; the scratch database lives in a caller-provided directory
(never the user's ``memory/``). Output: one JSON line, last stdout line.

Evidence level: AUTO (multi-process local automation). It does not claim
REAL-MIC or any device-level evidence.

Usage:
    python scripts/acceptance/verify_memory_persistence.py --workdir <scratch-dir>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FACT_ORIGINAL = "用户偏好中文回答，讨厌冗长的解释"
FACT_EDITED = "用户偏好中文回答，而且喜欢简洁的回复"
DB_NAME = "memory.db"
FACTS_DIR_NAME = "facts"


def _paths(workdir: Path) -> tuple[Path, Path]:
    return workdir / DB_NAME, workdir / FACTS_DIR_NAME


def child_write(workdir: str) -> int:
    from core.memory.store import SqliteMemoryStore
    from core.memory.write import MemoryWriter

    db, facts = _paths(Path(workdir))
    store = SqliteMemoryStore(db)
    try:
        writer = MemoryWriter(store, facts_dir=facts)
        record_id = writer.write_fact(FACT_ORIGINAL, tags=("偏好",))
        if record_id is None:
            print(json.dumps({"error": "write_fact refused"}))
            return 1
        source = store.source_of(record_id)
    finally:
        store.close()
    print(json.dumps({"id": record_id, "source": source}, ensure_ascii=False))
    return 0


def child_verify(workdir: str) -> int:
    from core.memory.recall import MemoryRecaller
    from core.memory.store import SqliteMemoryStore
    from core.memory.write import MemoryWriter

    db, facts = _paths(Path(workdir))
    store = SqliteMemoryStore(db)
    try:
        recaller = MemoryRecaller(store)
        writer = MemoryWriter(store, facts_dir=facts)

        # 1) Persistence: the fact written by the *other* process is there.
        hits = recaller.facts("中文回答")
        persisted = len(hits) == 1 and hits[0].text == FACT_ORIGINAL

        # 2) The hand-edited Markdown folds into the index in this process.
        counts = writer.sync_facts()
        folded = counts.get("updated") == 1

        # 3) The edited wording is what recall now returns.
        edited_hits = recaller.facts("简洁")
        edited_visible = len(edited_hits) == 1 and edited_hits[0].text == FACT_EDITED

        # 4) The superseded wording no longer matches.
        stale_hits = recaller.facts("讨厌冗长")
        stale_gone = len(stale_hits) == 0
    finally:
        store.close()

    print(
        json.dumps(
            {
                "persisted_across_processes": persisted,
                "sync_counts": counts,
                "hand_edit_folded": folded,
                "edited_text_recalled": edited_visible,
                "stale_text_gone": stale_gone,
            },
            ensure_ascii=False,
        )
    )
    return 0 if all([persisted, folded, edited_visible, stale_gone]) else 1


# Same rationale as core/agents/acp.py _UTF8_ENV: a Python child on Windows
# writes its stdout in the ANSI code page unless forced, and the JSON here
# carries Chinese fact text -- silent U+FFFD corruption is worse than failure.
_CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run_child(role: str, workdir: Path):
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), f"--{role}", "--workdir", str(workdir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_CHILD_ENV,
        cwd=str(ROOT),
        timeout=120,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, help="scratch directory for db + facts")
    parser.add_argument("--write", action="store_true", help="internal: phase 1 child")
    parser.add_argument("--verify", action="store_true", help="internal: phase 2 child")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    if args.write:
        return child_write(str(workdir))
    if args.verify:
        return child_verify(str(workdir))

    # Parent orchestration: write -> hand-edit -> verify, three processes.
    workdir.mkdir(parents=True, exist_ok=True)
    code, out, err = run_child("write", workdir)
    if code != 0:
        print(json.dumps({"all_pass": False, "stage": "write", "stdout": out, "stderr": err[-2000:]}))
        return 1
    written = json.loads(out.strip().splitlines()[-1])

    # Simulate the human editing the mirrored Markdown (front matter kept).
    fact_file = workdir / FACTS_DIR_NAME / written["source"]
    raw = fact_file.read_text(encoding="utf-8")
    head, sep, _body = raw.partition("\n\n")
    if not sep:
        print(json.dumps({"all_pass": False, "stage": "edit", "reason": "no front-matter separator"}))
        return 1
    fact_file.write_text(head + "\n\n" + FACT_EDITED + "\n", encoding="utf-8")

    code, out, err = run_child("verify", workdir)
    checks = {}
    if out.strip():
        try:
            checks = json.loads(out.strip().splitlines()[-1])
        except json.JSONDecodeError:
            pass
    payload = {
        "evidence": "CROSS_PROCESS_MEMORY_PERSISTENCE_LOCAL",
        "level": "AUTO_MULTI_PROCESS",
        "written_id": written["id"],
        "fact_source": written["source"],
        **checks,
        "audio_saved": False,
        "exit_code": code,
    }
    all_pass = code == 0 and all(
        payload.get(k) is True
        for k in (
            "persisted_across_processes",
            "hand_edit_folded",
            "edited_text_recalled",
            "stale_text_gone",
        )
    )
    payload["all_pass"] = all_pass
    # ASCII-escaped so the machine-readable line survives any console codepage.
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
