#!/usr/bin/env python3
"""Tests for bin/hooks/update_notice.py — the SessionStart update-notice hook.

    python3 tests/test_update_notice_hook.py

Runs the hook as a real subprocess against a seeded, isolated BAZAAR_CONFIG_DIR (a fresh cached
update_check result, so no git/network is touched). Verifies: no-op for daemon passes, emits
additionalContext when an update is available, stays silent with no update / a dismissed version.
"""

import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "hooks" / "update_notice.py"

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _seed(cfg_dir, *, update_available, latest="9.9.9", declined_latest=None):
    """Write a fresh cached update_state.json so `update_check check` returns it without hitting git."""
    state = {
        "last_check_ts": time.time(),  # fresh -> within TTL -> cached path, no fetch
        "last_result": {"update_available": update_available, "current": "0.2.0",
                        "latest": latest, "behind_by": 5 if update_available else 0,
                        "branch": "origin/main", "reason": "ok"},
    }
    if declined_latest is not None:
        # Mirror run_snooze: a dismiss writes BOTH the version AND an active snooze window.
        state["declined_latest"] = declined_latest
        state["snooze_until_ts"] = time.time() + 30 * 86400
    (Path(cfg_dir) / "update_state.json").write_text(json.dumps(state))


def _run_hook(cfg_dir, *, daemon=False, data_dir=None):
    env = {**os.environ, "BAZAAR_CONFIG_DIR": str(cfg_dir)}
    if data_dir is not None:
        env["BAZAAR_DATA_DIR"] = str(data_dir)
    if daemon:
        env["BAZAAR_DAEMON_PASS"] = "1"
    else:
        env.pop("BAZAAR_DAEMON_PASS", None)
    return subprocess.run([sys.executable, str(HOOK)],
                          input='{"hook_event_name":"SessionStart","source":"startup"}',
                          capture_output=True, text=True, env=env, timeout=30)


def test_daemon_passes_are_noop():
    print("no-op inside a daemon headless pass:")
    with tempfile.TemporaryDirectory() as cfg:
        _seed(cfg, update_available=True)  # update IS available...
        out = _run_hook(cfg, daemon=True)  # ...but this is a daemon pass
        check("exit 0", out.returncode == 0)
        check("emits NOTHING (no context injected for -p)", out.stdout.strip() == "")


def test_emits_when_update_available():
    print("injects additionalContext when an update is available (interactive):")
    with tempfile.TemporaryDirectory() as cfg:
        _seed(cfg, update_available=True, latest="9.9.9")
        out = _run_hook(cfg)
        check("exit 0", out.returncode == 0)
        try:
            payload = json.loads(out.stdout)
        except ValueError:
            payload = {}
        ctx = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("hookEventName is SessionStart",
              payload.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart")
        check("note offers /bazaar-upgrade", "/bazaar-upgrade" in ctx)
        check("note carries the version delta", "9.9.9" in ctx and "0.2.0" in ctx)


def test_silent_when_no_update():
    print("silent when up to date:")
    with tempfile.TemporaryDirectory() as cfg:
        _seed(cfg, update_available=False)
        out = _run_hook(cfg)
        check("no output", out.stdout.strip() == "")


def test_silent_when_version_dismissed():
    print("silent when this version was already dismissed (snoozed):")
    with tempfile.TemporaryDirectory() as cfg:
        _seed(cfg, update_available=True, latest="9.9.9", declined_latest="9.9.9")
        out = _run_hook(cfg)
        check("dismissed version -> no output", out.stdout.strip() == "")


def test_fail_open_on_garbage_state():
    print("fail-open: a corrupt state file never breaks the session:")
    with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as data:
        (Path(cfg) / "update_state.json").write_text("{not json")
        # Disable the network check (interval 0) so this stays hermetic — corrupt state would
        # otherwise read as due and trigger a real git fetch.
        (Path(data) / "config.json").write_text(json.dumps({"update_check_interval_hours": 0}))
        out = _run_hook(cfg, data_dir=data)
        check("exit 0", out.returncode == 0)
        check("no output (disabled / unusable cache)", out.stdout.strip() == "")
        check("no crash / no traceback", "Traceback" not in out.stderr)


if __name__ == "__main__":
    print("update_notice.py (SessionStart hook) tests\n")
    test_daemon_passes_are_noop()
    test_emits_when_update_available()
    test_silent_when_no_update()
    test_silent_when_version_dismissed()
    test_fail_open_on_garbage_state()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
