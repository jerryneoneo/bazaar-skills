#!/usr/bin/env python3
"""Tests for notify_setup.py — Instant-mode setup/inspection (tab match + status shaping).

    python3 tests/test_notify_setup.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import notify_setup as ns  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _t(url):
    return {"url": url, "webSocketDebuggerUrl": "ws://x"}


def test_market_tab_matches_origins():
    print("_market_tab matches a market's push origins:")
    targets = [_t("https://www.facebook.com/marketplace/inbox"), _t("https://www.carousell.sg/")]
    check("fb tab found", ns._market_tab("fb", targets) is not None)
    check("ig tab absent → None", ns._market_tab("ig", targets) is None)


def _patch(fda, targets, eval_ret):
    saved = (ns.notify_db.available, ns.bp.list_page_targets, ns.bp.cdp_eval)
    ns.notify_db.available = lambda: fda
    ns.bp.list_page_targets = lambda *a, **k: targets
    ns.bp.cdp_eval = lambda *a, **k: eval_ret

    def restore():
        ns.notify_db.available, ns.bp.list_page_targets, ns.bp.cdp_eval = saved
    return restore


def test_status_instant_ready_when_fda_and_push():
    print("status: instant_ready when FDA on + a market granted with a push sub:")
    restore = _patch(True, [_t("https://www.facebook.com/")],
                     {"perm": "granted", "hasPushSub": True})
    try:
        s = ns.status()
        check("fda true", s["fda"] is True)
        check("fb permission granted", s["markets"]["fb"]["permission"] == "granted")
        check("instant_ready true", s["instant_ready"] is True)
    finally:
        restore()


def test_status_not_ready_without_fda():
    print("status: NOT ready without FDA even if a site is granted:")
    restore = _patch(False, [_t("https://www.facebook.com/")],
                     {"perm": "granted", "hasPushSub": True})
    try:
        check("instant_ready false (no FDA)", ns.status()["instant_ready"] is False)
    finally:
        restore()


def test_status_not_ready_without_push_permission():
    print("status: NOT ready when FDA on but no granted+subscribed market:")
    restore = _patch(True, [_t("https://www.facebook.com/")],
                     {"perm": "default", "hasPushSub": False})
    try:
        check("instant_ready false (no push grant)", ns.status()["instant_ready"] is False)
    finally:
        restore()


if __name__ == "__main__":
    print("notify_setup tests\n")
    test_market_tab_matches_origins()
    test_status_instant_ready_when_fda_and_push()
    test_status_not_ready_without_fda()
    test_status_not_ready_without_push_permission()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
