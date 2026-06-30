#!/usr/bin/env python3
"""update_check.py — throttled, fail-open "is a newer SELLY available upstream?" check.

SELLY should notice when the repo it was cloned from has moved on and OFFER to update (it never
auto-applies unless `selly-config auto_upgrade` is true — see /selly-upgrade). This is the engine
the entry points call: the global launchers + the in-dir SessionStart hook (interactive) and the
always-on daemon (which notifies over the channel). All of them share ONE throttle + snooze state so
the user is asked at most once per cadence, never on every keystroke.

Design, mirroring eval_state.py / scan_state.py:
  • THROTTLE the network probe (a `git fetch`) to once per `update_check_interval_hours` (config.json,
    default 24, 0 = disabled) — repeated calls in-between return the cached last result, no network.
  • PROMPT governance via snooze: declining sets a snooze window so the same version stops asking; a
    NEWER upstream version breaks through the snooze and asks again.
  • FAIL-OPEN: no network / not a git checkout / any error -> update_available:false (never blocks use).

State: ~/.selly/update_state.json (cross-cutting install state, alongside config.json; relocate via
$SELLY_CONFIG_DIR for tests). Compares HEAD against the branch's upstream (`@{u}`), else origin/main.

Usage:
  update_check.py check  [--force] [--default-branch main]
      -> {update_available, current, latest, behind_by, branch, checked, should_prompt, reason}
  update_check.py snooze [--days N]   # the user declined: suppress this version for N days
  update_check.py clear               # reset snooze/throttle (e.g. after a successful upgrade)

Exit: 0 ok (read the JSON) · 2 bad input. `check` is fail-open and always exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # crash-safe (tmp + os.replace) JSON writes

RUNTIME_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_SNOOZE_DAYS = 1.0
SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400.0


def _config_dir() -> Path:
    """~/.selly (cross-cutting install state). Relocatable via $SELLY_CONFIG_DIR for tests."""
    env = os.environ.get("SELLY_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".selly"


def _state_path() -> Path:
    return _config_dir() / "update_state.json"


def _data_dir() -> Path:
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else RUNTIME_DIR / "data"


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def interval_hours_from_config() -> float:
    raw = _load_json(_data_dir() / "config.json").get("update_check_interval_hours",
                                                       DEFAULT_INTERVAL_HOURS)
    try:
        return max(float(raw), 0.0)  # 0 disables the auto check (manual --force still works)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS


def snooze_days_from_config() -> float:
    raw = _load_json(_data_dir() / "config.json").get("update_snooze_days", DEFAULT_SNOOZE_DAYS)
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_SNOOZE_DAYS


# --------------------------------------------------------------------------- pure decisions

def is_due(last_check_ts: float, interval_hours: float, now_ts: float) -> bool:
    """Pure throttle decision. interval <= 0 -> never auto-due; never-checked / aged-out -> due."""
    if interval_hours <= 0:
        return False
    if not last_check_ts:
        return True
    return (now_ts - last_check_ts) >= interval_hours * SECONDS_PER_HOUR


def should_prompt(result: dict, snooze_until_ts: float, declined_latest, now_ts: float) -> bool:
    """Pure: ask only if an update exists AND we're not snoozing THIS version. A newer upstream
    version (latest != the one the user declined) always breaks through an active snooze."""
    if not result.get("update_available"):
        return False
    latest = result.get("latest")
    is_new_version = latest is not None and latest != declined_latest
    if now_ts < snooze_until_ts and not is_new_version:
        return False
    return True


def summarize(current, latest, behind_by: int) -> str:
    """Pure: a human label for the update. Avoids a confusing 'vX -> vX' when commits landed
    upstream without a VERSION bump (behind by commits but the version string is unchanged)."""
    if latest and current and latest != current:
        return f"v{current} -> v{latest}"
    if behind_by:
        return f"{behind_by} new commit{'s' if behind_by != 1 else ''} (v{current or '?'})"
    return f"v{current or '?'}"


# --------------------------------------------------------------------------- git probe (injectable)

def _default_runner(cwd: Path):
    def run(args: list[str]):
        try:
            proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=30)
            return proc.returncode, proc.stdout
        except (OSError, subprocess.SubprocessError):
            return 1, ""
    return run


def _working_version(cwd: Path) -> str:
    try:
        return (cwd / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def git_check(run, cwd: Path, default_branch: str = "main") -> dict:
    """Fetch the upstream branch and compare. Returns a result dict; ok:false (fail-open) on any
    git/network failure. `run(args) -> (returncode, stdout)` is injected so this is unit-testable."""
    current = _working_version(cwd)
    rc, out = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    ref = out.strip() if rc == 0 and out.strip() else f"origin/{default_branch}"
    remote, _, branch = ref.partition("/")
    if not branch:
        remote, branch, ref = "origin", default_branch, f"origin/{default_branch}"
    frc, _ = run(["git", "fetch", "--quiet", remote, branch])
    if frc != 0:
        return {"ok": False, "reason": "fetch failed (offline or not a git checkout)",
                "branch": ref, "behind_by": 0, "current": current, "latest": None,
                "update_available": False}
    crc, cout = run(["git", "rev-list", "--count", f"HEAD..{ref}"])
    behind = int(cout.strip()) if crc == 0 and cout.strip().isdigit() else 0
    lrc, lout = run(["git", "show", f"{ref}:VERSION"])
    latest = lout.strip() if lrc == 0 and lout.strip() else current
    return {"ok": True, "reason": "ok", "branch": ref, "behind_by": behind,
            "current": current, "latest": latest, "update_available": behind > 0}


# --------------------------------------------------------------------------- commands

def run_check(force: bool, default_branch: str, *, now_ts: float, run, cwd: Path,
              state_path: Path, interval_hours: float) -> dict:
    state = _load_json(state_path)
    last_ts = float(state.get("last_check_ts", 0) or 0)
    if not force and interval_hours <= 0:
        result = dict(state.get("last_result") or {"update_available": False,
                      "current": _working_version(cwd), "latest": None, "behind_by": 0, "branch": ""})
        result["reason"] = "auto update-check disabled (config)"
        result["summary"] = summarize(result.get("current"), result.get("latest"),
                                      result.get("behind_by", 0))
        return {**result, "checked": False, "should_prompt": False}
    if not force and not is_due(last_ts, interval_hours, now_ts) and state.get("last_result"):
        result = dict(state["last_result"])
        snooze_until = float(state.get("snooze_until_ts", 0) or 0)
        declined = state.get("declined_latest")
        checked = False
    else:
        gc = git_check(run, cwd, default_branch)  # network OUTSIDE the lock (slow; don't hold it)
        if gc["ok"]:
            result = {k: gc[k] for k in ("update_available", "current", "latest", "behind_by", "branch")}
        else:  # offline: keep the last known availability, just refresh current + note why
            prev = state.get("last_result") or {}
            result = {"update_available": prev.get("update_available", False),
                      "current": gc["current"], "latest": prev.get("latest"),
                      "behind_by": prev.get("behind_by", 0), "branch": gc["branch"]}
        result["reason"] = gc["reason"]
        # Persist under a cross-process lock, re-reading inside so a concurrent snooze/clear from
        # another surface (launcher / hook / daemon) is never lost (read-modify-write safety).
        with atomic_io.locked(state_path):
            cur = _load_json(state_path)
            merged = {**cur, "last_check_ts": now_ts, "last_result": result}
            atomic_io.write_json(state_path, merged)
            snooze_until = float(merged.get("snooze_until_ts", 0) or 0)
            declined = merged.get("declined_latest")
        checked = True
    result["summary"] = summarize(result.get("current"), result.get("latest"),
                                  result.get("behind_by", 0))
    sp = should_prompt(result, snooze_until, declined, now_ts)
    return {**result, "checked": checked, "should_prompt": sp}


def run_snooze(days: float, *, now_ts: float, state_path: Path) -> dict:
    with atomic_io.locked(state_path):
        state = _load_json(state_path)
        latest = (state.get("last_result") or {}).get("latest")
        updated = {**state, "snooze_until_ts": now_ts + days * SECONDS_PER_DAY,
                   "declined_latest": latest}
        atomic_io.write_json(state_path, updated)
    return {"snoozed_days": days, "declined_latest": latest}


def run_clear(*, state_path: Path) -> dict:
    with atomic_io.locked(state_path):
        state = _load_json(state_path)
        for key in ("snooze_until_ts", "declined_latest", "last_check_ts"):
            state.pop(key, None)
        atomic_io.write_json(state_path, state)
    return {"cleared": True}


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="update_check.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--force", action="store_true", help="ignore the throttle; probe now")
    c.add_argument("--default-branch", default="main", help="upstream branch if no @{u} is set")
    s = sub.add_parser("snooze")
    s.add_argument("--days", type=float, default=None, help="suppress the current version for N days")
    sub.add_parser("clear")
    return p.parse_args(argv[1:])


def main(argv) -> int:
    try:
        ns = _parse_args(argv)
    except SystemExit:
        return 2
    now_ts = time.time()
    state_path = _state_path()
    if ns.cmd == "check":
        result = run_check(ns.force, ns.default_branch, now_ts=now_ts,
                           run=_default_runner(RUNTIME_DIR), cwd=RUNTIME_DIR,
                           state_path=state_path, interval_hours=interval_hours_from_config())
    elif ns.cmd == "snooze":
        days = ns.days if ns.days is not None else snooze_days_from_config()
        result = run_snooze(days, now_ts=now_ts, state_path=state_path)
    else:  # clear
        result = run_clear(state_path=state_path)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
