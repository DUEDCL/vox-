"""Release blocker #6: a ≥30 minute CPU / memory / event-rate profile.

Runs the console-less runtime, samples this process, and writes a CSV plus a
summary. It can be started unattended -- but the *conclusion* still needs a human,
because "is 180 MB acceptable" depends on the machine and on what else is running.
The script reports numbers and refuses to grade them.

Zero new dependencies: ``psutil`` is not in requirements, so on Windows the
counters come from ``GetProcessMemoryInfo`` / ``GetProcessTimes`` via ``ctypes``, and
elsewhere from ``resource``. When neither is available it records "not collected"
rather than a zero -- a zero would read as a measurement.

    .venv\\Scripts\\python.exe scripts/acceptance/resource_profile.py --minutes 30
    .venv\\Scripts\\python.exe scripts/acceptance/resource_profile.py --minutes 1 --voice
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import os
import platform
import statistics
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audio import load_voice_config
from vox_plugin.runtime import VoiceRuntime
from vox_plugin.voice_stack import open_voice_stack


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_seconds(value: _FileTime) -> float:
    """FILETIME is 100 ns ticks."""
    return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 1e7


class Sampler:
    """Process RSS and cumulative CPU seconds, or an honest "unavailable"."""

    def __init__(self) -> None:
        self.backend = "none"
        self.detail = ""
        self._handle = None
        if platform.system() == "Windows":
            try:
                self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
                # Declaring these is not optional on 64-bit: ctypes defaults a
                # return value to ``c_int``, so ``GetCurrentProcess``'s pseudo
                # handle (HANDLE)-1 comes back truncated to 0xFFFFFFFF and every
                # subsequent call fails with an invalid handle. The symptom is a
                # profile of "n/a" that looks like an unsupported platform.
                self._kernel.GetCurrentProcess.restype = wintypes.HANDLE
                self._kernel.GetCurrentProcess.argtypes = []
                self._kernel.GetProcessTimes.restype = wintypes.BOOL
                self._kernel.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(_FileTime),
                    ctypes.POINTER(_FileTime),
                    ctypes.POINTER(_FileTime),
                    ctypes.POINTER(_FileTime),
                ]
                self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
                self._psapi.GetProcessMemoryInfo.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(_MemoryCounters),
                    wintypes.DWORD,
                ]
                self._handle = self._kernel.GetCurrentProcess()
                self.backend = "windows"
            except (OSError, AttributeError) as exc:
                self.backend = "none"
                self.detail = f"{type(exc).__name__}: {exc}"
        else:
            try:
                import resource  # noqa: F401 - probing availability

                self.backend = "resource"
            except ImportError as exc:
                self.backend = "none"
                self.detail = f"{type(exc).__name__}: {exc}"

    def rss_mb(self) -> float | None:
        if self.backend == "windows":
            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(_MemoryCounters)
            if not self._psapi.GetProcessMemoryInfo(
                self._handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return counters.WorkingSetSize / (1024 * 1024)
        if self.backend == "resource":
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports kB, macOS reports bytes.
            return usage / 1024 if platform.system() == "Linux" else usage / (1024 * 1024)
        return None

    def cpu_seconds(self) -> float | None:
        if self.backend == "windows":
            creation, exit_, kernel, user = (_FileTime() for _ in range(4))
            if not self._kernel.GetProcessTimes(
                self._handle,
                ctypes.byref(creation),
                ctypes.byref(exit_),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return _filetime_to_seconds(kernel) + _filetime_to_seconds(user)
        if self.backend in {"resource"}:
            return time.process_time()
        return None


def summarise(rows: list[dict]) -> dict:
    """The numbers a writeup needs. No verdict -- that part needs a human."""
    if not rows:
        return {"samples": 0}
    rss = [row["rss_mb"] for row in rows if row["rss_mb"] is not None]
    cpu = [row["cpu_pct"] for row in rows if row["cpu_pct"] is not None]
    events = [row["events"] for row in rows]
    report: dict = {
        "samples": len(rows),
        "minutes": round(rows[-1]["t_s"] / 60, 2),
        "events_total": events[-1] if events else 0,
    }
    if rss:
        report.update(
            rss_mb_first=round(rss[0], 1),
            rss_mb_last=round(rss[-1], 1),
            rss_mb_peak=round(max(rss), 1),
            # Growth is the number that matters over 30 minutes: a steady 180 MB is
            # fine, 180 climbing to 900 is a leak.
            rss_mb_growth=round(rss[-1] - rss[0], 1),
        )
    else:
        report["rss"] = "not collected"
    if cpu:
        report.update(
            cpu_pct_mean=round(statistics.fmean(cpu), 2),
            cpu_pct_peak=round(max(cpu), 2),
        )
    else:
        report["cpu"] = "not collected"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between samples")
    parser.add_argument("--voice", action="store_true", help="open the microphone too")
    parser.add_argument("--orb", action="store_true", help="spawn the wake orb too")
    parser.add_argument("--out", default=".vox-resource-profile.csv")
    args = parser.parse_args()

    sampler = Sampler()
    if sampler.backend == "none":
        print("warning: 这台机器上采不到进程计数器，CSV 里会记 not collected。")

    config = load_voice_config()
    stack = open_voice_stack(config, with_tts=False)
    runtime = VoiceRuntime(with_desktop=args.orb, visible=False)
    report = runtime.start()
    print(f"orb={report.desktop} tools={len(report.tools)} agents={len(report.agents)}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    if args.voice:
        runtime.attach_microphone(stack.capture)
        try:
            stack.capture.start()
            print("microphone: open")
        except Exception as exc:  # noqa: BLE001
            print(f"microphone did not open: {type(exc).__name__}: {exc}")

    rows: list[dict] = []
    started = time.monotonic()
    deadline = started + args.minutes * 60
    last_cpu = sampler.cpu_seconds()
    last_at = started
    print(f"sampling every {args.interval:g}s for {args.minutes:g} min. Ctrl+C stops early.")
    try:
        while time.monotonic() < deadline:
            time.sleep(args.interval)
            now = time.monotonic()
            cpu_now = sampler.cpu_seconds()
            cpu_pct = None
            if cpu_now is not None and last_cpu is not None and now > last_at:
                cpu_pct = (cpu_now - last_cpu) / (now - last_at) * 100
            last_cpu, last_at = cpu_now, now
            row = {
                "t_s": round(now - started, 1),
                "rss_mb": sampler.rss_mb(),
                "cpu_pct": cpu_pct,
                "events": len(runtime.seen),
                "turns": runtime.turns,
                "state": runtime.plugin.machine.state.value,
                "callback_errors": getattr(stack.capture, "callback_errors", 0),
            }
            rows.append(row)
            print(
                f"  {row['t_s']:>7.1f}s  rss="
                f"{'n/a' if row['rss_mb'] is None else format(row['rss_mb'], '.1f')}MB  cpu="
                f"{'n/a' if cpu_pct is None else format(cpu_pct, '.1f')}%  "
                f"events={row['events']} state={row['state']}",
                flush=True,
            )
    except KeyboardInterrupt:
        print("stopped early")
    finally:
        if args.voice:
            stack.capture.stop()
        runtime.close()
        stack.close()

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["t_s", "rss_mb", "cpu_pct", "events", "turns", "state", "callback_errors"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})

    report = summarise(rows)
    print("")
    print(f"csv: {out}")
    for key, value in report.items():
        print(f"  {key}: {value}")
    print("")
    print("等级 REAL-WIN。这个脚本只报数字，不给结论 ——「180 MB 可不可以接受」")
    print("取决于这台机器和同时在跑的东西，那一句要你自己写进 prototype-results.md。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
