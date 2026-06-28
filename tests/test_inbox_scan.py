#!/usr/bin/env python3
"""Tests for inbox_scan.py — the unified, non-LLM inbox classifier that routes each marketplace
conversation row to the correct side (buy vs sell) so neither peek fires on the other's rows.

    python3 tests/test_inbox_scan.py

Claims:
  1. is_fresh: a row is fresh only when it is unread AND its snippet changed since the memo.
  2. classify: a fresh row whose handle matches a tracked BUY thread fires buy ONLY (never sell).
  3. classify: a fresh row matching a tracked SELL thread, or an unknown non-system handle (a new
     enquiry), fires sell ONLY.
  4. classify: a fresh row from a known SYSTEM handle (carousell_assistant, promos) fires neither.
  5. classify: a read row (unread False) fires nothing, even if it matches a tracked thread.
  6. classify: the memo suppresses re-firing while the same reply sits unread.
  7. classify: a handle present in BOTH indexes is claimed by BUY (precedence).
  8. classify: next_memo advances for every observed row (fresh or not).
  9. build_buy_index: only liaising/agreed buyer_threads (by seller_handle) are indexed.
 10. build_sell_index: only active sell threads (by buyer_handle) are indexed.
 11. classify: a market that was not scanned (found False) is absent from sell_markets (caller falls
     back to the aggregate probe) and never appears in buy.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import inbox_scan  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _row(handle, snippet, unread):
    return {"handle": handle, "snippet": snippet, "unread": unread}


def _car(rows, found=True):
    return {"carousell": {"found": found, "rows": rows}}


# --------------------------------------------------------------------------- is_fresh

def test_is_fresh():
    print("is_fresh = unread AND snippet changed since memo:")
    memo = {"carousell:maxlinda": {"snippet": "old"}}
    check("unread + changed snippet → fresh",
          inbox_scan.is_fresh("carousell:maxlinda", _row("maxlinda", "new reply", True), memo) is True)
    check("unread + same snippet → not fresh",
          inbox_scan.is_fresh("carousell:maxlinda", _row("maxlinda", "old", True), memo) is False)
    check("read (unread False) → not fresh",
          inbox_scan.is_fresh("carousell:maxlinda", _row("maxlinda", "new reply", False), memo) is False)
    check("unseen key + unread → fresh",
          inbox_scan.is_fresh("carousell:newcomer", _row("newcomer", "hi", True), memo) is True)


# --------------------------------------------------------------------------- classify routing

def test_buy_reply_fires_buy_only():
    print("fresh row matching a BUY thread → buy only, sell not flagged:")
    buy_index = {"maxlinda": {"want_id": "iphone-5s-black", "thread_id": "carousell:1410917548"}}
    out = inbox_scan.classify(_car([_row("maxlinda", "Can do $55", True)]), buy_index, {}, {})
    check("one buy entry", len(out["buy"]) == 1)
    check("buy entry carries want_id", out["buy"] and out["buy"][0]["want_id"] == "iphone-5s-black")
    check("buy entry carries thread_id", out["buy"] and out["buy"][0]["thread_id"] == "carousell:1410917548")
    check("sell NOT flagged for carousell", out["sell_markets"].get("carousell") is False)


def test_sell_tracked_thread_fires_sell_only():
    print("fresh row matching a tracked SELL thread → sell only, buy empty:")
    sell_index = {"truewolf.5feb9c": "carousell:2143175040"}
    out = inbox_scan.classify(_car([_row("truewolf.5feb9c", "can collect today?", True)]), {}, sell_index, {})
    check("sell flagged for carousell", out["sell_markets"].get("carousell") is True)
    check("buy empty", out["buy"] == [])


def test_new_enquiry_fires_sell():
    print("fresh unread unknown non-system handle (new enquiry) → sell:")
    out = inbox_scan.classify(_car([_row("brandnew_buyer", "is this available?", True)]), {}, {}, {})
    check("sell flagged", out["sell_markets"].get("carousell") is True)
    check("buy empty", out["buy"] == [])


def test_system_handle_ignored():
    print("fresh unread SYSTEM handle (promo) → neither buy nor sell:")
    out = inbox_scan.classify(
        _car([_row("carousell_assistant", "Back to School Savings!", True),
              _row("selltocarousell_mobiles", "Trade-In Discount", True)]), {}, {}, {})
    check("sell NOT flagged", out["sell_markets"].get("carousell") is False)
    check("buy empty", out["buy"] == [])


def test_read_row_ignored():
    print("read row (unread False) fires nothing even when it matches a thread:")
    buy_index = {"maxlinda": {"want_id": "w", "thread_id": "carousell:1410917548"}}
    out = inbox_scan.classify(_car([_row("maxlinda", "Can do $55", False)]), buy_index, {}, {})
    check("buy empty", out["buy"] == [])
    check("sell not flagged", out["sell_markets"].get("carousell") is False)


def test_memo_suppresses_refire():
    print("memo suppresses re-firing while the same reply sits unread:")
    buy_index = {"maxlinda": {"want_id": "w", "thread_id": "carousell:1410917548"}}
    memo = {"carousell:maxlinda": {"snippet": "Can do $55", "unread": True}}
    out = inbox_scan.classify(_car([_row("maxlinda", "Can do $55", True)]), buy_index, {}, memo)
    check("buy empty (already seen)", out["buy"] == [])


def test_buy_precedence_over_sell():
    print("handle present in BOTH indexes → claimed by buy:")
    buy_index = {"dual": {"want_id": "w", "thread_id": "carousell:BUY"}}
    sell_index = {"dual": "carousell:SELL"}
    out = inbox_scan.classify(_car([_row("dual", "hello", True)]), buy_index, sell_index, {})
    check("buy claims it", len(out["buy"]) == 1 and out["buy"][0]["thread_id"] == "carousell:BUY")
    check("sell not flagged", out["sell_markets"].get("carousell") is False)


def test_next_memo_advances_for_all_rows():
    print("next_memo advances for every observed row (fresh or not):")
    out = inbox_scan.classify(
        _car([_row("maxlinda", "fresh", True), _row("someoneelse", "stale", False)]), {}, {}, {})
    check("memo has fresh row", out["next_memo"].get("carousell:maxlinda", {}).get("snippet") == "fresh")
    check("memo has read row too", out["next_memo"].get("carousell:someoneelse", {}).get("snippet") == "stale")


def test_unscanned_market_absent():
    print("a market not scanned (found False) is absent from sell_markets and buy:")
    out = inbox_scan.classify({"carousell": {"found": False, "rows": []}},
                              {"maxlinda": {"want_id": "w", "thread_id": "t"}}, {}, {})
    check("carousell absent from sell_markets", "carousell" not in out["sell_markets"])
    check("buy empty", out["buy"] == [])


# --------------------------------------------------------------------------- index builders

def _write(d: Path, name: str, obj: dict):
    (d / name).write_text(json.dumps(obj))


def test_build_buy_index_only_liaise():
    print("build_buy_index indexes only liaising/agreed threads by seller_handle:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write(d, "carousell:1.json", {"thread_id": "carousell:1", "want_id": "w1",
                                        "seller_handle": "MaxLinda", "status": "liaising"})
        _write(d, "carousell:2.json", {"thread_id": "carousell:2", "want_id": "w1",
                                        "seller_handle": "thevibe_", "status": "agreed"})
        _write(d, "carousell:3.json", {"thread_id": "carousell:3", "want_id": "w1",
                                        "seller_handle": "vyywl", "status": "closed"})
        idx = inbox_scan.build_buy_index(d)
        check("liaising indexed (normalized handle)", idx.get("maxlinda", {}).get("thread_id") == "carousell:1")
        check("agreed indexed", "thevibe_" in idx)
        check("closed NOT indexed", "vyywl" not in idx)


def test_build_sell_index_only_active():
    print("build_sell_index indexes only active threads by buyer_handle:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write(d, "carousell:10.json", {"thread_id": "carousell:10", "buyer_handle": "Truewolf.5feb9c",
                                        "status": "active"})
        _write(d, "carousell:11.json", {"thread_id": "carousell:11", "buyer_handle": "wuzen22",
                                        "status": "sold"})
        idx = inbox_scan.build_sell_index(d)
        check("active indexed (normalized)", idx.get("truewolf.5feb9c") == "carousell:10")
        check("sold NOT indexed", "wuzen22" not in idx)


if __name__ == "__main__":
    print("inbox_scan tests\n")
    test_is_fresh()
    test_buy_reply_fires_buy_only()
    test_sell_tracked_thread_fires_sell_only()
    test_new_enquiry_fires_sell()
    test_system_handle_ignored()
    test_read_row_ignored()
    test_memo_suppresses_refire()
    test_buy_precedence_over_sell()
    test_next_memo_advances_for_all_rows()
    test_unscanned_market_absent()
    test_build_buy_index_only_liaise()
    test_build_sell_index_only_active()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
