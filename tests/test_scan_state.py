#!/usr/bin/env python3
"""Tests for scan_state.py — the cadence gate for autonomous distribution SCAN.

    python3 tests/test_scan_state.py

Pure cadence logic (`due_market` / `mark_scanned`) is tested inline; a CLI check
exercises the real files and asserts no floor/address leak in the output.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import scan_state  # noqa: E402

NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def hours_ago(n):
    from datetime import timedelta
    return (NOW - timedelta(hours=n)).isoformat()


def test_never_scanned_is_due():
    print("an enabled, never-scanned market is due:")
    sel = {"fb": {"enabled": True}, "carousell": {"enabled": True}}
    market, info = scan_state.due_market(sel, {}, 24, NOW)
    check("a never-scanned market is returned", market in {"fb", "carousell"})
    check("first enabled wins on a tie (fb)", market == "fb")
    check("fb marked overdue", info["fb"]["overdue"] is True)
    check("age is null when never scanned", info["fb"]["age_hours"] is None)


def test_recent_scan_not_due():
    print("a market scanned within the interval is NOT due:")
    sel = {"fb": {"enabled": True}}
    state = {"fb": {"last_scanned_at": hours_ago(2)}}
    market, info = scan_state.due_market(sel, state, 24, NOW)
    check("nothing due", market is None)
    check("fb not overdue", info["fb"]["overdue"] is False)
    check("age ~2h", abs(info["fb"]["age_hours"] - 2.0) < 0.01)


def test_stale_scan_is_due():
    print("a market last scanned past the interval is due:")
    sel = {"fb": {"enabled": True}}
    state = {"fb": {"last_scanned_at": hours_ago(30)}}
    market, _ = scan_state.due_market(sel, state, 24, NOW)
    check("fb is due again after 30h (interval 24h)", market == "fb")


def test_most_overdue_wins():
    print("the MOST overdue market is chosen among several due:")
    sel = {"fb": {"enabled": True}, "carousell": {"enabled": True}, "ebay": {"enabled": True}}
    state = {
        "fb": {"last_scanned_at": hours_ago(25)},
        "carousell": {"last_scanned_at": hours_ago(50)},
        "ebay": {"last_scanned_at": hours_ago(30)},
    }
    market, _ = scan_state.due_market(sel, state, 24, NOW)
    check("carousell (oldest) wins", market == "carousell")


def test_never_beats_old():
    print("a never-scanned market outranks an old-but-scanned one:")
    sel = {"fb": {"enabled": True}, "carousell": {"enabled": True}}
    state = {"fb": {"last_scanned_at": hours_ago(1000)}}  # carousell never scanned
    market, _ = scan_state.due_market(sel, state, 24, NOW)
    check("carousell (never scanned) wins over very-old fb", market == "carousell")


def test_disabled_market_ignored():
    print("a disabled market is never due, even if never scanned:")
    sel = {"fb": {"enabled": False}, "carousell": {"enabled": True}}
    state = {"carousell": {"last_scanned_at": hours_ago(1)}}
    market, info = scan_state.due_market(sel, state, 24, NOW)
    check("nothing due (fb disabled, carousell fresh)", market is None)
    check("fb absent from info", "fb" not in info)


def test_array_selection_all_enabled():
    print("legacy ARRAY selection treats every listed market as enabled:")
    market, info = scan_state.due_market(["fb", "carousell"], {}, 24, NOW)
    check("array form yields a due market", market == "fb")
    check("both present in info", set(info) == {"fb", "carousell"})


def test_mark_is_immutable():
    print("mark_scanned returns a new dict and never mutates the input:")
    original = {"fb": {"last_scanned_at": hours_ago(99)}}
    updated = scan_state.mark_scanned(original, "carousell", NOW)
    check("original unchanged", original == {"fb": {"last_scanned_at": hours_ago(99)}})
    check("carousell stamped at now", updated["carousell"]["last_scanned_at"] == NOW.isoformat())
    check("fb preserved", updated["fb"] == original["fb"])


def test_parse_iso_z_and_offset():
    print("parse_iso handles 'Z' and explicit offsets:")
    z = scan_state.parse_iso("2026-06-24T12:00:00Z")
    off = scan_state.parse_iso("2026-06-24T20:00:00+08:00")
    check("Z parsed as UTC noon", z == NOW)
    check("+08:00 equals the same instant", off == NOW)
    check("empty -> None", scan_state.parse_iso("") is None)


def test_cli_due_no_secret_leak():
    print("CLI `due` runs on real files and leaks no floor/address:")
    if not (ROOT / "data" / "seller_config.json").exists():
        print("  [SKIP] CLI `due` check — no data/seller_config.json yet (run after onboarding)")
        return
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "scan_state.py"), "due", "--now", NOW.isoformat()],
        capture_output=True, text=True,
    )
    ok = proc.returncode == 0
    if ok:
        payload = json.loads(proc.stdout)
        ok = "due_market" in payload and "markets" in payload and "interval_hours" in payload
        ok = ok and "Sample Road" not in proc.stdout and "000000" not in proc.stdout
    check("due returns a valid envelope, no floor/address leak", ok)


def test_cli_bad_input():
    print("CLI rejects a bad subcommand and an unparseable --now:")
    bad = [["bogus"], ["due", "--now", "not-a-date"]]
    ok = True
    for args in bad:
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "scan_state.py"), *args],
                           capture_output=True, text=True)
        if p.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed input exits nonzero", ok)


if __name__ == "__main__":
    print("scan_state tests\n")
    test_never_scanned_is_due()
    test_recent_scan_not_due()
    test_stale_scan_is_due()
    test_most_overdue_wins()
    test_never_beats_old()
    test_disabled_market_ignored()
    test_array_selection_all_enabled()
    test_mark_is_immutable()
    test_parse_iso_z_and_offset()
    test_cli_due_no_secret_leak()
    test_cli_bad_input()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
