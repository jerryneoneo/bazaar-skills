#!/usr/bin/env python3
"""Tests for availability.py — the deterministic meetup/handover availability shim.

    python3 tests/test_availability.py

Covers the three pure builders (no I/O) that reply-pipeline cites for timing answers, plus the
CLI input guards. The file-reading run() paths are exercised indirectly via the CLI guards.
"""

import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import availability  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


# Sat 2026-07-04 is blocked; Sun 2026-07-05 and Sat 2026-07-11 are open (verified weekdays).
AVAIL = {
    "timezone": "Asia/Singapore",
    "weekly_windows": [
        {"day": "Sat", "from": "10:00", "to": "12:00"},
        {"day": "Sat", "from": "15:00", "to": "17:00"},
        {"day": "Sun", "from": "11:00", "to": "13:00"},
    ],
    "blocked_dates": ["2026-07-04"],
}


def test_local_slots_expands_windows():
    print("local: expands weekly windows across the range, minus blocked dates:")
    out = availability.build_local_slots(AVAIL, date(2026, 7, 4), date(2026, 7, 11))
    dates = [s["date"] for s in out["slots"]]
    check("source is manual", out["source"] == "manual")
    check("timezone passed through", out["timezone"] == "Asia/Singapore")
    check("blocked Saturday 07-04 excluded", "2026-07-04" not in dates)
    check("Sun 07-05 included", "2026-07-05" in dates)
    check("Sat 07-11 yields both windows", dates.count("2026-07-11") == 2)
    check("every slot carries day + from/to",
          all({"date", "day", "from", "to"} <= set(s) for s in out["slots"]))


def test_local_slots_empty_when_no_matching_days():
    print("local: a Mon-Wed range (no configured weekday) yields no slots:")
    out = availability.build_local_slots(AVAIL, date(2026, 7, 6), date(2026, 7, 8))
    check("no slots", out["slots"] == [])


def test_mcp_directive():
    print("calendar_mcp: emits an instruction (Python can't reach the MCP):")
    out = availability.build_mcp_directive(AVAIL, date(2026, 7, 1), date(2026, 7, 5))
    check("source is calendar_mcp", out["source"] == "calendar_mcp")
    check("range echoed", out["range"] == {"from": "2026-07-01", "to": "2026-07-05"})
    check("instruction names the MCP + the end date",
          "Calendar MCP" in out["instruction"] and "2026-07-05" in out["instruction"])
    check("never invents availability", "Never invent availability" in out["instruction"])


def test_skip_directive():
    print("skip: vague timing, promises no specific date:")
    out = availability.build_skip_directive()
    check("source is skip", out["source"] == "skip")
    check("instruction stays cautious", "don't promise" in out["instruction"])


def cli(*a):
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "availability.py"), *a],
                       capture_output=True, text=True)
    return p.returncode


def test_cli_input_guards():
    print("CLI: malformed/invalid input is rejected with exit 2:")
    check("non-date → exit 2", cli("not-a-date", "2026-07-05") == 2)
    check("reversed range → exit 2", cli("2026-07-10", "2026-07-01") == 2)
    check("missing arg → exit 2", cli("2026-07-01") == 2)


if __name__ == "__main__":
    print("availability tests\n")
    test_local_slots_expands_windows()
    test_local_slots_empty_when_no_matching_days()
    test_mcp_directive()
    test_skip_directive()
    test_cli_input_guards()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
