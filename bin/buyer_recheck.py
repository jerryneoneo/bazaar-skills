#!/usr/bin/env python3
"""buyer_recheck.py — deterministic, ~0-token re-probe that gates the FORCED buyer sweep.

The daemon's safety-net FORCED buyer pass used to fire a full LLM browser pass just to CONFIRM
nothing is unhandled. This re-probe makes that confirmation ~0 tokens: it reads the CURRENT absolute
unread count per enabled market straight from the warm CDP Chrome (reusing buyer_peek's probes +
transport) and reports which markets still have unread mail. The forced branch then fires the LLM
pass ONLY for markets that actually have something, and skips entirely when every inbox is clear
(the common idle case).

Difference from buyer_peek: buyer_peek gates on a count RISE / snippet CHANGE versus a per-market
memo, so it does NOT re-fire while a reply is still pending. buyer_recheck deliberately IGNORES that
memo and reports the ABSOLUTE count, so a STRANDED thread (buyer_peek's memo already advanced, but
the LLM pass never actually replied) is still caught and re-fired. It never reads or writes that
memo, and NEVER advances a thread cursor (the buyer pass owns cursors; this is strictly read-only).

FAIL-OPEN-SAFE, but CONSERVATIVELY (the opposite of buyer_peek): because this gates a SAFETY NET, a
market it cannot read (CDP/DOM/socket error, tab missing) is reported `unknown:true` and COUNTED as
unhandled, so the caller falls back to the conservative forced LLM pass rather than risk skipping a
real buyer. A probe that fails open to "clear" would defeat the whole point of the safety net.

Scope note: this is a count-based gate (works uniformly for fb/carousell/ebay via the existing
MARKET_PROBES counts). A per-thread cursor diff (to suppress a persistent-unread thread the agent
intentionally ignores) is a later refinement (see the Tier 3 READ); it is not needed here because a
false positive only costs one idempotent LLM pass that finds nothing, while a false negative could
strand a buyer.

Contract (stdout, exit 0):
    {"unhandled": int,                      # number of markets with unread (or unreadable)
     "markets": {<id>: {"count": int, "unknown": bool, "snippet": str, "unhandled": bool}},
     "latest_text": str}                    # a hint for the LLM pass (first unhandled market)

Usage:
    buyer_recheck.py            # probe enabled markets, print the recheck JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buyer_peek  # noqa: E402  reuse CDP transport (_MiniWS/cdp_eval), MARKET_PROBES, probe_market


def recheck() -> dict:
    """Read the absolute unread state of every enabled market and report which are unhandled.

    Pure read: no memo, no cursor writes. Fail-open CONSERVATIVE per market (unreadable → unhandled).
    """
    targets = buyer_peek.list_page_targets()
    markets_out: dict[str, dict] = {}
    unhandled = 0
    latest_text = ""

    for market in buyer_peek.enabled_markets():
        cur = buyer_peek.probe_market(market, buyer_peek.MARKET_PROBES[market], targets)
        unknown = not cur.get("found", False)
        count = int(cur.get("count") or 0)
        is_unhandled = unknown or count > 0
        markets_out[market] = {"count": count, "unknown": unknown,
                               "snippet": cur.get("snippet", ""), "unhandled": is_unhandled}
        if is_unhandled:
            unhandled += 1
            if not latest_text:
                snip = cur.get("snippet") or ("unreadable inbox" if unknown else "unread")
                latest_text = f"[{market}] {snip}".strip()

    return {"unhandled": unhandled, "markets": markets_out, "latest_text": latest_text}


def main(argv: list[str]) -> int:
    try:
        print(json.dumps(recheck()))
    except Exception as exc:  # last-resort fail-open: never crash the daemon
        # Conservative: a recheck that cannot run at all must NOT report "clear" (that would let a
        # forced sweep skip a possible buyer). Report unhandled so the caller fires the LLM pass.
        print(json.dumps({"unhandled": 1, "markets": {}, "latest_text": "", "error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
