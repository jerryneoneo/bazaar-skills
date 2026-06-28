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
  4. FB now flows through the PRECISE path: precise {'fb': True} → fb new True regardless of the flat
     aggregate count (the Olaf miss — a known-thread reply that didn't bump the aggregate badge).
  5. precise {'fb': False} → fb new False (a noise-only FB inbox no longer fires a sell pass).
  6. precise raises → FB falls back to the aggregate is_new() (never less sensitive than before).
"""

import re
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


def _patch(enabled, probe_results, precise, memo=None, sell_threads=None):
    """Swap buyer_peek + inbox_scan seams for one peek(); return restore().

    `precise` drives sell_markets (the bool gate); `sell_threads` (optional) drives the per-market
    thread-id hint. Both now ride a SINGLE inbox_scan.sell_peek() (C-followup — one memo advance)."""
    saved = (buyer_peek.list_page_targets, buyer_peek.enabled_markets, buyer_peek.probe_market,
             buyer_peek.load_memo, buyer_peek.save_memo, inbox_scan.sell_peek)
    buyer_peek.list_page_targets = lambda *a, **k: []
    buyer_peek.enabled_markets = lambda: list(enabled)
    buyer_peek.probe_market = lambda market, probe, targets: probe_results[market]
    buyer_peek.load_memo = lambda: dict(memo or {})
    buyer_peek.save_memo = lambda *a, **k: None

    def _sell_peek():
        markets = precise() if callable(precise) else dict(precise)
        return {"sell_markets": markets, "sell_threads": dict(sell_threads or {})}
    inbox_scan.sell_peek = _sell_peek

    def restore():
        (buyer_peek.list_page_targets, buyer_peek.enabled_markets, buyer_peek.probe_market,
         buyer_peek.load_memo, buyer_peek.save_memo, inbox_scan.sell_peek) = saved
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


def test_fb_precise_actionable_fires_despite_flat_count():
    print("FB precise True → fb new True even when the aggregate count is FLAT (the Olaf miss):")
    # memo already at count 0; aggregate count stays 0 (the badge didn't bump) → is_new would be
    # False, but the precise per-thread scan caught the known-thread reply.
    restore = _patch(["fb"], {"fb": {"found": True, "count": 0, "snippet": ""}},
                     {"fb": True}, memo={"fb": {"count": 0, "snippet": ""}})
    try:
        out = buyer_peek.peek(update_memo=False)
        check("fb new is True (precise wins over flat aggregate)", out["markets"]["fb"]["new"] is True)
        check("pending 1", out["pending"] == 1)
    finally:
        restore()


def test_fb_precise_clear_suppresses():
    print("FB precise False → fb new False even with a non-zero aggregate count (noise suppressed):")
    restore = _patch(["fb"], {"fb": {"found": True, "count": 5, "snippet": "noise"}},
                     {"fb": False})
    try:
        out = buyer_peek.peek(update_memo=False)
        check("fb new is False", out["markets"]["fb"]["new"] is False)
        check("pending 0", out["pending"] == 0)
    finally:
        restore()


def test_fb_precise_exception_falls_back_to_aggregate():
    print("FB precise raises → fb falls back to aggregate is_new (never less sensitive):")
    def _boom():
        raise RuntimeError("scan down")
    restore = _patch(["fb"], {"fb": {"found": True, "count": 2, "snippet": "buyer msg"}},
                     _boom, memo={})  # empty memo → aggregate is_new True (count 2 > 0)
    try:
        out = buyer_peek.peek(update_memo=False)
        check("fb new is True via aggregate fallback", out["markets"]["fb"]["new"] is True)
    finally:
        restore()


def test_peek_carries_sell_threads_from_single_sell_peek():
    print("C-followup: peek() surfaces per-market sell_threads AND advances the SELL memo ONCE"
          " (one inbox_scan.sell_peek, not sell_markets_new + sell_threads_new):")
    saved = inbox_scan.sell_peek
    calls = []
    saved_probe = buyer_peek.probe_market
    saved_targets = buyer_peek.list_page_targets
    saved_enabled = buyer_peek.enabled_markets
    saved_save = buyer_peek.save_memo
    saved_load = buyer_peek.load_memo
    try:
        buyer_peek.list_page_targets = lambda *a, **k: []
        buyer_peek.enabled_markets = lambda: ["fb"]
        buyer_peek.probe_market = lambda *a, **k: {"found": True, "count": 0, "snippet": ""}
        buyer_peek.load_memo = lambda: {}
        buyer_peek.save_memo = lambda *a, **k: None

        def _one():
            calls.append(1)
            return {"sell_markets": {"fb": True}, "sell_threads": {"fb": ["fb:9988"]}}
        inbox_scan.sell_peek = _one
        out = buyer_peek.peek(update_memo=False)
        check("sell_peek called exactly once (no double memo advance)", len(calls) == 1)
        check("fb new is True", out["markets"]["fb"]["new"] is True)
        check("per-market sell_threads surfaced in the contract",
              out["markets"]["fb"].get("sell_threads") == ["fb:9988"])
    finally:
        inbox_scan.sell_peek = saved
        buyer_peek.probe_market = saved_probe
        buyer_peek.list_page_targets = saved_targets
        buyer_peek.enabled_markets = saved_enabled
        buyer_peek.save_memo = saved_save
        buyer_peek.load_memo = saved_load


# --------------------------------------------------------------------------- B2: the FB probe JS
# noise regex must NOT over-match real buyer previews (the buyer_peek copy of the inbox_scan fix)

def _fb_probe_noise_regex():
    """Extract the NOISE regex literal embedded in the FB probe JS and port it to a Python regex,
    so this test guards the in-page copy against drift back to the over-matching pattern."""
    js = buyer_peek.MARKET_PROBES["fb"]["js"]
    m = re.search(r"const NOISE = /(.*?)/i;", js)
    assert m, "could not locate the NOISE regex literal in the FB probe JS"
    return re.compile(m.group(1), re.IGNORECASE)


def test_fb_probe_noise_regex_keeps_real_previews():
    print("B2 (buyer_peek copy): the FB probe NOISE regex does NOT swallow real buyer previews:")
    noise = _fb_probe_noise_regex()
    for text in ["Olaf · Glass Kettle Within 5m can you meet?",
                 "Jane · Sofa within 2m now",
                 "Marketplace listing - 2 new messages from buyer · Sofa shall we meet?",
                 "Bob · Item 5m left ok?"]:
        check(f"real preview NOT noise: {text[:42]!r}", noise.search(text) is None)


def test_fb_probe_noise_regex_still_drops_noise():
    print("B2 (buyer_peek copy): the FB probe NOISE regex still excludes the genuine noise rows:")
    noise = _fb_probe_noise_regex()
    for text in ["Number of unread notifications20+",
                 "Marketplace 3 new messages",
                 "Singapore · Within 1 kilometer",
                 "Singapore · Within 5 km"]:
        check(f"genuine noise IS matched: {text[:42]!r}", noise.search(text) is not None)


def test_fb_probe_noise_regex_matches_python_copy():
    print("B2: the buyer_peek FB probe NOISE regex and inbox_scan.FB_NOISE_RE agree on every case"
          " (the three copies must stay consistent):")
    noise = _fb_probe_noise_regex()
    cases = ["Olaf · Glass Kettle Within 5m can you meet?", "Jane · Sofa within 2m now",
             "Bob · Item 5m left ok?", "Number of unread notifications20+",
             "Marketplace 3 new messages", "Singapore · Within 1 kilometer",
             "Singapore · Within 5 km", "Toa Payoh · Within 500 meters"]
    for text in cases:
        check(f"both copies agree on: {text[:38]!r}",
              bool(noise.search(text)) == bool(inbox_scan.FB_NOISE_RE.search(text)))


if __name__ == "__main__":
    print("buyer_peek precise-signal tests\n")
    test_fb_probe_noise_regex_keeps_real_previews()
    test_fb_probe_noise_regex_still_drops_noise()
    test_fb_probe_noise_regex_matches_python_copy()
    test_precise_clear_suppresses_promo()
    test_precise_actionable_fires()
    test_precise_exception_falls_back_to_aggregate()
    test_fb_precise_actionable_fires_despite_flat_count()
    test_fb_precise_clear_suppresses()
    test_fb_precise_exception_falls_back_to_aggregate()
    test_peek_carries_sell_threads_from_single_sell_peek()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
