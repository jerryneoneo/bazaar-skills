#!/usr/bin/env python3
"""Tests for update_check.py — the throttled, fail-open upstream-update engine (no real git/network).

    python3 tests/test_update_check.py

Covers the pure throttle + prompt-governance decisions and the git probe / state flow with an
injected `run` (git) and explicit clock — so nothing hits the network or a real repo.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import update_check as uc  # noqa: E402

_failures = []
HOUR = 3600.0
DAY = 86400.0


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _cwd_with_version(tmp, version="0.1.0"):
    (Path(tmp) / "VERSION").write_text(version + "\n")
    return Path(tmp)


def _make_run(upstream=(0, "origin/main\n"), fetch=(0, ""), behind=(0, "0\n"), show=(0, "0.1.0\n")):
    def run(args):
        if args[:2] == ["git", "rev-parse"]:
            return upstream
        if args[:2] == ["git", "fetch"]:
            return fetch
        if args[:2] == ["git", "rev-list"]:
            return behind
        if args[:2] == ["git", "show"]:
            return show
        return (1, "")
    return run


def test_is_due():
    print("is_due (throttle):")
    check("disabled (0) -> never due", uc.is_due(0, 0, 1000) is False)
    check("never checked -> due", uc.is_due(0, 24, 1000) is True)
    check("fresh -> not due", uc.is_due(1000, 24, 1000 + HOUR) is False)
    check("aged out -> due", uc.is_due(1000, 24, 1000 + 25 * HOUR) is True)


def test_should_prompt():
    print("should_prompt (snooze/new-version):")
    avail = {"update_available": True, "latest": "0.2.0"}
    none = {"update_available": False, "latest": "0.1.0"}
    check("no update -> no prompt", uc.should_prompt(none, 0, None, 1000) is False)
    check("update, no snooze -> prompt", uc.should_prompt(avail, 0, None, 1000) is True)
    check("snoozed same version -> no prompt",
          uc.should_prompt(avail, 5000, "0.2.0", 1000) is False)
    check("snoozed but NEWER version breaks through",
          uc.should_prompt({"update_available": True, "latest": "0.3.0"}, 5000, "0.2.0", 1000) is True)
    check("snooze expired -> prompt again",
          uc.should_prompt(avail, 500, "0.2.0", 1000) is True)


def test_summarize():
    print("summarize (friendly label, no vX->vX):")
    check("version bump -> arrow", uc.summarize("0.2.0", "0.3.0", 5) == "v0.2.0 -> v0.3.0")
    check("behind but same version -> commit count",
          uc.summarize("0.2.0", "0.2.0", 3) == "3 new commits (v0.2.0)")
    check("singular commit", uc.summarize("0.2.0", "0.2.0", 1) == "1 new commit (v0.2.0)")
    check("up to date -> bare version", uc.summarize("0.2.0", "0.2.0", 0) == "v0.2.0")
    check("missing latest -> bare version", uc.summarize("0.2.0", None, 0) == "v0.2.0")


def test_git_check():
    print("git_check (stubbed git):")
    with tempfile.TemporaryDirectory() as tmp:
        cwd = _cwd_with_version(tmp, "0.1.0")
        # behind by 3, upstream VERSION is newer
        r = uc.git_check(_make_run(behind=(0, "3\n"), show=(0, "0.2.0\n")), cwd)
        check("ok", r["ok"] is True)
        check("update_available true", r["update_available"] is True)
        check("behind_by parsed", r["behind_by"] == 3)
        check("current from working tree", r["current"] == "0.1.0")
        check("latest from upstream VERSION", r["latest"] == "0.2.0")
        check("branch is the upstream ref", r["branch"] == "origin/main")

        # up to date
        r2 = uc.git_check(_make_run(behind=(0, "0\n"), show=(0, "0.1.0\n")), cwd)
        check("up-to-date -> no update", r2["update_available"] is False and r2["behind_by"] == 0)

        # fetch fails -> fail-open
        r3 = uc.git_check(_make_run(fetch=(1, "")), cwd)
        check("fetch fail -> ok false", r3["ok"] is False)
        check("fetch fail -> update_available false", r3["update_available"] is False)

        # no upstream configured -> falls back to origin/<default-branch>
        r4 = uc.git_check(_make_run(upstream=(1, ""), behind=(0, "1\n"), show=(0, "0.2.0\n")), cwd,
                          default_branch="main")
        check("no @{u} -> origin/main fallback", r4["branch"] == "origin/main" and r4["update_available"])


def test_run_check_throttle_and_state():
    print("run_check (throttle + state + offline):")
    with tempfile.TemporaryDirectory() as tmp:
        cwd = _cwd_with_version(tmp, "0.1.0")
        state_path = Path(tmp) / "update_state.json"
        run = _make_run(behind=(0, "2\n"), show=(0, "0.2.0\n"))
        # first call: due (no state) -> hits git, persists
        r1 = uc.run_check(False, "main", now_ts=1000, run=run, cwd=cwd,
                          state_path=state_path, interval_hours=24)
        check("first call checked", r1["checked"] is True)
        check("first call sees update", r1["update_available"] is True and r1["should_prompt"] is True)
        check("result carries a friendly summary", r1.get("summary") == "v0.1.0 -> v0.2.0")
        check("state persisted", state_path.exists())

        # second call within TTL: cached, no network (use a run that would FAIL if called)
        boom = lambda args: (_ for _ in ()).throw(AssertionError("network called within TTL"))
        r2 = uc.run_check(False, "main", now_ts=1000 + HOUR, run=boom, cwd=cwd,
                          state_path=state_path, interval_hours=24)
        check("within TTL -> cached (no network)", r2["checked"] is False)
        check("cached still shows update", r2["update_available"] is True)

        # forced: bypasses TTL, hits git again
        r3 = uc.run_check(True, "main", now_ts=1000 + HOUR, run=run, cwd=cwd,
                          state_path=state_path, interval_hours=24)
        check("force -> checked again", r3["checked"] is True)

        # offline at next due window: keep last-known availability
        r4 = uc.run_check(True, "main", now_ts=1000 + 40 * HOUR, run=_make_run(fetch=(1, "")),
                          cwd=cwd, state_path=state_path, interval_hours=24)
        check("offline keeps last-known update_available", r4["update_available"] is True)
        check("offline reason surfaced", "offline" in r4["reason"] or "fetch failed" in r4["reason"])


def test_run_check_disabled():
    print("run_check (disabled via interval 0):")
    with tempfile.TemporaryDirectory() as tmp:
        cwd = _cwd_with_version(tmp, "0.1.0")
        state_path = Path(tmp) / "update_state.json"
        boom = lambda args: (_ for _ in ()).throw(AssertionError("git called while disabled"))
        r = uc.run_check(False, "main", now_ts=1000, run=boom, cwd=cwd,
                         state_path=state_path, interval_hours=0)
        check("disabled -> no network, no prompt", r["checked"] is False and r["should_prompt"] is False)


def test_snooze_and_clear():
    print("snooze + clear:")
    with tempfile.TemporaryDirectory() as tmp:
        cwd = _cwd_with_version(tmp, "0.1.0")
        state_path = Path(tmp) / "update_state.json"
        run = _make_run(behind=(0, "1\n"), show=(0, "0.2.0\n"))
        uc.run_check(False, "main", now_ts=1000, run=run, cwd=cwd, state_path=state_path,
                     interval_hours=24)
        uc.run_snooze(7, now_ts=1000, state_path=state_path)
        st = json.loads(state_path.read_text())
        check("snooze_until set ~7d out", abs(st["snooze_until_ts"] - (1000 + 7 * DAY)) < 1)
        check("declined_latest recorded", st["declined_latest"] == "0.2.0")
        # within snooze, same version -> no prompt
        r = uc.run_check(False, "main", now_ts=1000 + HOUR, run=run, cwd=cwd, state_path=state_path,
                         interval_hours=24)
        check("snoozed -> should_prompt false", r["should_prompt"] is False)
        # a newer upstream version breaks through
        run2 = _make_run(behind=(0, "2\n"), show=(0, "0.3.0\n"))
        r2 = uc.run_check(True, "main", now_ts=1000 + 2 * HOUR, run=run2, cwd=cwd,
                          state_path=state_path, interval_hours=24)
        check("newer version breaks snooze", r2["should_prompt"] is True)
        # clear resets snooze/declined/throttle
        uc.run_clear(state_path=state_path)
        st2 = json.loads(state_path.read_text())
        check("clear drops snooze/declined/last_check",
              all(k not in st2 for k in ("snooze_until_ts", "declined_latest", "last_check_ts")))


if __name__ == "__main__":
    print("update_check.py tests\n")
    test_is_due()
    test_should_prompt()
    test_summarize()
    test_git_check()
    test_run_check_throttle_and_state()
    test_run_check_disabled()
    test_snooze_and_clear()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
