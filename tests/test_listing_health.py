#!/usr/bin/env python3
"""Tests for listing_health.py — stale LIVE-listing detection + dedup ledger.

    python3 tests/test_listing_health.py

Focus: the deterministic DETECT core. The clock is the last buyer inbound (anchor fallback only when
a buyer never wrote); eligibility excludes draft / live-but-unpublished (owned by triage); the warn
ledger dedups the proactive ping. Suggestion composition is the MAINT LLM pass's job, not tested
here. State isolated per test via tmp dirs / SELLY_DATA_DIR.
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

import listing_health as lh  # noqa: E402

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def _in(ts):
    return {"msg_id": f"i{ts}", "dir": "in", "text": "interested", "ts": ts}


def _out(ts):
    return {"msg_id": f"o{ts}", "dir": "out", "text": "hi", "ts": ts}


def _item(item_id="widget", status="live", urls=None, price=20, photos=2, **extra):
    rec = {"item_id": item_id, "title": item_id.title(), "status": status,
           "list_price": price, "currency": "SGD",
           "listing_urls": urls if urls is not None else {"fb": "u", "carousell": "u"},
           "photos": [f"p{i}.jpg" for i in range(photos)]}
    rec.update(extra)
    return rec


def _thread(item_id, rows):
    return {"thread_id": f"fb:{item_id}", "item_id": item_id, "transcript": rows}


def _write_json(tmp, rel, payload):
    p = Path(tmp) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))
    return p


# ---- pure: last_inbound_ts / item_inbound_ts -------------------------------

def test_inbound_ts():
    print("last/item inbound ts: latest dir==in, ignores cursor + outbound:")
    t = _thread("w", [_out(_ago(5)), _in(_ago(4)), _in(_ago(2)), _out(_ago(1))])
    check("last_inbound = newest inbound", lh.last_inbound_ts(t) == lh._safe_iso(_ago(2)))
    check("no inbound -> None", lh.last_inbound_ts(_thread("w", [_out(_ago(1))])) is None)
    threads = [_thread("w", [_in(_ago(9))]), _thread("w", [_in(_ago(3))]), _thread("x", [_in(_ago(1))])]
    check("item_inbound = max across w threads", lh.item_inbound_ts("w", threads) == lh._safe_iso(_ago(3)))
    check("item with no threads -> None", lh.item_inbound_ts("z", threads) is None)


# ---- pure: published_anchor ------------------------------------------------

def test_published_anchor():
    print("published_anchor: fallback chain precedence:")
    check("published_at wins",
          lh.published_anchor(_item(published_at=_ago(10), imported_at=_ago(30)), None, NOW)
          == lh._safe_iso(_ago(10)))
    check("falls to imported_at",
          lh.published_anchor({"imported_at": "2026-06-20"}, None, NOW) == lh._safe_iso("2026-06-20"))
    check("falls to distribution_offered_at",
          lh.published_anchor({"distribution_offered_at": _ago(8)}, None, NOW) == lh._safe_iso(_ago(8)))
    check("last resort now (no anchors, no path)", lh.published_anchor({}, None, NOW) == NOW)


# ---- pure: staleness -------------------------------------------------------

def test_staleness():
    print("staleness: clock = inbound when present else anchor; boundary; disabled:")
    item = _item()
    fresh = lh.staleness(item, lh._safe_iso(_ago(3)), NOW, 7, NOW)
    check("recent inbound -> not stale", fresh["stale"] is False and fresh["basis"] == "since_inbound")
    old = lh.staleness(item, lh._safe_iso(_ago(9)), NOW, 7, NOW)
    check("old inbound -> stale", old["stale"] is True)
    never = lh.staleness(item, None, lh._safe_iso(_ago(10)), 7, NOW)
    check("never inbound, old anchor -> stale (no_inbound basis)",
          never["stale"] is True and never["basis"] == "no_inbound")
    boundary = lh.staleness(item, lh._safe_iso(_ago(7)), NOW, 7, NOW)
    check("exactly stale_days -> stale", boundary["stale"] is True)
    disabled = lh.staleness(item, lh._safe_iso(_ago(99)), NOW, 0, NOW)
    check("stale_days<=0 -> never stale", disabled["stale"] is False)


# ---- pure: stale_listings (eligibility + ordering) -------------------------

def test_stale_listings_eligibility():
    print("stale_listings: only live+published+silent; draft/unpublished excluded; sorted:")
    items = [
        _item("silent", published_at=_ago(30)),                 # live, published, never inbound -> stale
        _item("hot"),                                           # live, recent inbound -> not stale
        _item("draft", status="draft", urls={}),               # draft -> excluded
        _item("unpub", urls={}),                                # live but no urls -> excluded
        _item("veryold", published_at=_ago(40)),               # stale, more overdue
    ]
    threads = [_thread("hot", [_in(_ago(1))])]
    rows = lh.stale_listings(items, threads, None, 7, NOW)
    ids = [r["item_id"] for r in rows]
    check("silent + veryold flagged", set(ids) == {"silent", "veryold"})
    check("hot not flagged", "hot" not in ids)
    check("draft excluded", "draft" not in ids)
    check("unpublished-live excluded", "unpub" not in ids)
    check("sorted most-overdue first", ids[0] == "veryold")


# ---- pure: ledger transforms -----------------------------------------------

def test_ledger_transforms():
    print("needs_warn / reset_on_engagement / mark_warned:")
    row = {"item_id": "w", "last_inbound_ts": None, "list_price": 20, "photo_count": 2}
    check("never warned -> needs warn", lh.needs_warn("w", row, {}, 14, NOW) is True)
    led = lh.mark_warned({}, row, NOW)
    check("mark_warned stamps item", "w" in lh._ledger_items(led))
    check("within rewarn window -> no warn", lh.needs_warn("w", row, led, 14, NOW) is False)
    later = NOW + timedelta(days=15)
    check("past rewarn window -> warn", lh.needs_warn("w", row, led, 14, later) is True)
    # re-engagement after warn (warned_engagement_ts None) -> drop entry
    led2 = lh.reset_on_engagement(led, "w", _ago(1))
    check("re-engagement drops entry", "w" not in lh._ledger_items(led2))
    check("reset immutable (input kept)", "w" in lh._ledger_items(led))


# ---- pure: pick_due --------------------------------------------------------

def test_pick_due():
    print("pick_due: most-overdue unwarned item; disabled/interval gates:")
    items = [_item("a", published_at=_ago(30)), _item("b", published_at=_ago(40))]
    threads = []
    pick = lh.pick_due(items, threads, None, {}, {"listing_health_enabled": True}, NOW)
    check("picks most-overdue (b)", pick and pick["item_id"] == "b")
    check("disabled -> None",
          lh.pick_due(items, threads, None, {}, {"listing_health_enabled": False}, NOW) is None)
    recent_pick = {"last_picked_at": _ago(0.1), "items": {}}
    check("interval rate-limit -> None",
          lh.pick_due(items, threads, None, recent_pick, {"listing_health_enabled": True}, NOW) is None)
    all_warned = {"items": {"a": {"warned_at": _ago(1)}, "b": {"warned_at": _ago(1)}}}
    check("all warned within window -> None",
          lh.pick_due(items, threads, None, all_warned, {"listing_health_enabled": True}, NOW) is None)


# ---- config parsing --------------------------------------------------------

def test_config_parsing():
    print("config parsing: defaults + rejects bad:")
    check("stale_days default", lh._stale_days_from_config({}) == 7.0)
    check("stale_days explicit", lh._stale_days_from_config({"stale_days": 3}) == 3.0)
    check("enabled default true", lh._enabled_from_config({}) is True)
    raised = False
    try:
        lh._stale_days_from_config({"stale_days": "soon"})
    except ValueError:
        raised = True
    check("non-numeric stale_days rejected", raised)


# ---- IO: run_due / run_start / run_mark ------------------------------------

def test_run_due_and_mark_suppresses():
    print("run_due picks a stale item; run_mark then suppresses it within rewarn window:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"listing_health_enabled": True, "stale_days": 7})
        _write_json(tmp, "items/silent.json", _item("silent", published_at=_ago(30)))
        due = lh.run_due(NOW, base=base)
        check("due_item is the stale one", due["due_item"] == "silent")
        check("stale_count 1", due["stale_count"] == 1)
        lh.run_mark("silent", NOW, base=base)
        due2 = lh.run_due(NOW, base=base)
        check("warned item suppressed -> no due_item", due2["due_item"] is None)
        check("but still shown in list (state of world)", lh.run_list(NOW, base=base)["count"] == 1)


def test_run_start_writes_session():
    print("run_start writes the MAINT session baton + stamps last_picked_at:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"listing_health_enabled": True})
        _write_json(tmp, "items/silent.json", _item("silent", published_at=_ago(30)))
        sess = lh.run_start("silent", NOW, base=base)
        check("session active", sess["active"] is True and sess["item_id"] == "silent")
        on_disk = json.loads((base / "listing_health_session.json").read_text())
        check("session persisted", on_disk["item_id"] == "silent")
        ledger = json.loads((base / "listing_health_state.json").read_text())
        check("last_picked_at stamped", ledger.get("last_picked_at") is not None)
        # interval rate-limit now blocks a fresh pick
        due = lh.run_due(NOW, base=base)
        check("rate-limited after start", due["due_item"] is None)


# ---- CLI smoke -------------------------------------------------------------

def test_cli():
    print("CLI: due/list/mark exit codes + JSON via SELLY_DATA_DIR:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"listing_health_enabled": True})
        _write_json(tmp, "items/silent.json", _item("silent", published_at=_ago(30)))
        env = {**os.environ, "SELLY_DATA_DIR": tmp}
        exe = [sys.executable, str(ROOT / "bin" / "listing_health.py")]
        p = subprocess.run(exe + ["due", "--now", NOW.isoformat()], capture_output=True, text=True, env=env)
        check("due exit 0", p.returncode == 0)
        check("due JSON names item", json.loads(p.stdout)["due_item"] == "silent")
        bad = subprocess.run(exe + ["mark"], capture_output=True, text=True, env=env)
        check("mark without --item -> exit 2", bad.returncode == 2)
        missing = subprocess.run(exe + ["mark", "--item", "ghost"], capture_output=True, text=True, env=env)
        check("mark unknown item -> exit 3", missing.returncode == 3)


if __name__ == "__main__":
    print("listing_health.py tests\n")
    test_inbound_ts()
    test_published_anchor()
    test_staleness()
    test_stale_listings_eligibility()
    test_ledger_transforms()
    test_pick_due()
    test_config_parsing()
    test_run_due_and_mark_suppresses()
    test_run_start_writes_session()
    test_cli()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
