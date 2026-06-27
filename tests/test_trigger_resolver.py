#!/usr/bin/env python3
"""Tests for trigger_resolver.py — per-platform trigger path (notification vs polling).

    python3 tests/test_trigger_resolver.py

The resolver is EMPIRICAL and pure: a platform is on the notification path only if a readable OS
notification from its origin actually arrived within the viability window; otherwise it polls
(the safe default). No live DB here; we feed synthetic notification lists.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import trigger_resolver as tr  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


NOW = "2026-06-27T12:00:00"


def _notif(origin, ts):
    return {"origin": origin, "title": "x", "body": "y", "ts": ts}


def test_no_notifications_polls():
    print("no readable notifications at all → every platform polls (safe default):")
    check("fb polls", tr.resolve("fb", [], NOW) == "poll")
    check("carousell polls", tr.resolve("carousell", [], NOW) == "poll")


def test_recent_origin_notification_enables_path():
    print("a recent notification from the platform's origin → notification path:")
    notifs = [_notif("www.facebook.com", "2026-06-27T11:30:00")]
    check("fb on notification path", tr.resolve("fb", notifs, NOW) == "notification")
    check("carousell still polls (no carousell notif)", tr.resolve("carousell", notifs, NOW) == "poll")


def test_stale_notification_falls_back_to_poll():
    print("an OLD notification (outside the window) does not enable the path:")
    notifs = [_notif("www.facebook.com", "2026-06-01T00:00:00")]
    check("stale fb → poll", tr.resolve("fb", notifs, NOW, window_hours=168) == "poll")


def test_origin_substring_match():
    print("origin match is substring-tolerant (www. prefix, regional TLDs):")
    check("carousell.sg matches", tr.notification_viable(
        "carousell", [_notif("www.carousell.sg", NOW)], NOW))
    check("ebay regional matches", tr.notification_viable(
        "ebay", [_notif("www.ebay.com.sg", NOW)], NOW))
    check("unrelated origin does not match",
          not tr.notification_viable("fb", [_notif("www.pinterest.com", NOW)], NOW))


def test_unknown_platform_polls():
    print("an unknown platform has no origin mapping → polls:")
    check("unknown → poll", tr.resolve("depop", [_notif("www.depop.com", NOW)], NOW) == "poll")


if __name__ == "__main__":
    print("trigger_resolver tests\n")
    test_no_notifications_polls()
    test_recent_origin_notification_enables_path()
    test_stale_notification_falls_back_to_poll()
    test_origin_substring_match()
    test_unknown_platform_polls()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
