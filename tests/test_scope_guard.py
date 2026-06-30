#!/usr/bin/env python3
"""Tests for scope_guard.py — the PreToolUse hard backstop for per-marketplace tab scope.

Runnable with plain python:  python3 tests/test_scope_guard.py

Invariant: when $SELLY_RESOURCE is set (a concurrent per-marketplace worker), a browser_navigate
to ANOTHER marketplace's host is DENIED; the worker's own host (any region) and non-marketplace
hosts are allowed; unscoped passes (no $SELLY_RESOURCE) are never affected. Fail-open on errors.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "hooks" / "scope_guard.py"

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _run(resource, url, tool="mcp__playwright__browser_navigate"):
    env = dict(os.environ)
    env.pop("SELLY_RESOURCE", None)
    if resource is not None:
        env["SELLY_RESOURCE"] = resource
    event = json.dumps({"tool_name": tool, "tool_input": {"url": url}})
    out = subprocess.run([sys.executable, str(HOOK)], input=event,
                         capture_output=True, text=True, env=env)
    denied = '"permissionDecision": "deny"' in out.stdout
    return out.returncode, denied, out.stdout


def test_unscoped_never_blocks():
    print("unscoped pass (no $SELLY_RESOURCE) is never blocked:")
    rc, denied, _ = _run(None, "https://www.facebook.com/marketplace/inbox")
    check("rc 0", rc == 0)
    check("not denied", not denied)


def test_own_market_allowed():
    print("worker may navigate its OWN marketplace (any region):")
    _, d1, _ = _run("carousell", "https://www.carousell.sg/inbox")
    check("carousell worker → carousell.sg allowed", not d1)
    _, d2, _ = _run("carousell", "https://www.carousell.com.my/inbox")
    check("carousell worker → carousell.com.my (other region, same market) allowed", not d2)


def test_cross_marketplace_denied():
    print("worker may NOT navigate to ANOTHER marketplace (the dangerous case):")
    _, d1, out1 = _run("carousell", "https://www.facebook.com/marketplace/inbox")
    check("carousell worker → facebook DENIED", d1)
    _, d2, _ = _run("fb", "https://www.carousell.sg/inbox")
    check("fb worker → carousell DENIED", d2)
    _, d3, _ = _run("fb", "https://www.ebay.com.sg/itm/123")
    check("fb worker → ebay DENIED", d3)


def test_non_marketplace_host_allowed():
    print("non-marketplace hosts are not over-blocked:")
    _, denied, _ = _run("carousell", "https://www.google.com/search?q=ipad+price")
    check("carousell worker → google allowed (not a marketplace)", not denied)


def test_non_navigate_tool_allowed():
    print("the hook only judges navigation:")
    _, denied, _ = _run("carousell", "https://www.facebook.com/x", tool="mcp__playwright__browser_click")
    check("a non-navigate tool is allowed", not denied)


def test_malformed_input_fails_open():
    print("fail-open on bad input:")
    env = {**os.environ, "SELLY_RESOURCE": "carousell"}
    out = subprocess.run([sys.executable, str(HOOK)], input="not json",
                         capture_output=True, text=True, env=env)
    check("garbage stdin → allow (rc 0, no deny)", out.returncode == 0 and "deny" not in out.stdout)


if __name__ == "__main__":
    print("scope_guard tests\n")
    test_unscoped_never_blocks()
    test_own_market_allowed()
    test_cross_marketplace_denied()
    test_non_marketplace_host_allowed()
    test_non_navigate_tool_allowed()
    test_malformed_input_fails_open()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
