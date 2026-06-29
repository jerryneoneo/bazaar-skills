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
import inbox_scan  # noqa: E402  precise per-thread signal that overrides the count for enumerable markets

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


def _patch_precise(signal):
    """Override the precise per-thread signal (inbox_scan.sell_actionable_now). Returns restore()."""
    saved = inbox_scan.sell_actionable_now
    inbox_scan.sell_actionable_now = lambda: dict(signal)
    return lambda: setattr(inbox_scan, "sell_actionable_now", saved)


def test_precise_excludes_promos():
    print("precise signal: enumerable market has count>0 (promos) but precise says clear → unhandled 0:")
    restore = _patch(["carousell"], {"carousell": {"found": True, "count": 9, "snippet": "promo"}})
    unpatch = _patch_precise({"carousell": False})
    try:
        out = buyer_recheck.recheck()
        check("carousell unhandled False despite count 9", out["markets"]["carousell"]["unhandled"] is False)
        check("total unhandled 0 (forced sweep skipped)", out["unhandled"] == 0)
    finally:
        unpatch(); restore()


def test_precise_flags_real_activity():
    print("precise signal: enumerable market precise True → still unhandled (real buyer not missed):")
    restore = _patch(["carousell"], {"carousell": {"found": True, "count": 1, "snippet": "real buyer"}})
    unpatch = _patch_precise({"carousell": True})
    try:
        out = buyer_recheck.recheck()
        check("carousell unhandled True", out["markets"]["carousell"]["unhandled"] is True)
        check("total unhandled 1", out["unhandled"] == 1)
    finally:
        unpatch(); restore()


def test_market_absent_from_precise_uses_count():
    print("market absent from precise (scan down) → conservative count-based rule kept:")
    restore = _patch(["fb"], {"fb": {"found": True, "count": 3, "snippet": "x"}})
    unpatch = _patch_precise({})  # nothing precise → fall back to count
    try:
        out = buyer_recheck.recheck()
        check("fb flagged unhandled by count", out["markets"]["fb"]["unhandled"] is True)
    finally:
        unpatch(); restore()


def test_fb_precise_drives_unhandled_true():
    print("FB precise True → fb unhandled True even when the aggregate count is FLAT (the Olaf miss):")
    restore = _patch(["fb"], {"fb": {"found": True, "count": 0, "snippet": ""}})
    unpatch = _patch_precise({"fb": True})  # precise caught a known-thread reply the badge missed
    try:
        out = buyer_recheck.recheck()
        check("fb unhandled True (precise over flat count)", out["markets"]["fb"]["unhandled"] is True)
        check("total unhandled 1", out["unhandled"] == 1)
    finally:
        unpatch(); restore()


def test_fb_precise_drives_unhandled_false():
    print("FB precise False → fb unhandled False despite a non-zero aggregate count (noise excluded):")
    restore = _patch(["fb"], {"fb": {"found": True, "count": 7, "snippet": "noise"}})
    unpatch = _patch_precise({"fb": False})
    try:
        out = buyer_recheck.recheck()
        check("fb unhandled False despite count 7", out["markets"]["fb"]["unhandled"] is False)
        check("total unhandled 0 (forced sweep skipped)", out["unhandled"] == 0)
    finally:
        unpatch(); restore()


def test_fb_scan_failure_keeps_count_fallback():
    print("inbox_scan raises → FB drops out of precise, conservative count>0 fallback kept:")
    restore = _patch(["fb"], {"fb": {"found": True, "count": 4, "snippet": "x"}})
    saved = inbox_scan.sell_actionable_now
    inbox_scan.sell_actionable_now = lambda: (_ for _ in ()).throw(RuntimeError("scan down"))
    try:
        out = buyer_recheck.recheck()
        check("fb flagged unhandled by count fallback", out["markets"]["fb"]["unhandled"] is True)
    finally:
        inbox_scan.sell_actionable_now = saved
        restore()


def test_carousell_non_inbox_tab_flagged_by_count():
    print("carousell tab open on a non-inbox page → relaxed count reads the global badge, precise"
          " abstains → still flagged unhandled (no false 'inbox unreadable'):")
    # The relaxed probe now finds the off-inbox carousell tab and reads the global unread badge
    # (count=2); the precise classifier abstains off /inbox (carousell absent from the signal), so the
    # conservative count fallback flags it. Previously the probe found no tab (found:False) and the
    # pass escalated "tab not open" instead of just reading the count / navigating.
    restore = _patch(["carousell"], {"carousell": {"found": True, "count": 2, "snippet": "still avail?"}})
    unpatch = _patch_precise({})  # carousell abstained off /inbox → absent from precise
    try:
        out = buyer_recheck.recheck()
        check("carousell not 'unknown' (the tab WAS found via the alt)",
              out["markets"]["carousell"]["unknown"] is False)
        check("carousell flagged unhandled by the global count", out["markets"]["carousell"]["unhandled"] is True)
        check("total unhandled 1", out["unhandled"] == 1)
    finally:
        unpatch(); restore()


if __name__ == "__main__":
    print("buyer_recheck tests\n")
    test_carousell_non_inbox_tab_flagged_by_count()
    test_clear_inboxes_zero_unhandled()
    test_unread_market_flagged()
    test_unreadable_market_conservative()
    test_readonly_no_memo_write()
    test_precise_excludes_promos()
    test_precise_flags_real_activity()
    test_market_absent_from_precise_uses_count()
    test_fb_precise_drives_unhandled_true()
    test_fb_precise_drives_unhandled_false()
    test_fb_scan_failure_keeps_count_fallback()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
