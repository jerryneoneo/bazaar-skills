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
 12. parse_fb_row: FB row text "<Name> · <Listing><preview>" splits on the FIRST " · " — handle is
     the name (normalized), snippet is listing+preview; unread carries through.
 13. classify (FB): a fresh+unread unknown FB handle (a brand-new enquiry) fires sell['fb']=True —
     the Olaf-safety property (a new inbound on FB is never silently dropped).
 14. classify (FB): a fresh FB row matching a tracked BUY thread fires buy only; sell not flagged.
 15. classify (FB): an FB row that is READ (unread False) fires nothing, even on an index match.
 16. FB noise rows ("Number of unread notifications", the "… new messages" aggregate, location/nav)
     are excluded by the enumeration JS / parse, never producing a spurious sell pass.
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


def _fb(rows, found=True):
    return {"fb": {"found": found, "rows": rows}}


# --------------------------------------------------------------------------- is_fresh

def test_is_fresh():
    print("is_fresh (UNTRACKED row) = unread AND snippet changed since memo:")
    # memo carries unread=True (as next_memo always writes it) so the same-snippet case isn't a
    # spurious False→True transition (see B3).
    memo = {"carousell:maxlinda": {"snippet": "old", "unread": True}}
    check("unread + changed snippet → fresh",
          inbox_scan.is_fresh("carousell:maxlinda", _row("maxlinda", "new reply", True), memo) is True)
    check("unread + same snippet → not fresh",
          inbox_scan.is_fresh("carousell:maxlinda", _row("maxlinda", "old", True), memo) is False)
    check("read (unread False) + UNTRACKED → not fresh",
          inbox_scan.is_fresh("carousell:maxlinda", _row("maxlinda", "new reply", False), memo) is False)
    check("unseen key + unread → fresh",
          inbox_scan.is_fresh("carousell:newcomer", _row("newcomer", "hi", True), memo) is True)


# --------------------------------------------------------------------------- B1: snippet-change is
# PRIMARY on a TRACKED thread (the Olaf miss — a known-thread reply FB renders un-bolded)

def test_is_fresh_tracked_snippet_change_overrides_read():
    print("B1: on a TRACKED thread a snippet change is fresh REGARDLESS of the unread heuristic"
          " (the Olaf miss — FB did not re-bold the reply):")
    memo = {"fb:olaf": {"snippet": "Glass Kettle old preview", "unread": True}}
    # FB renders the new reply un-bolded → unread False, but the preview TEXT changed.
    row = _row("olaf", "Glass Kettle can you meet today?", False)
    check("tracked + snippet changed + unread False → fresh",
          inbox_scan.is_fresh("fb:olaf", row, memo, is_tracked=True) is True)
    check("UNtracked + snippet changed + unread False → NOT fresh (unknown row needs the unread gate)",
          inbox_scan.is_fresh("fb:olaf", row, memo, is_tracked=False) is False)


def test_normalize_snippet_key():
    print("B1 helper: normalize_snippet_key strips exactly ONE trailing FB time token and preserves"
          " product specs:")
    n = inbox_scan.normalize_snippet_key
    # FB relative/absolute timestamps at the tail are stripped.
    check("strips glued relative minutes", n("Glass Kettle can you meet?2m") == "Glass Kettle can you meet?")
    check("strips clock time", n("Glass Kettle?10:11 AM") == "Glass Kettle?")
    check("strips weekday (space-preceded)", n("Glass Kettle? Sat") == "Glass Kettle?")
    check("strips Yesterday", n("Glass Kettle? Yesterday") == "Glass Kettle?")
    check("strips date", n("Glass Kettle?12/05") == "Glass Kettle?")
    # Only ONE token removed → a spec before the real timestamp survives (no chained over-strip).
    check("two-token tail keeps the spec, strips only the timestamp",
          n("Nike Shoes 8w 5m") == "Nike Shoes 8w")
    # A spaced spec is not a glued duration token → preserved.
    check("spaced spec preserved", n("size 8 w") == "size 8 w")
    # Same preview + different clock → same key (no re-fire); different preview → different key.
    check("same preview, diff clock → equal key",
          n("can you meet?2m") == n("can you meet?5m"))
    check("different preview → different key",
          n("can you meet?2m") != n("yes 6pm works10m"))


def test_is_fresh_normalizes_volatile_timestamp_no_refire():
    print("B1 caveat: the trailing volatile relative-time token must be normalized out of the memo"
          " key so a known thread does NOT re-fire every peek as the clock ticks:")
    # Same preview text, only the trailing relative timestamp changed ("2m" → "5m").
    memo = {"fb:olaf": {"snippet": "Glass Kettle can you meet?2m", "unread": True}}
    same_text_new_clock = _row("olaf", "Glass Kettle can you meet?5m", True)
    check("same preview, changed relative timestamp → NOT fresh (no re-fire storm)",
          inbox_scan.is_fresh("fb:olaf", same_text_new_clock, memo, is_tracked=True) is False)
    # A real new message (preview text differs) on the same known thread → fresh.
    real_reply = _row("olaf", "Glass Kettle yes 6pm works10m", True)
    check("changed preview text → fresh (a real reply still fires)",
          inbox_scan.is_fresh("fb:olaf", real_reply, memo, is_tracked=True) is True)


def test_classify_tracked_sell_snippet_change_unbolded_fires():
    print("B1 end-to-end: a snippet change on a tracked SELL thread with unread False fires sell"
          " (classify supplies the is_tracked context):")
    sell_index = {("fb", "olaf"): ["fb:9988"]}
    memo = {"fb:olaf": {"snippet": "Glass Kettle old preview2m", "unread": True}}
    rows = _fb([_row("olaf", "Glass Kettle can you meet today?5m", False)])
    out = inbox_scan.classify(rows, {}, sell_index, memo)
    check("sell flagged for fb despite unread False", out["sell_markets"].get("fb") is True)
    check("matched thread surfaced", out["sell_threads"].get("fb") == ["fb:9988"])


def test_classify_tracked_buy_snippet_change_unbolded_fires():
    print("B1 end-to-end (buy side): a snippet change on a tracked BUY thread with unread False"
          " fires buy:")
    buy_index = {"olaf": {"want_id": "glass-kettle", "thread_id": "fb:9988"}}
    memo = {"fb:olaf": {"snippet": "Glass Kettle old preview", "unread": True}}
    rows = _fb([_row("olaf", "Glass Kettle deal?", False)])
    out = inbox_scan.classify(rows, buy_index, {}, memo)
    check("one buy entry despite unread False", len(out["buy"]) == 1)


def test_classify_tracked_no_refire_on_clock_tick():
    print("B1 caveat end-to-end: a tracked thread with only a ticking timestamp does NOT re-fire:")
    sell_index = {("fb", "olaf"): ["fb:9988"]}
    memo = {"fb:olaf": {"snippet": "Glass Kettle can you meet?2m", "unread": True}}
    rows = _fb([_row("olaf", "Glass Kettle can you meet?5m", True)])
    out = inbox_scan.classify(rows, {}, sell_index, memo)
    check("sell NOT re-flagged on clock tick", out["sell_markets"].get("fb") is False)


# --------------------------------------------------------------------------- B3: memo-poison repair
# (a False→True unread transition is fresh even on an unchanged snippet)

def test_is_fresh_unread_transition_fires_on_same_snippet():
    print("B3: a stored unread=False → row unread=True transition is fresh even on an unchanged"
          " snippet (un-poison the fast path):")
    memo = {"carousell:maxlinda": {"snippet": "Can do $55", "unread": False}}
    row = _row("maxlinda", "Can do $55", True)  # same snippet, but now bolded
    check("False→True unread transition → fresh (regardless of snippet equality)",
          inbox_scan.is_fresh("carousell:maxlinda", row, memo) is True)
    # And it does NOT re-fire once the memo records unread=True with the same snippet.
    memo2 = {"carousell:maxlinda": {"snippet": "Can do $55", "unread": True}}
    check("True→True same snippet → not fresh (no re-fire)",
          inbox_scan.is_fresh("carousell:maxlinda", row, memo2) is False)


def test_classify_unread_transition_fires():
    print("B3 end-to-end: classify fires on a False→True unread transition with the same snippet:")
    sell_index = {("carousell", "buyerx"): ["carousell:77"]}
    memo = {"carousell:buyerx": {"snippet": "still available?", "unread": False}}
    rows = _car([_row("buyerx", "still available?", True)])
    out = inbox_scan.classify(rows, {}, sell_index, memo)
    check("sell flagged on the unread transition", out["sell_markets"].get("carousell") is True)


# --------------------------------------------------------------------------- B2: noise regex no
# longer over-matches real buyer previews

def test_fb_noise_regex_keeps_real_previews():
    print("B2: real buyer previews containing 'within Nm' / 'N new messages' are NOT treated as"
          " noise (handle parses, so the message is not dropped):")
    real_previews = [
        "Olaf · Glass Kettle Within 5m can you meet?",
        "Jane · Sofa within 2m now",
        "Marketplace listing - 2 new messages from buyer · Sofa shall we meet?",
        "Bob · Item 5m left ok?",
    ]
    for text in real_previews:
        row = inbox_scan.parse_fb_row({"text": text, "unread": True})
        check(f"real preview NOT noise (handle non-empty): {text[:42]!r}", row["handle"] != "")


def test_fb_noise_regex_still_drops_genuine_noise():
    print("B2: the genuine noise rows (notifications counter, the 'N new messages' aggregate row,"
          " the '<City> · Within N km' location filter) ARE still excluded:")
    noise_texts = [
        "Number of unread notifications20+",
        "Marketplace 3 new messages",
        "Singapore · Within 1 kilometer",
        "Singapore · Within 5 km",
        "Toa Payoh · Within 500 meters",
    ]
    for text in noise_texts:
        row = inbox_scan.parse_fb_row({"text": text, "unread": True})
        check(f"genuine noise → empty handle: {text[:42]!r}", row["handle"] == "")


# --------------------------------------------------------------------------- C2: market-scoped,
# collision-aware sell index (a same-handle collision must not mis-route the peek-thread hint)

def test_build_sell_index_market_scoped_collision():
    print("C2: build_sell_index is market-scoped — the same handle active on two markets does NOT"
          " collide; each (market, handle) resolves to its own thread:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write(d, "carousell:111.json", {"thread_id": "carousell:111", "buyer_handle": "janedoe",
                                         "status": "active"})
        _write(d, "fb:222.json", {"thread_id": "fb:222", "buyer_handle": "janedoe",
                                  "status": "active"})
        idx = inbox_scan.build_sell_index(d)
        check("carousell:janedoe → its own thread list",
              idx.get(("carousell", "janedoe")) == ["carousell:111"])
        check("fb:janedoe → its own thread list",
              idx.get(("fb", "janedoe")) == ["fb:222"])


def test_classify_collision_no_crossmarket_hint():
    print("C2: classify must NOT surface a cross-market thread_id for a same-handle collision — a"
          " carousell row from 'janedoe' surfaces ONLY the carousell thread, never fb:222:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write(d, "carousell:111.json", {"thread_id": "carousell:111", "buyer_handle": "janedoe",
                                         "status": "active"})
        _write(d, "fb:222.json", {"thread_id": "fb:222", "buyer_handle": "janedoe",
                                  "status": "active"})
        sell_index = inbox_scan.build_sell_index(d)
        out = inbox_scan.classify(_car([_row("janedoe", "still available?", True)]), {}, sell_index, {})
        check("sell flagged for carousell", out["sell_markets"].get("carousell") is True)
        check("carousell surfaces ONLY its own thread (not fb:222)",
              out["sell_threads"].get("carousell") == ["carousell:111"])


def test_classify_ambiguous_same_market_handle_no_hint():
    print("C2: when one handle maps to >1 active thread ON THE SAME market, contribute NO hint"
          " (conservative contract: 0 or >1 → no peek-thread):")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write(d, "carousell:1.json", {"thread_id": "carousell:1", "buyer_handle": "dupe",
                                       "status": "active"})
        _write(d, "carousell:2.json", {"thread_id": "carousell:2", "buyer_handle": "dupe",
                                       "status": "active"})
        sell_index = inbox_scan.build_sell_index(d)
        out = inbox_scan.classify(_car([_row("dupe", "hi", True)]), {}, sell_index, {})
        check("market still flagged (there IS new mail)", out["sell_markets"].get("carousell") is True)
        check("no thread hint when ambiguous (>1 on same market)",
              out["sell_threads"].get("carousell", []) == [])


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
    sell_index = {("carousell", "truewolf.5feb9c"): ["carousell:2143175040"]}
    out = inbox_scan.classify(_car([_row("truewolf.5feb9c", "can collect today?", True)]), {}, sell_index, {})
    check("sell flagged for carousell", out["sell_markets"].get("carousell") is True)
    check("buy empty", out["buy"] == [])


def test_classify_surfaces_matched_sell_thread_id():
    print("Fix C: classify surfaces the matched SELL thread_id(s) per market (sell_threads):")
    sell_index = {("carousell", "truewolf.5feb9c"): ["carousell:2143175040"]}
    out = inbox_scan.classify(_car([_row("truewolf.5feb9c", "can collect today?", True)]), {}, sell_index, {})
    check("sell_threads names the matched thread_id",
          out.get("sell_threads", {}).get("carousell") == ["carousell:2143175040"])
    check("sell_markets bool still True (back-compat)", out["sell_markets"].get("carousell") is True)


def test_classify_new_enquiry_has_no_thread_id():
    print("Fix C: a NEW enquiry (no tracked thread) flags the market but lists no thread_id:")
    out = inbox_scan.classify(_car([_row("brandnew_buyer", "is this available?", True)]), {}, {}, {})
    check("sell_markets True (a fresh enquiry still needs a pass)",
          out["sell_markets"].get("carousell") is True)
    check("sell_threads has no id for an untracked enquiry",
          out.get("sell_threads", {}).get("carousell", []) == [])


def test_classify_multiple_matched_sell_threads():
    print("Fix C: two fresh tracked-sell rows on one market → both thread_ids surfaced (the 0/>1 rule"
          " upstream uses this to stay conservative):")
    sell_index = {("carousell", "alice"): ["carousell:1"], ("carousell", "bob"): ["carousell:2"]}
    out = inbox_scan.classify(
        _car([_row("alice", "still up?", True), _row("bob", "lower price?", True)]), {}, sell_index, {})
    check("both thread_ids surfaced",
          sorted(out.get("sell_threads", {}).get("carousell", [])) == ["carousell:1", "carousell:2"])


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
    print("read row (unread False) on a tracked thread with an UNCHANGED snippet fires nothing"
          " (already-seen; B1 only makes a snippet CHANGE primary, not a stale re-read):")
    buy_index = {"maxlinda": {"want_id": "w", "thread_id": "carousell:1410917548"}}
    memo = {"carousell:maxlinda": {"snippet": "Can do $55", "unread": True}}
    out = inbox_scan.classify(_car([_row("maxlinda", "Can do $55", False)]), buy_index, {}, memo)
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
    sell_index = {("carousell", "dual"): ["carousell:SELL"]}
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


# --------------------------------------------------------------------------- FB parse + routing

def test_parse_fb_row():
    print("parse_fb_row splits FB '<Name> · <Listing><preview>' on the FIRST ' · ':")
    raw = {"text": "Olaf · Home Proud Glass Kettle HP 15GK 1.5LI'm not aro...", "unread": True}
    row = inbox_scan.parse_fb_row(raw)
    check("handle is the name (normalized)", row["handle"] == "olaf")
    check("snippet keeps the listing", "Home Proud Glass Kettle" in row["snippet"])
    check("snippet keeps the preview", "I'm not aro" in row["snippet"])
    check("snippet does NOT include the name", "Olaf" not in row["snippet"])
    check("unread carries through", row["unread"] is True)


def test_parse_fb_row_splits_on_first_separator():
    print("parse_fb_row splits on the FIRST ' · ' even when the listing also contains ' · ':")
    raw = {"text": "Jos · Dear Singapore · Marketplace blah", "unread": False}
    row = inbox_scan.parse_fb_row(raw)
    check("handle is only the leading name", row["handle"] == "jos")
    check("snippet keeps the rest incl later ' · '", "Dear Singapore · Marketplace" in row["snippet"])


def test_fb_classify_new_enquiry_fires_sell():
    print("FB Olaf-safety: fresh+unread unknown FB handle (new enquiry) → sell['fb'] True:")
    rows = [inbox_scan.parse_fb_row({"text": "Olaf · Home Proud Glass Kettle HP 15GK 1.5LIs this still around?",
                                     "unread": True})]
    out = inbox_scan.classify(_fb(rows), {}, {}, {})
    check("sell flagged for fb", out["sell_markets"].get("fb") is True)
    check("buy empty", out["buy"] == [])


def test_fb_known_buy_thread_precedence():
    print("FB row matching a tracked BUY thread → buy only, sell not flagged:")
    buy_index = {"olaf": {"want_id": "glass-kettle", "thread_id": "fb:9988"}}
    rows = [inbox_scan.parse_fb_row({"text": "Olaf · Home Proud Glass Kettle HP 15GK 1.5LCan do $7?",
                                     "unread": True})]
    out = inbox_scan.classify(_fb(rows), buy_index, {}, {})
    check("one buy entry", len(out["buy"]) == 1)
    check("buy entry carries thread_id", out["buy"] and out["buy"][0]["thread_id"] == "fb:9988")
    check("buy entry market is fb", out["buy"] and out["buy"][0]["market"] == "fb")
    check("sell NOT flagged for fb", out["sell_markets"].get("fb") is False)


def test_fb_read_row_ignored():
    print("FB read row (unread False) on a tracked thread with an UNCHANGED snippet fires nothing"
          " (already-seen; B1 only fires on a snippet CHANGE):")
    sell_index = {("fb", "olaf"): ["fb:9988"]}
    snippet_text = "Olaf · Home Proud Glass Kettle HP 15GK 1.5LGood news!"
    rows = [inbox_scan.parse_fb_row({"text": snippet_text, "unread": False})]
    memo = {"fb:olaf": {"snippet": rows[0]["snippet"], "unread": True}}
    out = inbox_scan.classify(_fb(rows), {}, sell_index, memo)
    check("sell NOT flagged", out["sell_markets"].get("fb") is False)
    check("buy empty", out["buy"] == [])


def test_fb_noise_rows_excluded():
    print("FB noise rows (notifications/aggregate/location) parse to an EMPTY handle (defense-in-depth"
          " for any that slip past the in-page JS filter) → classify drops them, no sell pass:")
    noise_texts = [
        "Number of unread notifications20+",
        "Marketplace 3 new messages",
        "Marketplace · Within 1 kilometer · 3 new messages",
        "Singapore · Within 1 kilometer",
    ]
    for text in noise_texts:
        row = inbox_scan.parse_fb_row({"text": text, "unread": True})
        check(f"noise row → empty handle: {text[:40]!r}", row["handle"] == "")
    # And classify drops empty-handle rows (no sell pass even when marked unread).
    rows = [inbox_scan.parse_fb_row({"text": t, "unread": True}) for t in noise_texts]
    out = inbox_scan.classify(_fb(rows), {}, {}, {})
    check("no sell pass from noise-only inbox", out["sell_markets"].get("fb") is False)
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


def test_sell_threads_new_wrapper_and_failopen():
    print("Fix C: sell_threads_new()/sell_markets_new() are thin wrappers over the same _peek:")
    saved = inbox_scan._peek
    try:
        inbox_scan._peek = lambda path: {
            "buy": [], "sell_markets": {"carousell": True, "fb": False},
            "sell_threads": {"carousell": ["carousell:9"], "fb": []}, "next_memo": {}}
        check("sell_threads_new returns {market:[thread_id,...]}",
              inbox_scan.sell_threads_new() == {"carousell": ["carousell:9"], "fb": []})
        check("sell_markets_new still returns the bool contract",
              inbox_scan.sell_markets_new() == {"carousell": True, "fb": False})
        # Fail-open: a _peek that omits sell_threads (older shape) must not crash the wrapper.
        inbox_scan._peek = lambda path: {"buy": [], "sell_markets": {}, "next_memo": {}}
        check("missing sell_threads → empty dict (fail-open)", inbox_scan.sell_threads_new() == {})
    finally:
        inbox_scan._peek = saved


def test_sell_peek_single_peek_returns_both_signals():
    print("C-followup: sell_peek() does ONE _peek and returns BOTH sell_markets + sell_threads"
          " (so the poll path advances the SELL memo once and reuses the result for both):")
    saved = inbox_scan._peek
    calls = []
    try:
        def _one(path):
            calls.append(path)
            return {"buy": [], "sell_markets": {"fb": True, "carousell": False},
                    "sell_threads": {"fb": ["fb:9988"], "carousell": []}, "next_memo": {}}
        inbox_scan._peek = _one
        out = inbox_scan.sell_peek()
        check("exactly ONE _peek (no double memo-advance)", len(calls) == 1)
        check("_peek hit the SELL memo path", calls[0] == inbox_scan.SELL_MEMO_PATH)
        check("carries sell_markets bool contract",
              out["sell_markets"] == {"fb": True, "carousell": False})
        check("carries sell_threads {market:[id,...]} contract",
              out["sell_threads"] == {"fb": ["fb:9988"], "carousell": []})
        # Fail-open: an older-shape _peek omitting sell_threads must not crash sell_peek.
        inbox_scan._peek = lambda path: {"buy": [], "sell_markets": {"fb": True}, "next_memo": {}}
        out2 = inbox_scan.sell_peek()
        check("missing sell_threads → empty dict (fail-open)",
              out2["sell_threads"] == {} and out2["sell_markets"] == {"fb": True})
    finally:
        inbox_scan._peek = saved


def test_poll_path_single_fresh_thread_yields_both_signals_no_double_advance():
    print("C-followup end-to-end: in the poll path a single fresh tracked FB thread yields BOTH"
          " sell_markets['fb']=True AND a non-None peek-thread hint (today's bug: the hint is None"
          " because buyer_peek then buyer_peek_thread advance the SELL memo TWICE):")
    import agent_daemon  # noqa: PLC0415
    import buyer_peek  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        sell_memo = d / "inbox_sell_state.json"
        # One tracked, ACTIVE FB sell thread; an inbox row whose preview changed since the memo.
        rows = _fb([_row("olaf", "Glass Kettle can you meet today?5m", True)])
        sell_index = {("fb", "olaf"): ["fb:9988"]}
        saved = (inbox_scan.SELL_MEMO_PATH, inbox_scan.scan, inbox_scan.build_sell_index,
                 inbox_scan.build_buy_index, buyer_peek.list_page_targets,
                 buyer_peek.enabled_markets, buyer_peek.probe_market)
        try:
            inbox_scan.SELL_MEMO_PATH = sell_memo
            inbox_scan.scan = lambda markets: rows
            inbox_scan.build_sell_index = lambda *a, **k: sell_index
            inbox_scan.build_buy_index = lambda *a, **k: {}
            buyer_peek.list_page_targets = lambda *a, **k: []
            buyer_peek.enabled_markets = lambda: ["fb"]
            buyer_peek.probe_market = lambda *a, **k: {"found": True, "count": 1, "snippet": "x"}

            # Step 1: the poll path's buyer_peek() — advances the SELL memo ONCE and surfaces both.
            bp = buyer_peek.peek(update_memo=True)
            check("sell_markets['fb'] is True (freshness gate fires)",
                  bp["markets"]["fb"]["new"] is True)
            # Step 2: the hint is derived from THAT result — NOT a second advancing probe.
            hint = agent_daemon.peek_thread_from(bp)
            check("peek-thread hint is non-None (was wrongly None on the double advance)",
                  hint == "fb:9988")
            # Prove the OLD behavior was the bug: a SECOND sell_threads_new() (what the old poll path
            # ran via buyer_peek_thread) now sees the already-advanced memo and returns no fresh thread.
            check("a second advancing probe (old bug path) would null the hint",
                  inbox_scan.sell_threads_new().get("fb", []) == [])
        finally:
            (inbox_scan.SELL_MEMO_PATH, inbox_scan.scan, inbox_scan.build_sell_index,
             inbox_scan.build_buy_index, buyer_peek.list_page_targets,
             buyer_peek.enabled_markets, buyer_peek.probe_market) = saved


def test_build_sell_index_only_active():
    print("build_sell_index indexes only active threads by buyer_handle:")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write(d, "carousell:10.json", {"thread_id": "carousell:10", "buyer_handle": "Truewolf.5feb9c",
                                        "status": "active"})
        _write(d, "carousell:11.json", {"thread_id": "carousell:11", "buyer_handle": "wuzen22",
                                        "status": "sold"})
        idx = inbox_scan.build_sell_index(d)
        check("active indexed (market-scoped, normalized)",
              idx.get(("carousell", "truewolf.5feb9c")) == ["carousell:10"])
        check("sold NOT indexed", ("carousell", "wuzen22") not in idx)


def test_carousell_row_enum_abstains_off_inbox():
    print("carousell row-enum ABSTAINS off /inbox → scan_market found:False (caller uses the count):")
    # The probe now matches any carousell tab (the count badge is global), but the conversation ROWS
    # only exist on /inbox/. Off-inbox the JS returns a NON-LIST (null) so scan_market reports
    # found:False — the market drops out of the PRECISE classifier and the caller falls back to the
    # global count (an empty-rows [] would instead assert "clear" and could strand a real unread).
    js = inbox_scan.CAROUSELL_ROW_ENUM_JS
    check("row-enum JS gates on the /inbox pathname", "/\\/inbox/.test(location.pathname)" in js)
    check("row-enum JS returns null (abstain) when off-inbox", "return null" in js)

    saved = (inbox_scan.bp._find_tab, inbox_scan.bp.cdp_eval)
    inbox_scan.bp._find_tab = lambda targets, probe: {"url": "https://www.carousell.sg/p/x-1/",
                                                      "webSocketDebuggerUrl": "ws://x"}
    inbox_scan.bp.cdp_eval = lambda ws, js: None  # JS abstained (off-inbox) → non-list
    try:
        out = inbox_scan.scan_market("carousell", [{"url": "https://www.carousell.sg/p/x-1/"}])
        check("scan_market abstains (found False) on a non-list result", out["found"] is False)
        check("no rows surfaced", out["rows"] == [])
    finally:
        inbox_scan.bp._find_tab, inbox_scan.bp.cdp_eval = saved


if __name__ == "__main__":
    print("inbox_scan tests\n")
    test_carousell_row_enum_abstains_off_inbox()
    test_is_fresh()
    test_is_fresh_tracked_snippet_change_overrides_read()
    test_normalize_snippet_key()
    test_is_fresh_normalizes_volatile_timestamp_no_refire()
    test_classify_tracked_sell_snippet_change_unbolded_fires()
    test_classify_tracked_buy_snippet_change_unbolded_fires()
    test_classify_tracked_no_refire_on_clock_tick()
    test_is_fresh_unread_transition_fires_on_same_snippet()
    test_classify_unread_transition_fires()
    test_fb_noise_regex_keeps_real_previews()
    test_fb_noise_regex_still_drops_genuine_noise()
    test_build_sell_index_market_scoped_collision()
    test_classify_collision_no_crossmarket_hint()
    test_classify_ambiguous_same_market_handle_no_hint()
    test_buy_reply_fires_buy_only()
    test_sell_tracked_thread_fires_sell_only()
    test_classify_surfaces_matched_sell_thread_id()
    test_classify_new_enquiry_has_no_thread_id()
    test_classify_multiple_matched_sell_threads()
    test_new_enquiry_fires_sell()
    test_system_handle_ignored()
    test_read_row_ignored()
    test_memo_suppresses_refire()
    test_buy_precedence_over_sell()
    test_next_memo_advances_for_all_rows()
    test_unscanned_market_absent()
    test_parse_fb_row()
    test_parse_fb_row_splits_on_first_separator()
    test_fb_classify_new_enquiry_fires_sell()
    test_fb_known_buy_thread_precedence()
    test_fb_read_row_ignored()
    test_fb_noise_rows_excluded()
    test_sell_threads_new_wrapper_and_failopen()
    test_sell_peek_single_peek_returns_both_signals()
    test_poll_path_single_fresh_thread_yields_both_signals_no_double_advance()
    test_build_buy_index_only_liaise()
    test_build_sell_index_only_active()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
