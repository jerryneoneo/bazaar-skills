#!/usr/bin/env python3
"""Tests for bin/hooks/onboarding_notice.py — the SessionStart self-heal hook.

    python3 tests/test_onboarding_notice.py

Runs the hook as a real subprocess against an isolated SELLY_DATA_DIR. Verifies: it offers
onboarding when data/seller_config.json is absent, stays silent once it exists, and is a no-op
inside the daemon's headless passes.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "hooks" / "onboarding_notice.py"

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _run_hook(data_dir, *, daemon=False):
    env = {**os.environ, "SELLY_DATA_DIR": str(data_dir)}
    if daemon:
        env["SELLY_DAEMON_PASS"] = "1"
    else:
        env.pop("SELLY_DAEMON_PASS", None)
    return subprocess.run([sys.executable, str(HOOK)],
                          input='{"hook_event_name":"SessionStart","source":"startup"}',
                          capture_output=True, text=True, env=env, timeout=30)


def test_offers_onboarding_when_not_configured():
    print("offers onboarding when seller_config.json is absent:")
    with tempfile.TemporaryDirectory() as data:
        out = _run_hook(data)  # empty data dir -> not onboarded
        check("exit 0", out.returncode == 0)
        try:
            payload = json.loads(out.stdout)
        except ValueError:
            payload = {}
        ctx = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("hookEventName is SessionStart",
              payload.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart")
        check("note points at selly-install runbook", ".claude/commands/selly-install.md" in ctx)
        check("note offers /selly-install", "/selly-install" in ctx)


def test_silent_when_onboarded():
    print("silent once seller_config.json exists:")
    with tempfile.TemporaryDirectory() as data:
        (Path(data) / "seller_config.json").write_text(json.dumps({"region": "SG"}))
        out = _run_hook(data)
        check("exit 0", out.returncode == 0)
        check("no output (already onboarded)", out.stdout.strip() == "")


def test_daemon_passes_are_noop():
    print("no-op inside a daemon headless pass:")
    with tempfile.TemporaryDirectory() as data:
        out = _run_hook(data, daemon=True)  # not onboarded, but a daemon pass
        check("exit 0", out.returncode == 0)
        check("emits NOTHING (no context injected for -p)", out.stdout.strip() == "")


def test_fail_open_no_crash():
    print("fail-open: never wedges the session:")
    with tempfile.TemporaryDirectory() as data:
        out = _run_hook(data)
        check("no crash / no traceback", "Traceback" not in out.stderr)


if __name__ == "__main__":
    print("onboarding_notice.py (SessionStart hook) tests\n")
    test_offers_onboarding_when_not_configured()
    test_silent_when_onboarded()
    test_daemon_passes_are_noop()
    test_fail_open_no_crash()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
