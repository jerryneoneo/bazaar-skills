#!/usr/bin/env python3
"""Tests for tab_park.py — keeping notification-path tabs backgrounded (pick_parking is pure).

    python3 tests/test_tab_park.py
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


if __name__ == "__main__":
    print("tab_park tests\n")
    test_parks_on_non_notification_tab()
    test_no_parking_tab_when_only_notification_tabs()
    test_ignores_blank_urls()
    test_park_never_raises()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
