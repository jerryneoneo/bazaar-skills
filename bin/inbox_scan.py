#!/usr/bin/env python3
"""inbox_scan.py — unified, non-LLM inbox classifier (~0 tokens, no Playwright, no LLM).

The marketplace inbox is SHARED by both sides: buyers messaging us about our listings (sell) and
sellers replying to threads where WE are the buyer (buy). The aggregate unread badge can't tell
them apart, so today `buyer_peek` fires a sell pass on buy-side unread, and `buy_peek` (file-only)
fires a full buy pass every cycle for any open want regardless of whether a seller actually replied.

This module reads the inbox ONCE per cycle over the warm CDP Chrome (reusing buyer_peek's stdlib
transport), enumerates conversation ROWS, and classifies each row by its leading counterparty
handle against our tracked threads:
  • handle matches a tracked BUY thread (data/buyer_threads, status liaising/agreed) → buy pass
  • handle matches a tracked SELL thread (data/threads, status active), or is an unknown non-system
    handle (a brand-new enquiry) → sell pass
  • a known SYSTEM handle (carousell_assistant, promos) → neither

Carousell only: its inbox rows expose no conversation id / href (hashed-class SPA), so the match key
is the leading handle token parsed from the row text, class-agnostically. FB rows are not per-thread
selectable via CDP (see buyer_peek MARKET_PROBES), so FB keeps buyer_peek's aggregate behavior; this
module deliberately scans only the markets it can enumerate and lets callers fall back for the rest.

Pure, fail-open: any probe error degrades to "nothing found" so it can never strand a reply (the
daemon's periodic forced sweep is the backstop).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import buyer_peek as bp  # reuse the CDP seam: cdp_eval, _find_tab, list_page_targets, SNIPPET_MAX

DATA_DIR = bp.SELLER_DIR / "data"
BUYER_THREADS_DIR = DATA_DIR / "buyer_threads"   # buy-side: we are the buyer (seller_handle)
SELL_THREADS_DIR = DATA_DIR / "threads"          # sell-side: buyers message us (buyer_handle)
# The buy and sell peeks run as SEPARATE daemon subprocesses, so each keeps its own per-row memo
# (a shared one would race: whichever ran first would advance it and the other would miss the row).
BUY_MEMO_PATH = DATA_DIR / "inbox_buy_state.json"
SELL_MEMO_PATH = DATA_DIR / "inbox_sell_state.json"

LIAISE_STATES = {"liaising", "agreed"}
ACTIVE_SELL_STATES = {"active"}
# Marketplace system / promo accounts that send unread messages but are never actionable.
SYSTEM_HANDLES = {"carousell_assistant", "selltocarousell_mobiles", "carousell"}

# Only markets whose inbox rows are reliably enumerable per-thread via CDP. Others fall back to
# buyer_peek's aggregate signal (see module docstring).
ENUMERABLE_MARKETS = ("carousell",)

# Leading-handle parse: the row text is "<handle><timestamp>[unread#]<listing><snippet>" with no
# separators (hashed-class SPA). The handle is everything before the first timestamp token.
_TS = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M|Today|Yesterday|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|"
    r"Saturday|\d{1,2}/\d{1,2})"
)

# Enumerate Carousell inbox conversation rows and return [{handle, snippet, unread}]. Class-agnostic
# (Carousell ships hashed classes): rows are div[role="button"] carrying an avatar <img>; the handle
# is parsed off the row text in Python so DOM drift is a single-regex fix. unread = the row has a
# lone-digit badge element. Fail-open to [] inside the page.
ROW_ENUM_JS = r"""(() => {
  try {
    const rows = Array.from(document.querySelectorAll('div[role="button"]'))
      .filter(r => r.querySelector('img') && (r.textContent || '').trim().length > 6);
    return rows.slice(0, 40).map(r => {
      const text = (r.textContent || '').trim().replace(/\s+/g, ' ');
      // Unread = a lone-count badge element: "2" or the capped "9+"/"99+" variant Carousell shows.
      const unread = Array.from(r.querySelectorAll('span,div'))
        .some(n => /^\d{1,3}\+?$/.test((n.textContent || '').trim()));
      return { text: text, unread: unread };
    });
  } catch (e) { return []; }
})()"""


# --------------------------------------------------------------------------- pure helpers

def normalize_handle(handle: str) -> str:
    return (handle or "").strip().lower()


def parse_row(raw: dict) -> dict:
    """Split a raw {text, unread} inbox row into {handle, snippet, unread}. The handle is the text
    before the first timestamp token; the snippet (timestamp + listing + last message) is the
    change signal kept in the memo."""
    text = (raw.get("text") or "").strip()
    m = _TS.search(text)
    if m and m.start() > 0:
        handle = text[:m.start()]
        snippet = text[m.start():]
    else:  # no timestamp before any text → degrade: first whitespace token as handle, whole text as
        # snippet. Fails SAFE: e.g. a display name that is itself a day-name token ("Monday") yields
        # an unknown handle → routed as a new enquiry (an extra sell pass), never a missed message.
        handle = text.split(" ", 1)[0]
        snippet = text
    return {
        "handle": normalize_handle(handle),
        "snippet": snippet[:bp.SNIPPET_MAX],
        "unread": bool(raw.get("unread")),
    }


def is_fresh(key: str, row: dict, memo: dict) -> bool:
    """A row is fresh (worth a pass) only when it is UNREAD and its snippet changed since the memo.
    Mirrors buyer_peek.is_new but per-row: unread is the inbound signal, snippet-change dedupes
    re-firing while the same reply sits unhandled."""
    if not row.get("unread"):
        return False
    return row.get("snippet", "") != memo.get(key, {}).get("snippet", "")


# --------------------------------------------------------------------------- index builders

def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build_buy_index(threads_dir: Path = BUYER_THREADS_DIR) -> dict:
    """{normalized seller_handle -> {want_id, thread_id}} for buy threads we are actively liaising."""
    index: dict = {}
    if not Path(threads_dir).is_dir():
        return index
    for path in sorted(Path(threads_dir).glob("*.json")):
        t = _load_json(path)
        if str(t.get("status", "")).lower() not in LIAISE_STATES:
            continue
        handle = normalize_handle(t.get("seller_handle", ""))
        if not handle:
            continue
        index[handle] = {"want_id": t.get("want_id", ""),
                         "thread_id": t.get("thread_id", path.stem)}
    return index


def build_sell_index(threads_dir: Path = SELL_THREADS_DIR) -> dict:
    """{normalized buyer_handle -> thread_id} for active sell threads (buyers messaging us)."""
    index: dict = {}
    if not Path(threads_dir).is_dir():
        return index
    for path in sorted(Path(threads_dir).glob("*.json")):
        t = _load_json(path)
        if str(t.get("status", "")).lower() not in ACTIVE_SELL_STATES:
            continue
        handle = normalize_handle(t.get("buyer_handle", ""))
        if not handle:
            continue
        index[handle] = t.get("thread_id", path.stem)
    return index


# --------------------------------------------------------------------------- classify

def classify(rows_by_market: dict, buy_index: dict, sell_index: dict, memo: dict) -> dict:
    """PURE router. For each SCANNED market (found True), walk its rows and route fresh ones:
    buy-thread match → buy (precedence); sell-thread match or unknown non-system → sell flagged;
    system handle → ignored. Advances next_memo for EVERY observed row so a still-unread reply
    doesn't re-fire next cycle. Unscanned markets are omitted (caller falls back to aggregate)."""
    next_memo = dict(memo)
    buy: list = []
    sell_markets: dict = {}

    for market, mdata in rows_by_market.items():
        if not mdata.get("found"):
            continue  # not scanned → omit; caller falls back to the aggregate probe
        sell_new = False
        for row in mdata.get("rows", []):
            handle = normalize_handle(row.get("handle", ""))
            if not handle:
                continue
            key = f"{market}:{handle}"
            fresh = is_fresh(key, row, memo)
            next_memo[key] = {"snippet": row.get("snippet", ""), "unread": bool(row.get("unread"))}
            if not fresh:
                continue
            if handle in buy_index:
                info = buy_index[handle]
                buy.append({"want_id": info.get("want_id", ""), "thread_id": info.get("thread_id", ""),
                            "market": market, "handle": handle,
                            "latest_text": f"[{handle}] {row.get('snippet', '')}".strip()})
            elif handle in sell_index:
                sell_new = True
            elif handle not in SYSTEM_HANDLES:
                sell_new = True  # unknown non-system handle → a brand-new buyer enquiry
        sell_markets[market] = sell_new

    return {"buy": buy, "sell_markets": sell_markets, "next_memo": next_memo}


# --------------------------------------------------------------------------- scan (CDP)

def _read_memo(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_memo(path: Path, memo: dict) -> None:
    try:
        path.write_text(json.dumps(memo, indent=2))
    except OSError:
        pass


def scan_market(market: str, targets: list) -> dict:
    """Return {found, rows:[{handle,snippet,unread}]} for one enumerable market. Fail-open."""
    probe = bp.MARKET_PROBES.get(market)
    if not probe:
        return {"found": False, "rows": []}
    tab = bp._find_tab(targets, probe)
    if tab is None:
        return {"found": False, "rows": []}
    try:
        raw = bp.cdp_eval(tab["webSocketDebuggerUrl"], ROW_ENUM_JS)
        if not isinstance(raw, list):
            return {"found": False, "rows": []}
        rows = [parse_row(r) for r in raw if isinstance(r, dict)]
        return {"found": True, "rows": [r for r in rows if r["handle"]]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"found": False, "rows": []}


def scan(markets: list) -> dict:
    """Scan the enumerable markets among `markets`. Non-enumerable markets are omitted (callers fall
    back to buyer_peek's aggregate signal for those)."""
    targets = bp.list_page_targets()
    return {m: scan_market(m, targets) for m in markets if m in ENUMERABLE_MARKETS}


def _peek(memo_path: Path) -> dict:
    """One scan+classify against `memo_path`, persisting the advanced memo. Fail-open to an empty
    classification so a probe error degrades to 'nothing found' (never strands a reply)."""
    try:
        enabled = [m for m in bp.enabled_markets() if m in ENUMERABLE_MARKETS]
        rows = scan(enabled)
        memo = _read_memo(memo_path)
        out = classify(rows, build_buy_index(), build_sell_index(), memo)
        _write_memo(memo_path, out["next_memo"])
        return out
    except Exception:  # last-resort fail-open — a peek must never crash its caller
        return {"buy": [], "sell_markets": {}, "next_memo": {}}


def buy_pending() -> dict:
    """Is there a fresh seller reply on a tracked (liaising/agreed) BUY thread? Returns the first one
    as {pending, want_id, thread_id, latest_text} (a superset of the buy_peek stdout contract — the
    extra thread_id lets a caller scope to the exact thread), or pending 0. ~0 tokens (CDP read only)."""
    out = _peek(BUY_MEMO_PATH)
    if out["buy"]:
        b = out["buy"][0]
        return {"pending": 1, "want_id": b["want_id"], "thread_id": b["thread_id"],
                "latest_text": b["latest_text"]}
    return {"pending": 0, "want_id": None, "thread_id": None, "latest_text": ""}


def sell_markets_new() -> dict:
    """Per enumerable market, True if it has a fresh SELL row (tracked sell thread or a new
    non-system enquiry) — buy-thread rows and promos excluded. {market: bool}. Memo-gated (won't
    re-fire while a reply is still pending) and persists the advanced memo."""
    return _peek(SELL_MEMO_PATH)["sell_markets"]


def sell_actionable_now() -> dict:
    """ABSOLUTE, READ-ONLY precise sell signal for the forced-sweep recheck: classifies against an
    EMPTY memo (so any currently-unread tracked-sell or new-enquiry row counts) and persists NOTHING.
    {market: bool} for enumerable markets only — callers fall back to the conservative count probe for
    markets absent here (non-enumerable, or a scan failure). Mirrors buyer_recheck's read-only,
    memo-free contract while excluding buy-thread rows and promos from 'unhandled'."""
    try:
        enabled = [m for m in bp.enabled_markets() if m in ENUMERABLE_MARKETS]
        rows = scan(enabled)
        out = classify(rows, build_buy_index(), build_sell_index(), {})  # empty memo → absolute
        return out["sell_markets"]
    except Exception:  # fail-open: no precise signal → caller uses the conservative count probe
        return {}


# --------------------------------------------------------------------------- CLI (dry-run/debug)

def main(argv: list) -> int:
    """Read-only dry-run: scan + classify against an EMPTY memo (does not persist), so it prints what
    is currently actionable without affecting the live peek memos."""
    enabled = [m for m in bp.enabled_markets() if m in ENUMERABLE_MARKETS]
    rows = scan(enabled)
    out = classify(rows, build_buy_index(), build_sell_index(), {})
    print(json.dumps({"buy": out["buy"], "sell_markets": out["sell_markets"],
                      "scanned": {m: len(v.get("rows", [])) for m, v in rows.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
