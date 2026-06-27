#!/usr/bin/env python3
"""Tests for buyer_recheck.py — the deterministic, ~0-token re-probe that gates the forced sweep.

    python3 tests/test_buyer_recheck.py

Claims:
  1. all inboxes clear (count 0) → unhandled 0 (the forced LLM pass is safely skipped).
  2. any market with unread → flagged unhandled (+ a latest_text hint for the pass).
  3. an UNREADABLE market (probe found:false) → conservatively counted as unhandled (a safety-net
     probe must never fail open to "clear").
  4. recheck NEVER advances the buyer_peek memo or any cursor (strictly read-only).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import buyer_recheck  # noqa: E402
import buyer_peek  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _patch(enabled, probe_results, on_save=None):
    """Swap buyer_peek seams for the duration of one recheck(); return a restore() callable."""
    saved = (buyer_peek.list_page_targets, buyer_peek.enabled_markets,
             buyer_peek.probe_market, buyer_peek.save_memo)
    buyer_peek.list_page_targets = lambda *a, **k: []
    buyer_peek.enabled_markets = lambda: list(enabled)
    buyer_peek.probe_market = lambda market, probe, targets: probe_results[market]
    buyer_peek.save_memo = on_save or (lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("recheck must never write the buyer_peek memo")))

    def restore():
        (buyer_peek.list_page_targets, buyer_peek.enabled_markets,
         buyer_peek.probe_market, buyer_peek.save_memo) = saved
    return restore


def test_clear_inboxes_zero_unhandled():
    print("all inboxes clear → unhandled 0 (forced pass safely skipped):")
    restore = _patch(["fb", "carousell"],
                     {"fb": {"found": True, "count": 0, "snippet": ""},
                      "carousell": {"found": True, "count": 0, "snippet": ""}})
    try:
        out = buyer_recheck.recheck()
        check("unhandled == 0", out["unhandled"] == 0)
        check("no market flagged", all(not m["unhandled"] for m in out["markets"].values()))
        check("no latest_text", out["latest_text"] == "")
    finally:
        restore()


def test_unread_market_flagged():
    print("a market with unread → flagged unhandled + latest_text hint:")
    restore = _patch(["fb", "carousell"],
                     {"fb": {"found": True, "count": 0, "snippet": ""},
                      "carousell": {"found": True, "count": 2, "snippet": "still available?"}})
    try:
        out = buyer_recheck.recheck()
        check("unhandled == 1", out["unhandled"] == 1)
        check("carousell flagged", out["markets"]["carousell"]["unhandled"] is True)
        check("fb not flagged", out["markets"]["fb"]["unhandled"] is False)
        check("latest_text points at carousell", "carousell" in out["latest_text"])
    finally:
        restore()


def test_unreadable_market_conservative():
    print("an unreadable market → conservatively counted as unhandled (never fail-open to clear):")
    restore = _patch(["carousell"], {"carousell": {"found": False, "count": 0, "snippet": ""}})
    try:
        out = buyer_recheck.recheck()
        check("unhandled == 1 (conservative)", out["unhandled"] == 1)
        check("market marked unknown", out["markets"]["carousell"]["unknown"] is True)
        check("market marked unhandled", out["markets"]["carousell"]["unhandled"] is True)
    finally:
        restore()


def test_readonly_no_memo_write():
    print("recheck is strictly read-only (asserts save_memo is never called):")
    # _patch installs a save_memo that raises if called; a clean recheck must not trip it.
    restore = _patch(["fb"], {"fb": {"found": True, "count": 1, "snippet": "hi"}})
    raised = False
    try:
        buyer_recheck.recheck()
    except AssertionError:
        raised = True
    finally:
        restore()
    check("did not write the memo", raised is False)


if __name__ == "__main__":
    print("buyer_recheck tests\n")
    test_clear_inboxes_zero_unhandled()
    test_unread_market_flagged()
    test_unreadable_market_conservative()
    test_readonly_no_memo_write()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
