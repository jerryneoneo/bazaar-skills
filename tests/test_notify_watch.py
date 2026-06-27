#!/usr/bin/env python3
"""Tests for notify_watch.py — the notification-path trigger (select_new is pure).

    python3 tests/test_notify_watch.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import notify_watch as nw  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


NOW = "2026-06-27T12:00:00"


def _n(rec_id, origin, title="Buyer", body="hi", ts=NOW):
    return {"rec_id": rec_id, "origin": origin, "title": title, "body": body, "ts": ts}


def test_fresh_fb_notification_fires_once():
    print("a new FB-origin notification → pending + content hint, cursor advances:")
    notifs = [_n(1582, "www.facebook.com", "Baba Mahamed", "Hi, is this available?")]
    res = nw.select_new(["fb", "carousell"], notifs, {}, NOW)
    check("fb pending", res["markets"].get("fb", {}).get("pending") == 1)
    check("hint carries sender + body", "Baba Mahamed" in res["latest_text"]
          and "available" in res["latest_text"])
    check("cursor advanced to rec_id", res["next_state"]["fb"] == 1582)
    check("carousell skipped (poll path, no fb-only effect)", "carousell" not in res["markets"])


def test_already_seen_does_not_refire():
    print("a notification at/below the cursor does not re-fire (idempotent):")
    notifs = [_n(1582, "www.facebook.com")]
    res = nw.select_new(["fb"], notifs, {"fb": 1582}, NOW)
    check("not pending", res["pending"] == 0)
    check("cursor stays put", res["next_state"]["fb"] == 1582)


def test_poll_path_market_skipped():
    print("a market with no notifications resolves to poll → skipped here:")
    # carousell has no notifications at all → resolve() = poll → select_new ignores it
    notifs = [_n(1582, "www.facebook.com")]
    res = nw.select_new(["carousell"], notifs, {}, NOW)
    check("nothing pending for poll-path market", res["pending"] == 0)
    check("no carousell entry", "carousell" not in res["markets"])


def test_no_notifications_failopen():
    print("empty notification list → pending 0 (fail-open):")
    res = nw.select_new(["fb", "carousell"], [], {}, NOW)
    check("pending 0", res["pending"] == 0)
    check("markets empty", res["markets"] == {})


def test_watch_contract_never_raises():
    print("watch() returns the buyer_peek-style contract and never raises:")
    out = nw.watch(["fb"], now_iso=NOW, update=False)
    check("has pending/markets/latest_text", all(k in out for k in ("pending", "markets", "latest_text")))
    check("pending is int", isinstance(out["pending"], int))


if __name__ == "__main__":
    print("notify_watch tests\n")
    test_fresh_fb_notification_fires_once()
    test_already_seen_does_not_refire()
    test_poll_path_market_skipped()
    test_no_notifications_failopen()
    test_watch_contract_never_raises()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
