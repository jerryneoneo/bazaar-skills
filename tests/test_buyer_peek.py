#!/usr/bin/env python3
"""Tests for buyer_peek.peek()'s PRECISE-signal integration (the highest-value path in the
cost-reduction change): for enumerable markets, `new` comes from inbox_scan.sell_markets_new()
(promos + buy-thread rows excluded), with a fail-open fallback to the aggregate is_new().

    python3 tests/test_buyer_peek.py

Claims:
  1. precise says clear → carousell new is False even when the aggregate count is high (promo unread
     no longer fires a sell pass).
  2. precise says actionable → carousell new is True (a real buyer is never suppressed).
  3. precise raises → fall back to the aggregate is_new() (never less sensitive than before).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import buyer_peek  # noqa: E402
import inbox_scan  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _patch(enabled, probe_results, precise, memo=None):
    """Swap buyer_peek + inbox_scan seams for one peek(); return restore()."""
    saved = (buyer_peek.list_page_targets, buyer_peek.enabled_markets, buyer_peek.probe_market,
             buyer_peek.load_memo, buyer_peek.save_memo, inbox_scan.sell_markets_new)
    buyer_peek.list_page_targets = lambda *a, **k: []
    buyer_peek.enabled_markets = lambda: list(enabled)
    buyer_peek.probe_market = lambda market, probe, targets: probe_results[market]
    buyer_peek.load_memo = lambda: dict(memo or {})
    buyer_peek.save_memo = lambda *a, **k: None
    inbox_scan.sell_markets_new = (precise if callable(precise) else (lambda: dict(precise)))

    def restore():
        (buyer_peek.list_page_targets, buyer_peek.enabled_markets, buyer_peek.probe_market,
         buyer_peek.load_memo, buyer_peek.save_memo, inbox_scan.sell_markets_new) = saved
    return restore


def test_precise_clear_suppresses_promo():
    print("precise clear → carousell new False despite high aggregate count (promo suppressed):")
    restore = _patch(["carousell"], {"carousell": {"found": True, "count": 9, "snippet": "promo"}},
                     {"carousell": False})
    try:
        out = buyer_peek.peek(update_memo=False)
        check("carousell new is False", out["markets"]["carousell"]["new"] is False)
        check("pending 0 (no sell pass)", out["pending"] == 0)
    finally:
        restore()


def test_precise_actionable_fires():
    print("precise actionable → carousell new True (real buyer never suppressed):")
    restore = _patch(["carousell"], {"carousell": {"found": True, "count": 1, "snippet": "is this avail?"}},
                     {"carousell": True})
    try:
        out = buyer_peek.peek(update_memo=False)
        check("carousell new is True", out["markets"]["carousell"]["new"] is True)
        check("pending 1", out["pending"] == 1)
    finally:
        restore()


def test_precise_exception_falls_back_to_aggregate():
    print("precise raises → fall back to aggregate is_new (never less sensitive):")
    def _boom():
        raise RuntimeError("scan down")
    restore = _patch(["carousell"], {"carousell": {"found": True, "count": 2, "snippet": "buyer msg"}},
                     _boom, memo={})  # empty memo → aggregate is_new True (count 2 > 0, snippet changed)
    try:
        out = buyer_peek.peek(update_memo=False)
        check("carousell new is True via aggregate fallback", out["markets"]["carousell"]["new"] is True)
    finally:
        restore()


if __name__ == "__main__":
    print("buyer_peek precise-signal tests\n")
    test_precise_clear_suppresses_promo()
    test_precise_actionable_fires()
    test_precise_exception_falls_back_to_aggregate()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
