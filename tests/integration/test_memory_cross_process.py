"""Cross-process persistence: the automatable half of release blocker #10.

Runs ``scripts/acceptance/verify_memory_persistence.py``, which drives two
genuine interpreter processes against one SQLite file: process A writes a
fact and exits, the Markdown mirror is edited out of band, then a fresh
process B must recall the original text, fold the edit via ``sync_facts``,
and afterwards return only the edited wording. No mocks anywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "verify_memory_persistence.py"

# Same rationale as core/agents/acp.py _UTF8_ENV: keep every hop of this
# subprocess tree speaking UTF-8 regardless of the host ANSI code page.
_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def test_fact_survives_a_process_boundary_and_hand_edits_stay_visible(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--workdir", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_ENV,
        cwd=str(ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["persisted_across_processes"] is True
    assert payload["hand_edit_folded"] is True
    assert payload["edited_text_recalled"] is True
    assert payload["stale_text_gone"] is True
    assert payload["audio_saved"] is False
