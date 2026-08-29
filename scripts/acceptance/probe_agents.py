"""REAL-AGENT probe: one command that says which backends can actually answer.

Release blocker #9 is "a real external agent completes one turn". On 2026-08-24 all
three configured backends were tried and all three were blocked -- ``claude`` was
not logged in, ``codex exec`` hung with no output, ``opencode`` could not reach its
cloud endpoint. That is "tried and blocked", not "not tried", and the difference is
worth keeping: the moment any one of them is logged in, this script is the retry.

It reports three levels per backend and never conflates them:

  configured   the entry exists in config/agents.toml
  available    ``check()`` found the command and the transport
  answered     a real turn came back with text -- **this** is REAL-AGENT

A mock subprocess would satisfy the first two and none of the third, which is why
this lives in scripts/acceptance/ and not in tests/.

    .venv\\Scripts\\python.exe scripts/acceptance/probe_agents.py
    .venv\\Scripts\\python.exe scripts/acceptance/probe_agents.py --agent claude --all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agents.contract import Task
from core.agents.registry import load_agents_config, open_agents

DEFAULT_PROMPT = "用一句话说明你是什么模型。"


def probe(adapter, prompt: str, timeout_note: str) -> dict:
    """One real turn. Every failure path is a result, not an exception."""
    descriptor = adapter.describe()
    row = {
        "name": descriptor.name,
        "kind": getattr(descriptor, "kind", "?"),
        "configured": True,
        "available": None,
        "answered": False,
        "level": "configured",
        "chars": 0,
        "chunks": 0,
        "elapsed_ms": 0,
        "error": "",
        "text": "",
    }
    try:
        status = adapter.check()
        row["available"] = bool(status.get("available", True))
        if not row["available"]:
            row["error"] = str(status.get("reason", "unavailable"))
            return row
        row["level"] = "available"
    except Exception as exc:  # noqa: BLE001
        row["available"] = False
        row["error"] = f"check failed: {type(exc).__name__}"
        return row

    task = Task(id=f"probe-{int(time.time())}", text=prompt)
    started = time.perf_counter()
    answer: list[str] = []
    try:
        for chunk in adapter.run(task):
            row["chunks"] += 1
            if chunk.text:
                answer.append(chunk.text)
            if chunk.kind == "done":
                row["error"] = chunk.error or ""
                break
    except Exception as exc:  # noqa: BLE001 - an adapter may raise during setup
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    joined = "".join(answer)
    row["chars"] = len(joined)
    row["text"] = joined[:400]
    if not row["error"] and joined.strip():
        row["answered"] = True
        row["level"] = "REAL-AGENT"
    elif not row["error"]:
        # A clean stream with no text is not an answer. Saying otherwise would
        # close a release blocker on an empty reply.
        row["error"] = "the stream finished with no text"
    row["timeout_note"] = timeout_note
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", action="append", help="probe only this agent (repeatable)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--all",
        action="store_true",
        help="probe entries that are disabled in the config too",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output only")
    args = parser.parse_args()

    config = load_agents_config()
    entries = config.get("agents", [])
    if args.all:
        entries = [{**entry, "enabled": True} for entry in entries]
    if args.agent:
        wanted = {name.casefold() for name in args.agent}
        entries = [entry for entry in entries if str(entry.get("name", "")).casefold() in wanted]
    adapters = open_agents({"agents": entries})

    if not adapters:
        print("没有可探测的 agent。config/agents.toml 里没有启用的条目 —— 加 --all 连关掉的也试。")
        return 1

    rows = []
    try:
        for adapter in adapters:
            name = adapter.describe().name
            if not args.json:
                print(f"--- {name} ---", flush=True)
            row = probe(adapter, args.prompt, "cli agents time out at 120s")
            rows.append(row)
            if not args.json:
                print(f"    level:     {row['level']}")
                print(f"    available: {row['available']}")
                print(f"    elapsed:   {row['elapsed_ms']}ms · {row['chunks']} chunk")
                if row["error"]:
                    print(f"    error:     {row['error']}")
                if row["text"]:
                    print(f"    text:      {row['text']}")
                print("", flush=True)
    finally:
        for adapter in adapters:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    real = [row["name"] for row in rows if row["answered"]]
    if args.json:
        print(json.dumps({"agents": rows, "real_agent": real}, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        if real:
            print(f"REAL-AGENT 已达成：{', '.join(real)}")
            print("把这些数字写进 docs/research/prototype-results.md，等级 REAL-AGENT。")
        else:
            print("没有一个后端答上来。这是「试过被挡」而不是「没试」——")
            print("把每个 error 原样记下来，恢复登录/网络后重跑这条命令即可。")
    return 0 if real else 1


if __name__ == "__main__":
    sys.exit(main())
