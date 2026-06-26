#!/usr/bin/env python3
"""Tests for eval_state.py cadence logic (pure is_due).

    python3 tests/test_eval_state.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import eval_state  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_is_due():
    print("cadence is_due:")
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    check("never-run -> due", eval_state.is_due(None, 24, now) is True)
    recent = (now - timedelta(hours=2)).isoformat()
    check("ran 2h ago, 24h interval -> not due", eval_state.is_due(recent, 24, now) is False)
    old = (now - timedelta(hours=30)).isoformat()
    check("ran 30h ago, 24h interval -> due", eval_state.is_due(old, 24, now) is True)
    check("interval 0 disables (never due)", eval_state.is_due(None, 0, now) is False)


def test_interval_from_config():
    print("interval parsing:")
    check("default when absent", eval_state._interval_from_config({}) == eval_state.DEFAULT_INTERVAL_HOURS)
    check("explicit value", eval_state._interval_from_config({"eval_interval_hours": 6}) == 6)
    check("0 kept (disables)", eval_state._interval_from_config({"eval_interval_hours": 0}) == 0)
    raised = False
    try:
        eval_state._interval_from_config({"eval_interval_hours": "soon"})
    except ValueError:
        raised = True
    check("non-numeric rejected", raised)


if __name__ == "__main__":
    print("eval_state.py tests\n")
    test_is_due()
    test_interval_from_config()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
