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

Two markets are per-thread enumerable today (ENUMERABLE_MARKETS):
  • Carousell — inbox rows expose no conversation id / href (hashed-class SPA), so the match key is
    the leading handle token parsed off the row text, class-agnostically; freshness is unread-badge +
    snippet-change.
  • Facebook Marketplace (selling inbox) — rows ARE per-thread selectable via CDP: each conversation
    is a `div[role="button"]` carrying an SVG avatar (`<image>`) and text "<Name> · <Listing><preview>".
    (This corrects the earlier note that FB rows are not per-thread selectable.) Per-thread enumeration
    is what closes the OLAF miss: FB's aggregate "Marketplace N new messages" badge can stay FLAT when
    a reply lands on a KNOWN thread, so the old aggregate gate dropped it. Here each row is classified
    individually instead.

    FB freshness — design (PRIMARY / SECONDARY, all FAIL-OPEN so FB can never be LESS sensitive than
    today's aggregate gate):
      PRIMARY   = snippet change on a TRACKED thread (is_fresh, is_tracked=True): the row's
                  "<Listing><preview>" text differs from the memo. This is font-weight/color
                  INDEPENDENT and fires REGARDLESS of the unread heuristic, so it catches a
                  known-thread reply even when FB renders the row un-bolded or the heuristic misreads
                  it as read (the OLAF miss). The comparison key strips the trailing VOLATILE
                  relative-time token (normalize_snippet_key) so a ticking clock on the SAME message
                  never re-fires.
      SECONDARY = the unread heuristic. For an UNTRACKED row (a brand-new enquiry / unknown handle)
                  there's no tracked baseline, so freshness still requires row["unread"]. FB renders
                  an UNREAD preview bolder / brighter than a READ one; the READ baseline was
                  calibrated live (FB_READ_GRAY / FB_READ_WEIGHT). The in-page detector flags a
                  preview leaf at >= MEDIUM weight (FB_UNREAD_WEIGHT_MIN=500) OR bolder than the
                  row's OWN title (theme-independent) OR not read-gray, and fails OPEN to unread on
                  uncertainty. A stored unread=False → row unread=True transition is also fresh even
                  on an unchanged snippet (un-poisons a memo an earlier peek misread as read).

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
ENUMERABLE_MARKETS = ("carousell", "fb")

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
CAROUSELL_ROW_ENUM_JS = r"""(() => {
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

# FB selling-inbox calibration (read live over CDP on the /marketplace/inbox/?filter=selling tab,
# 2026-06-28, all rows READ at the time — see the UNREAD caveat below):
#   READ preview leaf  → fontWeight 400, color rgb(176, 179, 184)  ← FB_READ_GRAY / FB_READ_WEIGHT
#   row title leaf     → fontWeight 500, color rgb(226, 229, 233)  (always brighter; NOT a read signal)
# UNREAD CAVEAT: the inbox had ZERO unread rows when calibrated, so the UNREAD threshold could NOT be
# confirmed from live data. FB normally renders an unread preview BOLDER and BRIGHTER than read. The
# in-page detector therefore treats "any preview leaf bolder than read OR not in the read-gray" as
# unread and fails OPEN to unread on uncertainty; the PRIMARY freshness signal is snippet-change
# (is_fresh), which does not depend on weight/color at all. Confirm FB_UNREAD_WEIGHT_MIN against one
# live unread row when one is available.
FB_READ_GRAY = "rgb(176, 179, 184)"   # computed color of a READ preview leaf (dark theme)
FB_READ_WEIGHT = 400                   # computed fontWeight of a READ preview leaf
# B5 — FB renders unread previews at >= MEDIUM weight (500), not only at 600+; the old 600 floor
# (and a t.length>3 guard) silently dropped semibold unread rows and 2-3 char previews ("ok","yes").
# Lowered to 500. Theme-independent backstop: a preview leaf BOLDER than the row's OWN title leaf is
# unread regardless of the dark-theme-only READ_GRAY color (so light theme is covered too). Still
# FAIL-OPEN: any uncertainty → unread. NOTE: B1 makes snippet-change PRIMARY, so this heuristic is
# now secondary; still worth confirming FB_UNREAD_WEIGHT_MIN against one live unread row.
FB_UNREAD_WEIGHT_MIN = 500             # >= this on a preview leaf ⇒ unread (confirm vs one live unread row)

# JS source for the anchored noise regex (mirrors FB_NOISE_RE). FB_ROW_ENUM_JS substitutes THIS
# literal so it can't drift from the Python FB_NOISE_RE (B2). The THIRD copy — the inline regex in
# buyer_peek.py's FB probe JS — is independent (the import cycle makes sharing awkward), so a test
# (test_buyer_peek.test_fb_probe_noise_regex_matches_python_copy) asserts all three stay consistent.
FB_NOISE_JS = (
    r"/^Number of unread notifications"
    r"|^Marketplace\b.*?\b\d+\s*new messages?\s*$"
    r"|^[^·]+·\s*Within\s+\d+\s*(?:kilomet(?:er|re)s?|met(?:er|re)s?|km)\s*$/i"
)

# FB row text separator and the noise rows that are NOT conversations (excluded by the JS and, as
# defense-in-depth, by parse_fb_row). Tokens observed live: the notifications counter, the aggregated
# "Marketplace … N new messages" row, and the "<City> · Within N kilometer/metre" location filter.
#
# B2 — these patterns are ANCHORED to the actual ROW SHAPE, not matched as free text anywhere in a
# preview, so a real buyer preview ("… Within 5m can you meet?", "… within 2m now", a preview that
# merely contains "N new messages") is NOT swallowed:
#   • notifications counter  → only the standalone aria row, anchored at the START.
#   • "N new messages" aggregate → the WHOLE row is "Marketplace … N new messages" (ends with it),
#     so anchor to END; a preview that continues past "new messages" is a real conversation.
#   • location filter        → the post-separator segment is ENTIRELY "Within N <unit>" (no trailing
#     conversation), so require it to fill the whole location segment to END. The bare `m\b` unit
#     alternative is DROPPED (it collided with ordinary English like "5m"/"meet").
FB_SEPARATOR = " · "
_FB_DIST_UNIT = r"(?:kilomet(?:er|re)s?|met(?:er|re)s?|km)"
FB_NOISE_RE = re.compile(
    r"^Number of unread notifications"
    r"|^Marketplace\b.*?\b\d+\s*new messages?\s*$"
    r"|^[^·]+·\s*Within\s+\d+\s*" + _FB_DIST_UNIT + r"\s*$",
    re.IGNORECASE,
)

# Enumerate FB Marketplace selling-inbox conversation rows → [{text, unread}]. Rows are
# div[role="button"] carrying an SVG avatar (<image>) and text "<Name> · <Listing><preview>". unread
# is computed IN-PAGE from each row's preview leaves vs the calibrated READ baseline (bolder OR
# brighter than read-gray ⇒ unread); noise rows are dropped; >= ~20 chars. Fail-open to [].
FB_ROW_ENUM_JS = r"""(() => {
  try {
    var READ_GRAY = '""" + FB_READ_GRAY + r"""';
    var UNREAD_WEIGHT_MIN = """ + str(FB_UNREAD_WEIGHT_MIN) + r""";
    var NOISE = """ + FB_NOISE_JS + r""";
    function leaves(el) {
      var out = [];
      (function walk(n) {
        if (n.nodeType !== 1) return;
        var kids = Array.prototype.slice.call(n.childNodes);
        var hasElemChild = kids.some(function (k) { return k.nodeType === 1; });
        if (hasElemChild) { kids.forEach(walk); }
        else if ((n.textContent || '').trim()) { out.push(n); }
      })(el);
      return out;
    }
    var rows = Array.prototype.slice.call(document.querySelectorAll('div[role="button"]'))
      .filter(function (r) {
        var t = (r.textContent || '').trim();
        return r.querySelector('image') && t.indexOf(' · ') !== -1 && t.length >= 20;
      });
    var out = [];
    for (var i = 0; i < rows.length && out.length < 40; i++) {
      var r = rows[i];
      var text = (r.textContent || '').trim().replace(/\s+/g, ' ');
      if (NOISE.test(text)) continue;
      var ls = leaves(r);
      // B4 — partition leaves STRUCTURALLY by DOM order, not by indexOf of collapsed text. The
      // separator-dot leaf splits the row: leaves AFTER its ordinal index are the PREVIEW region.
      // (indexOf found the FIRST occurrence, so a preview leaf whose text also appears in the title
      // was wrongly classified as title and the unread leaf skipped.)
      var sepOrdinal = -1;
      for (var s = 0; s < ls.length; s++) {
        if ((ls[s].textContent || '').trim() === '·') { sepOrdinal = s; break; }
      }
      // B5 — compute the row's TITLE weight (the leaf(s) before the separator) so we can flag a
      // preview leaf that is bolder than this row's own title — theme-independent, no color needed.
      var titleWeight = 0;
      var titleHi = sepOrdinal >= 0 ? sepOrdinal : ls.length;
      for (var k = 0; k < titleHi; k++) {
        var tw = parseInt(getComputedStyle(ls[k]).fontWeight, 10) || 0;
        if (tw > titleWeight) titleWeight = tw;
      }
      var unread = false;
      var previewLo = sepOrdinal >= 0 ? sepOrdinal + 1 : 0;
      for (var j = previewLo; j < ls.length; j++) {
        var n = ls[j];
        var t = (n.textContent || '').trim();
        if (t === '·' || /^\d{1,2}:\d{2}/.test(t)) continue;  // skip the separator dot + timestamp
        var cs = getComputedStyle(n);
        var fw = parseInt(cs.fontWeight, 10) || 0;
        // Bolder than the >= MEDIUM floor, OR bolder than this row's own title, OR not read-gray ⇒
        // unread. Fail OPEN to unread (the length>3 guard that dropped "ok"/"yes" is gone).
        if (fw >= UNREAD_WEIGHT_MIN) { unread = true; break; }
        if (titleWeight && fw > titleWeight) { unread = true; break; }
        if (cs.color && cs.color !== READ_GRAY) { unread = true; break; }
      }
      out.push({ text: text, unread: unread });
    }
    return out;
  } catch (e) { return []; }
})()"""

# scan_market looks up the per-market enumeration JS here. New enumerable markets register their JS.
ROW_ENUM_JS_BY_MARKET = {
    "carousell": CAROUSELL_ROW_ENUM_JS,
    "fb": FB_ROW_ENUM_JS,
}


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


def parse_fb_row(raw: dict) -> dict:
    """Split a raw FB {text, unread} row into {handle, snippet, unread}. FB row text is
    "<Name> · <Listing><preview>"; the handle is the name BEFORE the first " · " and the snippet is
    everything after it (listing + last message), which is the per-thread change signal kept in the
    memo. Splitting on the FIRST separator keeps the handle intact even when the listing/preview also
    contains " · ".

    Noise rows (the notifications counter, the aggregated "Marketplace … N new messages" row, the
    "<City> · Within N kilometer" location filter) parse to an EMPTY handle so classify() drops them.
    This mirrors the in-page JS filter as defense-in-depth (a JS-filter regression can't leak a
    spurious sell pass)."""
    text = (raw.get("text") or "").strip()
    unread = bool(raw.get("unread"))
    if FB_NOISE_RE.search(text):
        return {"handle": "", "snippet": text[:bp.SNIPPET_MAX], "unread": unread}
    sep = text.find(FB_SEPARATOR)
    if sep <= 0:  # no leading "<name> · " → not a conversation row; drop (empty handle)
        return {"handle": "", "snippet": text[:bp.SNIPPET_MAX], "unread": unread}
    handle = text[:sep]
    snippet = text[sep + len(FB_SEPARATOR):]
    return {
        "handle": normalize_handle(handle),
        "snippet": snippet[:bp.SNIPPET_MAX],
        "unread": unread,
    }


# Per-market raw-row parser. Carousell uses the timestamp-based split (parse_row); FB uses the
# "<Name> · <Listing><preview>" split (parse_fb_row).
ROW_PARSER_BY_MARKET = {
    "carousell": parse_row,
    "fb": parse_fb_row,
}


# A FB inbox row's snippet ends with exactly ONE VOLATILE relative-time token the clock keeps ticking
# ("2m", "5m", "1h", "10:11 AM", "Sat", "Just now", "Yesterday", "12/05"). The memo dedup key must
# reflect the PREVIEW MESSAGE TEXT, not that clock — otherwise a known thread re-fires on EVERY peek
# as the timestamp advances (a token disaster). We strip that single trailing token before comparing.
#
# It is deliberately CONSERVATIVE to avoid eating a product spec that happens to sit at the very end
# of a preview (e.g. "size 8w", "iphone 12s"):
#   • the relative-duration form is glued with NO space ("5m", not "5 m") — FB never spaces it — so a
#     spaced spec like "size 8 w" is not matched, and the digit must follow a non-word char or start
#     (it never strips a mid-word/model suffix like the "12s" in "iphone12s");
#   • only ONE trailing token is removed (no greedy "+"), so a two-token tail like "Shoes 8w 5m"
#     loses only the real "5m" timestamp, never the "8w" spec.
_TRAILING_TIME_RE = re.compile(
    r"(?:"
    r"\s*\d{1,2}:\d{2}\s*[AP]M"                          # clock time: "10:11 AM"
    r"|(?<![\w])\d{1,2}[smhdwy]"                          # relative dur, glued + not mid-word: "5m"
    r"|\s+(?:Just\s+now|Yesterday|Today|Mon|Tue|Wed|Thu|Fri|Sat|Sun)"  # word forms (need a space)
    r"|\s*\d{1,2}/\d{1,2}"                                # date: "12/05"
    r")\s*$",
    re.IGNORECASE,
)


def normalize_snippet_key(snippet: str) -> str:
    """The dedup key for a snippet: the preview text with the trailing volatile relative-time
    token(s) stripped (see _TRAILING_TIME_RE). Two peeks of the SAME message a few minutes apart
    therefore yield the SAME key (the clock ticked, the preview did not) → no re-fire; a genuinely
    new preview yields a DIFFERENT key → fresh."""
    return _TRAILING_TIME_RE.sub("", (snippet or "")).rstrip()


def is_fresh(key: str, row: dict, memo: dict, is_tracked: bool = False) -> bool:
    """Is this row worth a pass?

    PRIMARY (font-weight-independent): the preview TEXT changed since the memo. For a TRACKED thread
    (handle matches a tracked buy/sell thread — classify supplies is_tracked) a preview change is
    fresh REGARDLESS of the unread heuristic. This is the Olaf fix: FB may render a known-thread
    reply un-bolded (or the weight/color heuristic may misread it as read), but the changed preview
    text still trips freshness. The comparison is on the timestamp-normalized key so a ticking clock
    on the SAME message never re-fires.

    UNREAD GATE (secondary): an UNTRACKED row (a brand-new enquiry / unknown handle) still requires
    the unread heuristic — without a tracked baseline a bare text change can't be trusted as inbound.

    UNREAD TRANSITION (B3): a stored unread=False → row unread=True transition is fresh even on an
    unchanged snippet (an unread-state flip is new activity), un-poisoning the fast path when an
    earlier peek misread the row as read and stored that snippet.

    The memo dedup (same normalized snippet + same/raised unread) suppresses re-firing while the same
    reply sits unhandled."""
    prev = memo.get(key, {})
    snippet_changed = normalize_snippet_key(row.get("snippet", "")) != normalize_snippet_key(prev.get("snippet", ""))
    unread_now = bool(row.get("unread"))
    unread_transition = unread_now and not bool(prev.get("unread", False))

    if is_tracked:
        # PRIMARY: a preview change on a known thread is fresh no matter the weight/color heuristic.
        return snippet_changed or unread_transition
    # UNTRACKED: keep the unread gate, but still honor a snippet change while unread and the
    # False→True transition (B3) so a misread-as-read memo can't strand a real reply.
    if not unread_now:
        return False
    return snippet_changed or unread_transition


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


def _market_of(thread_id: str) -> str:
    """The market a thread belongs to = the prefix before the first ':' in its id
    ("carousell:2143175040" → "carousell", "fb:9988" → "fb"). Empty when unparseable."""
    tid = str(thread_id or "")
    return tid.split(":", 1)[0] if ":" in tid else ""


def build_sell_index(threads_dir: Path = SELL_THREADS_DIR) -> dict:
    """C2 — MARKET-SCOPED, collision-aware sell index:
        {(market, normalized buyer_handle) -> [thread_id, ...]}
    for active sell threads (buyers messaging us). The market is derived from the thread_id prefix.

    The earlier FLAT {handle -> thread_id} silently dropped a same-handle collision (the last sorted
    file won, possibly on a DIFFERENT market), which could mis-route the Fix-C peek-thread hint to
    the wrong thread/market. Keying by (market, handle) and keeping a LIST lets classify stay
    conservative: it only emits a hint when the handle resolves to EXACTLY ONE thread on THAT
    market."""
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
        thread_id = t.get("thread_id", path.stem)
        market = _market_of(thread_id)
        index.setdefault((market, handle), []).append(thread_id)
    return index


# --------------------------------------------------------------------------- classify

def classify(rows_by_market: dict, buy_index: dict, sell_index: dict, memo: dict) -> dict:
    """PURE router. For each SCANNED market (found True), walk its rows and route fresh ones:
    buy-thread match → buy (precedence); sell-thread match or unknown non-system → sell flagged;
    system handle → ignored. Advances next_memo for EVERY observed row so a still-unread reply
    doesn't re-fire next cycle. Unscanned markets are omitted (caller falls back to aggregate).

    `sell_threads` (Fix C) carries the matched SELL thread_id(s) per market — only TRACKED sell rows
    contribute an id (a brand-new enquiry flags the market but has no thread yet). A caller uses this
    to scope a worker to the one thread that actually has new mail (and stays conservative when the
    market has 0 or >1 fresh threads). `sell_markets` keeps its original bool-per-market contract."""
    next_memo = dict(memo)
    buy: list = []
    sell_markets: dict = {}
    sell_threads: dict = {}

    for market, mdata in rows_by_market.items():
        if not mdata.get("found"):
            continue  # not scanned → omit; caller falls back to the aggregate probe
        sell_new = False
        matched_threads: list = []
        for row in mdata.get("rows", []):
            handle = normalize_handle(row.get("handle", ""))
            if not handle:
                continue
            key = f"{market}:{handle}"
            # C2 — resolve the SELL match market-scoped (a same-handle collision on another market
            # must not leak here). A handle in the buy_index OR with an active sell thread on THIS
            # market is a TRACKED thread, which makes a snippet change PRIMARY (B1 — the Olaf fix).
            in_buy = handle in buy_index
            sell_ids = sell_index.get((market, handle), [])  # active sell thread_ids on THIS market
            is_tracked = in_buy or bool(sell_ids)
            fresh = is_fresh(key, row, memo, is_tracked=is_tracked)
            next_memo[key] = {"snippet": row.get("snippet", ""), "unread": bool(row.get("unread"))}
            if not fresh:
                continue
            if in_buy:
                info = buy_index[handle]
                buy.append({"want_id": info.get("want_id", ""), "thread_id": info.get("thread_id", ""),
                            "market": market, "handle": handle,
                            "latest_text": f"[{handle}] {row.get('snippet', '')}".strip()})
            elif sell_ids:
                sell_new = True
                # Conservative hint: contribute a thread id ONLY when the handle resolves to EXACTLY
                # ONE active thread on this market (0 / >1 → flag the market but emit no hint).
                if len(sell_ids) == 1:
                    matched_threads.append(sell_ids[0])
            elif handle not in SYSTEM_HANDLES:
                sell_new = True  # unknown non-system handle → a brand-new buyer enquiry (no thread id)
        sell_markets[market] = sell_new
        sell_threads[market] = matched_threads

    return {"buy": buy, "sell_markets": sell_markets, "sell_threads": sell_threads,
            "next_memo": next_memo}


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
    """Return {found, rows:[{handle,snippet,unread}]} for one enumerable market. Fail-open.

    The per-market enumeration JS and raw-row parser are looked up by market (Carousell vs FB), so a
    new enumerable market is registered in ROW_ENUM_JS_BY_MARKET + ROW_PARSER_BY_MARKET only."""
    probe = bp.MARKET_PROBES.get(market)
    enum_js = ROW_ENUM_JS_BY_MARKET.get(market)
    parser = ROW_PARSER_BY_MARKET.get(market)
    if not probe or not enum_js or parser is None:
        return {"found": False, "rows": []}
    tab = bp._find_tab(targets, probe)
    if tab is None:
        return {"found": False, "rows": []}
    try:
        raw = bp.cdp_eval(tab["webSocketDebuggerUrl"], enum_js)
        if not isinstance(raw, list):
            return {"found": False, "rows": []}
        rows = [parser(r) for r in raw if isinstance(r, dict)]
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
        return {"buy": [], "sell_markets": {}, "sell_threads": {}, "next_memo": {}}


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
    return _peek(SELL_MEMO_PATH).get("sell_markets", {})


def sell_threads_new() -> dict:
    """Per enumerable market, the matched TRACKED-SELL thread_id(s) with a fresh row this peek:
    {market: [thread_id, ...]} (a brand-new enquiry flags the market in sell_markets_new but has no
    thread yet, so it contributes no id here). Same memo + persistence as sell_markets_new — a thin
    wrapper over the SAME _peek classification, so the two never disagree about freshness. Fail-open
    to {} when the (older-shape) classification omits sell_threads."""
    return _peek(SELL_MEMO_PATH).get("sell_threads", {})


def sell_peek() -> dict:
    """C-followup — ONE SELL _peek, returning BOTH signals from a SINGLE memo advance:
        {"sell_markets": {market: bool}, "sell_threads": {market: [thread_id, ...]}}

    _peek ADVANCES the SELL memo as a side effect, so calling sell_markets_new() AND
    sell_threads_new() back-to-back peeks TWICE: the second peek sees the already-advanced memo and
    finds nothing fresh, nulling the Fix-C peek-thread hint. A caller that needs both (the daemon
    poll path) calls THIS once and reuses the result for the freshness gate and the thread hint.
    Fail-open: an older-shape classification that omits sell_threads yields {} for it (never crashes
    the caller)."""
    out = _peek(SELL_MEMO_PATH)
    return {"sell_markets": out.get("sell_markets", {}),
            "sell_threads": out.get("sell_threads", {})}


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
                      "sell_threads": out.get("sell_threads", {}),
                      "scanned": {m: len(v.get("rows", [])) for m, v in rows.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
