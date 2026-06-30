#!/usr/bin/env python3
"""Tests for relist_state.py — the per-(item, market) FREE-relist cooldown ledger.

    python3 tests/test_relist_state.py

Focus: the deterministic cooldown decision + the atomic stamp. due() is True before any relist,
False within `relist_cooldown_days`, True after it lapses; per-market keys are independent; a
cooldown of 0 disables the gate; the CLI exits cleanly and round-trips via SELLY_DATA_DIR.
State isolated per test via tmp dirs / SELLY_DATA_DIR.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import relist_state as rs  # noqa: E402

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _write_json(tmp, rel, payload):
    p = Path(tmp) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))
    return p


def _at(days):
    return (NOW - timedelta(days=days)).isoformat()


# ---- pure helpers ----------------------------------------------------------

def test_is_due_pure():
    print("is_due: never relisted -> True; within cooldown -> False; lapsed -> True:")
    empty = {}
    check("never relisted -> due", rs.is_due("carousell", "abc", empty, 1.0, NOW) is True)
    ledger = {"items": {"carousell:abc": {"last_relist_at": _at(0.25)}}}  # 6h ago, cooldown 1d
    check("within 1-day cooldown -> NOT due", rs.is_due("carousell", "abc", ledger, 1.0, NOW) is False)
    lapsed = {"items": {"carousell:abc": {"last_relist_at": _at(2)}}}     # 2d ago, cooldown 1d
    check("past cooldown -> due", rs.is_due("carousell", "abc", lapsed, 1.0, NOW) is True)


def test_is_due_zero_cooldown_disables():
    print("cooldown 0 -> always due (gate disabled):")
    ledger = {"items": {"carousell:abc": {"last_relist_at": _at(0)}}}     # relisted right now
    check("cooldown 0 -> due even just-relisted", rs.is_due("carousell", "abc", ledger, 0.0, NOW) is True)


def test_is_due_failopen_on_corrupt_stamp():
    print("a corrupt/missing stamp fails OPEN to due (eligible):")
    ledger = {"items": {"carousell:abc": {"last_relist_at": "not-a-date"}}}
    check("unparseable stamp -> due", rs.is_due("carousell", "abc", ledger, 1.0, NOW) is True)
    check("entry without a stamp -> due",
          rs.is_due("carousell", "abc", {"items": {"carousell:abc": {}}}, 1.0, NOW) is True)


def test_per_market_keys_independent():
    print("the SAME item on two markets has independent cooldowns:")
    ledger = rs.mark_relisted("carousell", "abc", {}, NOW)
    check("carousell stamped -> NOT due", rs.is_due("carousell", "abc", ledger, 1.0, NOW) is False)
    check("same item on fb -> still due", rs.is_due("fb", "abc", ledger, 1.0, NOW) is True)


def test_mark_relisted_immutable():
    print("mark_relisted returns a NEW ledger, never mutates the input:")
    original = {"items": {}}
    updated = rs.mark_relisted("carousell", "abc", original, NOW)
    check("input untouched", original == {"items": {}})
    check("new ledger has the stamp",
          updated["items"]["carousell:abc"]["last_relist_at"] == NOW.isoformat())


# ---- IO: run_due / run_mark ------------------------------------------------

def test_run_due_and_mark_roundtrip():
    print("run_due true before; run_mark stamps; run_due false within cooldown; true after lapse:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"relist_cooldown_days": 1})
        first = rs.run_due("abc", "carousell", NOW, base=base)
        check("due before any relist", first["due"] is True)
        check("cooldown_days surfaced", first["cooldown_days"] == 1.0)
        rs.run_mark("abc", "carousell", NOW, base=base)
        within = rs.run_due("abc", "carousell", NOW + timedelta(hours=6), base=base)
        check("NOT due 6h later", within["due"] is False)
        check("last_relist_at surfaced", within["last_relist_at"] == NOW.isoformat())
        after = rs.run_due("abc", "carousell", NOW + timedelta(days=2), base=base)
        check("due again 2 days later", after["due"] is True)


def test_run_due_default_cooldown_when_absent():
    print("absent relist_cooldown_days -> the 1-day default applies:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {})
        rs.run_mark("abc", "carousell", NOW, base=base)
        within = rs.run_due("abc", "carousell", NOW + timedelta(hours=6), base=base)
        check("default cooldown -> NOT due 6h later", within["due"] is False)
        check("default cooldown_days is 1.0", within["cooldown_days"] == 1.0)


# ---- CLI smoke -------------------------------------------------------------

def test_cli():
    print("CLI: due/mark exit codes + JSON via SELLY_DATA_DIR:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"relist_cooldown_days": 1})
        env = {**os.environ, "SELLY_DATA_DIR": tmp}
        exe = [sys.executable, str(ROOT / "bin" / "relist_state.py")]
        d = subprocess.run(exe + ["due", "--item", "abc", "--market", "carousell", "--now", NOW.isoformat()],
                           capture_output=True, text=True, env=env)
        check("due exit 0", d.returncode == 0)
        check("due JSON true initially", json.loads(d.stdout)["due"] is True)
        m = subprocess.run(exe + ["mark", "--item", "abc", "--market", "carousell", "--now", NOW.isoformat()],
                           capture_output=True, text=True, env=env)
        check("mark exit 0", m.returncode == 0)
        d2 = subprocess.run(exe + ["due", "--item", "abc", "--market", "carousell",
                                   "--now", (NOW + timedelta(hours=6)).isoformat()],
                            capture_output=True, text=True, env=env)
        check("due false within cooldown after mark", json.loads(d2.stdout)["due"] is False)
        bad = subprocess.run(exe + ["due", "--item", "abc"], capture_output=True, text=True, env=env)
        check("due without --market -> exit 2", bad.returncode == 2)


if __name__ == "__main__":
    print("relist_state.py tests\n")
    test_is_due_pure()
    test_is_due_zero_cooldown_disables()
    test_is_due_failopen_on_corrupt_stamp()
    test_per_market_keys_independent()
    test_mark_relisted_immutable()
    test_run_due_and_mark_roundtrip()
    test_run_due_default_cooldown_when_absent()
    test_cli()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
