#!/usr/bin/env python3
"""Tests for daemon_watchdog.py — the independent restart-on-stall watchdog.

    python3 tests/test_daemon_watchdog.py

The watchdog runs on a StartInterval (NOT KeepAlive), is read-only, and restarts the agent ONLY
when launchd thinks the job is loaded but its loop heartbeat went stale or its lock holder died.
We test the pure should_restart truth table + that main() kickstarts exactly once when (and only
when) a restart is warranted, and never crashes on an unreadable heartbeat.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import daemon_watchdog  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_should_restart_truth_table():
    print("should_restart: restart only when LOADED and (heartbeat stale OR holder dead):")
    stale = daemon_watchdog.STALE_SEC
    # Not loaded → never restart, regardless of age/holder.
    check("not loaded → no restart", daemon_watchdog.should_restart(False, stale + 999, False) is False)
    # Loaded + fresh + holder alive → healthy, no restart.
    check("loaded, fresh, alive → no restart",
          daemon_watchdog.should_restart(True, 5.0, True) is False)
    # Loaded + stale heartbeat → restart.
    check("loaded + stale heartbeat → restart",
          daemon_watchdog.should_restart(True, stale + 1, True) is True)
    # Loaded + holder dead → restart even if heartbeat looks fresh.
    check("loaded + holder dead → restart",
          daemon_watchdog.should_restart(True, 5.0, False) is True)
    # Loaded but heartbeat age unknown (None) + holder alive → no restart (can't prove a stall).
    check("loaded + unknown age + alive → no restart",
          daemon_watchdog.should_restart(True, None, True) is False)
    # Loaded + unknown age + holder dead → restart.
    check("loaded + unknown age + holder dead → restart",
          daemon_watchdog.should_restart(True, None, False) is True)


def _patch_main(monkey):
    """Apply a dict of {name: fn} onto daemon_watchdog, return the saved originals."""
    saved = {}
    for name, fn in monkey.items():
        saved[name] = getattr(daemon_watchdog, name)
        setattr(daemon_watchdog, name, fn)
    return saved


def _restore(saved):
    for name, fn in saved.items():
        setattr(daemon_watchdog, name, fn)


def test_main_kickstarts_once_when_stalled():
    print("main(): when loaded + stalled, it kickstarts the agent EXACTLY once and exits 0:")
    kicks = []
    saved = _patch_main({
        "_agent_loaded": lambda: True,
        "_heartbeat_age": lambda: daemon_watchdog.STALE_SEC + 100,
        "_holder_alive": lambda: True,
        "_kickstart": lambda: kicks.append(True),
    })
    try:
        rc = daemon_watchdog.main(["daemon_watchdog.py"])
        check("exits 0", rc == 0)
        check("kickstart called exactly once", kicks == [True])
    finally:
        _restore(saved)


def test_main_no_kick_when_healthy():
    print("main(): a healthy daemon (loaded, fresh, holder alive) is NOT restarted:")
    kicks = []
    saved = _patch_main({
        "_agent_loaded": lambda: True,
        "_heartbeat_age": lambda: 5.0,
        "_holder_alive": lambda: True,
        "_kickstart": lambda: kicks.append(True),
    })
    try:
        rc = daemon_watchdog.main(["daemon_watchdog.py"])
        check("exits 0", rc == 0)
        check("kickstart never called", kicks == [])
    finally:
        _restore(saved)


def test_main_no_kick_when_not_loaded():
    print("main(): if the agent LaunchAgent isn't loaded, the watchdog leaves it alone:")
    kicks = []
    saved = _patch_main({
        "_agent_loaded": lambda: False,
        "_heartbeat_age": lambda: daemon_watchdog.STALE_SEC + 100,
        "_holder_alive": lambda: False,
        "_kickstart": lambda: kicks.append(True),
    })
    try:
        rc = daemon_watchdog.main(["daemon_watchdog.py"])
        check("exits 0", rc == 0)
        check("kickstart never called (not loaded → not ours to restart)", kicks == [])
    finally:
        _restore(saved)


def test_main_survives_unreadable_heartbeat():
    print("main(): an unreadable/missing heartbeat (age None) never crashes; exit 0:")
    kicks = []
    saved = _patch_main({
        "_agent_loaded": lambda: True,
        "_heartbeat_age": lambda: None,
        "_holder_alive": lambda: True,  # holder alive + unknown age → no restart
        "_kickstart": lambda: kicks.append(True),
    })
    try:
        rc = daemon_watchdog.main(["daemon_watchdog.py"])
        check("exits 0 (no crash)", rc == 0)
        check("no kickstart (can't prove a stall)", kicks == [])
    finally:
        _restore(saved)


if __name__ == "__main__":
    print("daemon_watchdog tests\n")
    test_should_restart_truth_table()
    test_main_kickstarts_once_when_stalled()
    test_main_no_kick_when_healthy()
    test_main_no_kick_when_not_loaded()
    test_main_survives_unreadable_heartbeat()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
