#!/usr/bin/env python3
"""Tests for tab_park.py — keeping notification-path tabs backgrounded.

    python3 tests/test_tab_park.py

Covers pick_parking (which tab to bring forward) and needs_park (whether to park
at all — only when a Meta tab is actually frontmost, so an idle warm Chrome with
only poll-path tabs never steals focus).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import tab_park  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _t(url):
    return {"url": url, "webSocketDebuggerUrl": "ws://x"}


def test_parks_on_non_notification_tab():
    print("picks a non-notification tab so the Meta tab goes hidden:")
    targets = [_t("https://www.facebook.com/marketplace/inbox"),
               _t("https://www.carousell.sg/inbox/")]
    pick = tab_park.pick_parking(targets)
    check("parks on carousell (hides facebook)", pick and "carousell" in pick["url"])


def test_no_parking_tab_when_only_notification_tabs():
    print("only notification-path tabs open → no parking target (caller no-ops):")
    targets = [_t("https://www.facebook.com/x"), _t("https://www.instagram.com/")]
    check("returns None", tab_park.pick_parking(targets) is None)


def test_ignores_blank_urls():
    print("a tab with no URL is not chosen:")
    targets = [{"url": "", "webSocketDebuggerUrl": "ws://x"},
               _t("https://www.carousell.sg/")]
    pick = tab_park.pick_parking(targets)
    check("skips blank, picks carousell", pick and "carousell" in pick["url"])


def test_park_never_raises():
    print("park() is fail-open (returns a bool, never raises) even with no Chrome:")
    saved = tab_park.bp.list_page_targets
    tab_park.bp.list_page_targets = lambda *a, **k: []
    try:
        check("no tabs → False, no raise", tab_park.park() is False)
    finally:
        tab_park.bp.list_page_targets = saved


# --- needs_park: only park when a notification-path tab is actually in front ---
# list_page_targets() returns /json/list page targets MRU-first, so targets[0] is the
# active tab. We only re-hide a Meta tab when it is the one currently frontmost.

FB = _t("https://www.facebook.com/marketplace/inbox")
IG = _t("https://www.instagram.com/direct/inbox/")
CAR = _t("https://www.carousell.sg/inbox/")


def test_needs_park_false_when_no_tabs():
    print("needs_park: no tabs → False (nothing to hide):")
    check("empty list", tab_park.needs_park([]) is False)


def test_needs_park_false_when_only_pollpath_tab():
    print("needs_park: only a poll-path (Carousell) tab → False (the reported bug):")
    check("carousell only", tab_park.needs_park([CAR]) is False)


def test_needs_park_true_when_meta_tab_frontmost():
    print("needs_park: a Meta tab is frontmost → True (must be backgrounded):")
    check("fb in front", tab_park.needs_park([FB, CAR]) is True)
    check("ig in front", tab_park.needs_park([IG, CAR]) is True)


def test_needs_park_false_when_meta_open_but_not_frontmost():
    print("needs_park: Meta open but a non-Meta tab is already frontmost → False:")
    check("carousell front, fb behind", tab_park.needs_park([CAR, FB]) is False)


def test_needs_park_skips_targets_without_url():
    print("needs_park: a leading target with no url is ignored, falls to next page tab:")
    no_url = {"webSocketDebuggerUrl": "ws://x"}
    check("no-url then fb → True", tab_park.needs_park([no_url, FB]) is True)
    check("no-url then carousell → False", tab_park.needs_park([no_url, CAR]) is False)


def test_park_no_ops_when_only_pollpath_tab():
    print("park(): Carousell-only warm Chrome → no CDP bringToFront (no focus steal):")
    saved_list, saved_cdp = tab_park.bp.list_page_targets, tab_park._cdp_call
    calls = []
    tab_park.bp.list_page_targets = lambda *a, **k: [CAR]
    tab_park._cdp_call = lambda *a, **k: calls.append(a)
    try:
        result = tab_park.park()
        check("returns False", result is False)
        check("Page.bringToFront never sent", calls == [])
    finally:
        tab_park.bp.list_page_targets, tab_park._cdp_call = saved_list, saved_cdp


def test_park_acts_once_when_meta_frontmost():
    print("park(): Meta tab frontmost → exactly one bringToFront on the non-Meta tab:")
    saved_list, saved_cdp = tab_park.bp.list_page_targets, tab_park._cdp_call
    calls = []
    tab_park.bp.list_page_targets = lambda *a, **k: [FB, CAR]
    tab_park._cdp_call = lambda ws, method, *a, **k: calls.append((ws, method))
    try:
        result = tab_park.park()
        check("returns True", result is True)
        check("one bringToFront on carousell", calls == [(CAR["webSocketDebuggerUrl"], "Page.bringToFront")])
    finally:
        tab_park.bp.list_page_targets, tab_park._cdp_call = saved_list, saved_cdp


if __name__ == "__main__":
    print("tab_park tests\n")
    test_parks_on_non_notification_tab()
    test_no_parking_tab_when_only_notification_tabs()
    test_ignores_blank_urls()
    test_park_never_raises()
    test_needs_park_false_when_no_tabs()
    test_needs_park_false_when_only_pollpath_tab()
    test_needs_park_true_when_meta_tab_frontmost()
    test_needs_park_false_when_meta_open_but_not_frontmost()
    test_needs_park_skips_targets_without_url()
    test_park_no_ops_when_only_pollpath_tab()
    test_park_acts_once_when_meta_frontmost()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
