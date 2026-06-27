#!/usr/bin/env python3
"""Tests for the daemon's pause behavior — mid-flight interrupt + boundary-pause.

    python3 tests/test_pause_interrupt.py

Two claims:
  1. MID-FLIGHT INTERRUPT: a pause set while a pass is running terminates it within ~one poll
     cadence, and the single-flight run lock is released afterward (no stale lock). Uses a fake
     `claude` (CLAUDE_BIN seam, as test_eval_judge does) that just sleeps, so no real LLM runs.
  2. BOUNDARY-PAUSE: `agent_daemon.py --once --dry-run` with a paused control.json holds every
     background pass (buyer/buy/maint) and logs the pause edge; unpaused, the buyer gate runs.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import agent_daemon  # noqa: E402
import control  # noqa: E402

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _write_sleeper(tmp, marker, seconds=15):
    """A fake `claude` that records it started, then sleeps (so the pass 'runs long')."""
    path = Path(tmp) / "fake_claude.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import time, pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('1')\n"
        f"time.sleep({seconds})\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_paused_flag_terminates_running_pass():
    print("MID-FLIGHT: a pause set during a running pass terminates it (<=10s) + frees the lock:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        marker = Path(tmp) / "started"
        fake = _write_sleeper(tmp, marker, seconds=15)
        orig_lock = agent_daemon.RUN_LOCK
        agent_daemon.RUN_LOCK = Path(tmp) / ".daemon.runlock"
        env = {**os.environ, "CLAUDE_BIN": str(fake), "BAZAAR_HARNESS": "claude-code",
               "BAZAAR_DATA_DIR": tmp}
        channel = {"adapter": "console", "detail": {}}  # no telegram side-effects during the test
        result = {}

        def _run():
            t0 = time.monotonic()
            agent_daemon.run_pass("maint", channel, env, dry_run=False)
            result["elapsed"] = time.monotonic() - t0

        th = threading.Thread(target=_run, daemon=True)
        try:
            th.start()
            for _ in range(100):  # wait (≤10s) for the pass to actually start
                if marker.exists():
                    break
                time.sleep(0.1)
            check("pass actually started", marker.exists())
            check("run lock held while running", agent_daemon.RUN_LOCK.exists())
            control.pause(source="cli")          # ← interrupt mid-flight
            th.join(timeout=12)
            check("pass terminated after pause (not still running)", not th.is_alive())
            check("terminated within ~one poll cadence + margin", result.get("elapsed", 99) <= 10)
            check("run lock released after terminate", not agent_daemon.RUN_LOCK.exists())
        finally:
            agent_daemon.RUN_LOCK = orig_lock


def _run_daemon_once(data_dir):
    # Pin single-flight (BAZAAR_MAX_WORKERS=1): these boundary checks assert the single-flight
    # loop's pause/gate log lines, which differ from the concurrent supervisor's. The deployed
    # config may set max_concurrent_workers>1, so pin the path the assertions below were written
    # for rather than depending on the live config value.
    env = {**os.environ, "BAZAAR_DATA_DIR": data_dir, "TELEGRAM_BOT_TOKEN": "123:FAKE",
           "BAZAAR_HARNESS": "claude-code", "BAZAAR_MAX_WORKERS": "1"}
    out = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "agent_daemon.py"), "--once", "--dry-run",
         "--peek-timeout", "0"],
        capture_output=True, text=True, env=env, timeout=90)
    return out.stdout + out.stderr


def test_daemon_holds_background_passes_when_paused():
    print("BOUNDARY: --once --dry-run holds all background passes while paused:")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "control.json").write_text(json.dumps({
            "paused": True, "since": "2026-06-26T00:00:00Z", "source": "cli",
            "reason": "", "corrections": []}))
        log = _run_daemon_once(tmp)
        check("startup line shows paused=True", "paused=True" in log)
        check("logs the daemon PAUSED edge", "daemon PAUSED" in log)
        check("buyer gate skipped", "buyer peek" not in log and "buyer pass" not in log)
        check("maint gate skipped", "maint pass" not in log)
        check("buy gate skipped", "buy peek" not in log and "buy pass" not in log)


def test_daemon_runs_background_passes_when_not_paused():
    print("BOUNDARY: --once --dry-run runs the buyer gate when NOT paused:")
    with tempfile.TemporaryDirectory() as tmp:
        log = _run_daemon_once(tmp)  # no control.json → not paused
        check("startup line shows paused=False", "paused=False" in log)
        check("does NOT log daemon PAUSED", "daemon PAUSED" not in log)
        check("buyer gate is entered", ("buyer peek" in log) or ("buyer pass" in log))


def _run_hook(tmp, tool_name, command=""):
    payload = json.dumps({"tool_name": tool_name,
                          "tool_input": {"command": command} if command else {"ref": "x"}})
    out = subprocess.run([sys.executable, str(ROOT / "bin" / "hooks" / "pause_guard.py")],
                         input=payload, capture_output=True, text=True,
                         env={**os.environ, "BAZAAR_DATA_DIR": tmp})
    return out.stdout.strip()


def _is_deny(stdout):
    if not stdout:
        return False
    try:
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    except (ValueError, KeyError):
        return False


def test_hook_denies_only_when_paused():
    print("BACKSTOP: pause_guard denies mutation tools + reserve only while paused:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        # Not paused → allow everything (no output).
        check("not paused: click allowed", not _is_deny(_run_hook(tmp, "mcp__playwright__browser_click")))
        check("not paused: reserve allowed",
              not _is_deny(_run_hook(tmp, "Bash", "python3 bin/pacing_gate.py reserve --marketplace fb")))
        control.pause(source="cli")
        # Paused → deny mutation + reserve, but allow read-only.
        check("paused: click denied", _is_deny(_run_hook(tmp, "mcp__playwright__browser_click")))
        check("paused: reserve denied",
              _is_deny(_run_hook(tmp, "Bash", "python3 bin/pacing_gate.py reserve --marketplace fb")))
        check("paused: read-only Bash allowed", not _is_deny(_run_hook(tmp, "Bash", "ls data/")))
        check("paused: browser snapshot (read-only) allowed",
              not _is_deny(_run_hook(tmp, "mcp__playwright__browser_snapshot")))


if __name__ == "__main__":
    print("pause interrupt + boundary tests\n")
    test_paused_flag_terminates_running_pass()
    test_daemon_holds_background_passes_when_paused()
    test_daemon_runs_background_passes_when_not_paused()
    test_hook_denies_only_when_paused()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
