#!/usr/bin/env python3
"""Tests for wait_cdp.py — the CDP readiness poll (no real Chrome, no wall-clock waits).

    python3 tests/test_wait_cdp.py

The clock/probe/sleep are injected so we can simulate Chrome coming up on the Nth attempt and a
never-up timeout deterministically.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import wait_cdp  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


class _Clock:
    """A fake monotonic clock that only advances when sleep() is called."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, secs):
        self.t += secs


def test_ready_immediately():
    print("ready on the first probe:")
    clk = _Clock()
    res = wait_cdp.wait_for_cdp("http://x", timeout=30, interval=0.5,
                                probe=lambda u, **k: {"Browser": "Chrome/123"},
                                sleep=clk.sleep, monotonic=clk.monotonic)
    check("ready true", res["ready"] is True)
    check("one attempt", res["attempts"] == 1)
    check("no waiting", res["waited_sec"] == 0.0)
    check("browser surfaced", res["browser"] == "Chrome/123")


def test_ready_after_a_few_tries():
    print("Chrome comes up on the 3rd probe:")
    clk = _Clock()
    calls = {"n": 0}

    def probe(url, **k):
        calls["n"] += 1
        return {"Browser": "Chrome/123"} if calls["n"] >= 3 else None

    res = wait_cdp.wait_for_cdp("http://x", timeout=30, interval=0.5,
                                probe=probe, sleep=clk.sleep, monotonic=clk.monotonic)
    check("ready true", res["ready"] is True)
    check("took 3 attempts", res["attempts"] == 3)
    check("waited ~2 intervals", abs(res["waited_sec"] - 1.0) < 0.01)


def test_times_out():
    print("Chrome never comes up -> timeout:")
    clk = _Clock()
    res = wait_cdp.wait_for_cdp("http://x", timeout=2.0, interval=0.5,
                                probe=lambda u, **k: None,
                                sleep=clk.sleep, monotonic=clk.monotonic)
    check("ready false", res["ready"] is False)
    check("browser is null", res["browser"] is None)
    check("gave up at/after the timeout", res["waited_sec"] >= 2.0)


def test_exit_codes():
    print("CLI exit codes:")
    orig = wait_cdp.wait_for_cdp  # main() resolves this by module-global name -> patchable
    wait_cdp.wait_for_cdp = lambda *a, **k: {"ready": True, "waited_sec": 0.0,
                                            "attempts": 1, "browser": "Chrome/1"}
    try:
        check("ready -> exit 0", wait_cdp.main(["wait_cdp.py", "--timeout", "1"]) == 0)
    finally:
        wait_cdp.wait_for_cdp = orig
    # A port nothing listens on, zero timeout -> exit 3 (real probe + main integration, fast).
    check("never-up -> exit 3",
          wait_cdp.main(["wait_cdp.py", "--port", "1", "--timeout", "0", "--interval", "0.01"]) == 3)


if __name__ == "__main__":
    print("wait_cdp.py tests\n")
    test_ready_immediately()
    test_ready_after_a_few_tries()
    test_times_out()
    test_exit_codes()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
