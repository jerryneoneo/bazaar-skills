#!/usr/bin/env python3
"""Tests for delist_item.py — the canonical seller-initiated take-down writer.

    python3 tests/test_delist_item.py

REGRESSION FOCUS: the original bug was that a seller-initiated delete with NO active
listing session wrote its deletion marker into the transient single-slot
data/listing_session.json instead of the durable data/items/<id>.json, leaving the item
stuck at status:"live". These tests assert the removal ALWAYS lands on the durable item
record (and that the engine never reads or writes any session file).

Pure transition (apply_removal) is tested inline; a CLI check exercises the real script
against a temp items dir via $SELLY_ITEMS_DIR.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import delist_item  # noqa: E402

NOW = "2026-06-23T14:31:04+08:00"

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def live_record():
    return {
        "item_id": "logitech-mx-master-3",
        "title": "Logitech MX Master 3 Wireless Mouse (Graphite)",
        "list_price": 85,
        "currency": "SGD",
        "listing_urls": {
            "fb": "https://www.facebook.com/marketplace/item/27120758060952323/",
            "carousell": "https://www.carousell.sg/p/logitech-mx-master-3-advanced-wireless-mouse-1445444407/",
        },
        "status": "live",
    }


def test_apply_removal_transition():
    print("apply_removal flips a live record to removed and archives URLs:")
    rec = live_record()
    out = delist_item.apply_removal(rec, NOW)
    check("status -> removed_by_seller", out["status"] == "removed_by_seller")
    check("listing_urls cleared to {}", out["listing_urls"] == {})
    check("removed_urls holds the old fb url",
          out["removed_urls"].get("fb") == rec["listing_urls"]["fb"])
    check("removed_urls holds the old carousell url",
          out["removed_urls"].get("carousell") == rec["listing_urls"]["carousell"])
    check("removed_at stamped", out["removed_at"] == NOW)
    check("non-status fields preserved (list_price)", out["list_price"] == 85)


def test_apply_removal_is_immutable():
    print("apply_removal does not mutate the input record:")
    rec = live_record()
    delist_item.apply_removal(rec, NOW)
    check("input status still live", rec["status"] == "live")
    check("input listing_urls untouched", rec["listing_urls"] != {})
    check("input gained no removed_urls", "removed_urls" not in rec)


def test_apply_removal_idempotent_keeps_archive():
    print("re-removing an already-removed record never loses archived URLs:")
    rec = live_record()
    once = delist_item.apply_removal(rec, NOW)
    twice = delist_item.apply_removal(once, "2026-06-24T00:00:00+08:00")
    check("status stays removed_by_seller", twice["status"] == "removed_by_seller")
    check("listing_urls stays {}", twice["listing_urls"] == {})
    check("archived fb url survives second pass",
          twice["removed_urls"].get("fb") == rec["listing_urls"]["fb"])
    check("removed_at refreshed to new time", twice["removed_at"] == "2026-06-24T00:00:00+08:00")


def test_optional_reason():
    print("reason is written only when supplied:")
    no_reason = delist_item.apply_removal(live_record(), NOW)
    with_reason = delist_item.apply_removal(live_record(), NOW, "seller request")
    check("no reason key when omitted", "removed_reason" not in no_reason)
    check("reason key set when given", with_reason.get("removed_reason") == "seller request")


def _run_cli(item_id, items_dir, extra=None):
    env = {**os.environ, "SELLY_ITEMS_DIR": str(items_dir)}
    args = [sys.executable, str(ROOT / "bin" / "delist_item.py"), item_id] + (extra or [])
    return subprocess.run(args, capture_output=True, text=True, env=env)


def test_cli_writes_durable_item_record_not_session():
    print("REGRESSION: CLI updates the DURABLE item record (no active session needed):")
    with tempfile.TemporaryDirectory() as tmp:
        items = Path(tmp)
        path = items / "logitech-mx-master-3.json"
        path.write_text(json.dumps(live_record()) + "\n")
        # Deliberately NO listing_session.json anywhere — this is the "no active session" case
        # that used to misroute the write. The engine must not depend on one.

        p = _run_cli("logitech-mx-master-3", items, ["--reason", "seller request"])
        check("exit 0", p.returncode == 0)

        written = json.loads(path.read_text())
        check("durable record status -> removed_by_seller", written["status"] == "removed_by_seller")
        check("durable record listing_urls cleared", written["listing_urls"] == {})
        check("durable record archived the carousell url",
              written["removed_urls"].get("carousell", "").endswith("1445444407/"))
        check("durable record has removed_at", bool(written.get("removed_at")))
        check("no listing_session.json was created in the items dir",
              not (items / "listing_session.json").exists())

        out = json.loads(p.stdout)
        check("stdout reports ok", out.get("ok") is True)
        check("stdout reports removed status", out.get("status") == "removed_by_seller")


def test_cli_missing_item_exits_3():
    print("CLI on an unknown item exits 3 (no record to remove):")
    with tempfile.TemporaryDirectory() as tmp:
        p = _run_cli("does-not-exist", Path(tmp))
        check("exit 3", p.returncode == 3)
        check("stdout reports not-ok", json.loads(p.stdout).get("ok") is False)


def test_cli_empty_item_exits_2():
    print("CLI with an empty item_id exits 2 (bad input):")
    with tempfile.TemporaryDirectory() as tmp:
        p = _run_cli("   ", Path(tmp))
        check("exit 2", p.returncode == 2)


if __name__ == "__main__":
    print("delist_item tests\n")
    test_apply_removal_transition()
    test_apply_removal_is_immutable()
    test_apply_removal_idempotent_keeps_archive()
    test_optional_reason()
    test_cli_writes_durable_item_record_not_session()
    test_cli_missing_item_exits_3()
    test_cli_empty_item_exits_2()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
