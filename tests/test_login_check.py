#!/usr/bin/env python3
"""Structural tests for login_check.py (no live Chrome).

    python3 tests/test_login_check.py

Covers the pure classification, the fail-open behaviour (no tab / CDP error / unknown market),
and that probe_market maps each marker to the right status — by stubbing buyer_peek's CDP transport.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import buyer_peek  # noqa: E402
import login_check  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_classify():
    print("classify (pure):")
    check("logged_in passes through", login_check.classify({"state": "logged_in"}) == "logged_in")
    check("logged_out passes through", login_check.classify({"state": "logged_out"}) == "logged_out")
    check("unknown passes through", login_check.classify({"state": "unknown"}) == "unknown")
    check("garbage state -> unknown", login_check.classify({"state": "weird"}) == "unknown")
    check("non-dict -> unknown", login_check.classify(None) == "unknown")
    check("missing key -> unknown", login_check.classify({}) == "unknown")


def _stub(targets, eval_result=None, eval_raises=False):
    """Stub buyer_peek's CDP transport; return a restore() callable."""
    orig_list, orig_eval = buyer_peek.list_page_targets, buyer_peek.cdp_eval
    buyer_peek.list_page_targets = lambda *a, **k: targets

    def fake_eval(ws_url, expression, timeout=6):
        if eval_raises:
            raise OSError("socket closed mid-frame")
        return eval_result

    buyer_peek.cdp_eval = fake_eval
    return lambda: (setattr(buyer_peek, "list_page_targets", orig_list),
                    setattr(buyer_peek, "cdp_eval", orig_eval))


def test_probe_market():
    print("probe_market (stubbed CDP):")
    fb_tab = [{"type": "page", "url": "https://www.facebook.com/marketplace/inbox/",
               "webSocketDebuggerUrl": "ws://x"}]

    restore = _stub(fb_tab, {"state": "logged_in"})
    try:
        check("authed marker -> logged_in",
              login_check.check_market("fb")["status"] == "logged_in")
    finally:
        restore()

    restore = _stub(fb_tab, {"state": "logged_out"})
    try:
        check("login form -> logged_out",
              login_check.check_market("fb")["status"] == "logged_out")
    finally:
        restore()

    restore = _stub(fb_tab, eval_raises=True)
    try:
        check("CDP error -> unknown (fail-open)",
              login_check.check_market("fb")["status"] == "unknown")
    finally:
        restore()

    # No tab open for the market -> unknown, never a false logged_out.
    restore = _stub([{"type": "page", "url": "https://example.com", "webSocketDebuggerUrl": "ws://y"}])
    try:
        res = login_check.check_market("carousell")
        check("no tab -> unknown", res["status"] == "unknown")
    finally:
        restore()

    # A market with no probe is unknown, not an error.
    restore = _stub([])
    try:
        check("unknown market -> unknown", login_check.check_market("nope")["status"] == "unknown")
    finally:
        restore()


def test_exit_codes():
    print("market-mode exit codes:")
    fb_tab = [{"type": "page", "url": "https://www.facebook.com/",
               "webSocketDebuggerUrl": "ws://x"}]
    for state, code in (("logged_in", 0), ("logged_out", 1), ("unknown", 3)):
        restore = _stub(fb_tab, {"state": state})
        try:
            rc = login_check.main(["login_check.py", "market", "fb"])
            check(f"{state} -> exit {code}", rc == code)
        finally:
            restore()
    check("bad usage -> exit 2", login_check.main(["login_check.py"]) == 2)


def test_check_all():
    print("all-mode aggregates enabled markets:")
    fb_tab = [{"type": "page", "url": "https://www.facebook.com/", "webSocketDebuggerUrl": "ws://x"}]
    restore = _stub(fb_tab, {"state": "logged_in"})
    try:
        out = login_check.check_all(["fb", "carousell"])
        check("both markets present", set(out["markets"]) == {"fb", "carousell"})
        check("fb logged_in", out["markets"]["fb"]["status"] == "logged_in")
        # carousell has no matching tab in this target set -> unknown (fail-open, no false claim).
        check("carousell unknown (no tab)", out["markets"]["carousell"]["status"] == "unknown")
    finally:
        restore()


if __name__ == "__main__":
    print("login_check.py structural tests\n")
    test_classify()
    test_probe_market()
    test_exit_codes()
    test_check_all()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
