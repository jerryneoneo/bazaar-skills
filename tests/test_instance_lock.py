#!/usr/bin/env python3
"""Tests for instance_lock.py — the PID-aware, stale-reclaiming daemon singleton lock.

    python3 tests/test_instance_lock.py

The lock guarantees one agent_daemon at a time (the heartbeat-TTL lease liveness assumes a single
heartbeater, and a second Telegram consumer steals updates). We test the pure liveness/parse helpers
and the acquire contract — a fresh acquire wins + writes our PID, a SECOND acquire on a held lock is
refused with truthful holder info, and a lock pointing at a DEAD PID is reclaimed (no respawn churn).
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import instance_lock  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_is_pid_alive():
    print("is_pid_alive: our own PID is alive, an impossible PID is not:")
    check("our pid is alive", instance_lock.is_pid_alive(os.getpid()) is True)
    check("an impossibly-high pid is dead", instance_lock.is_pid_alive(99999999) is False)


def test_read_holder_pid():
    print("read_holder_pid: parses an int, returns None on missing/garbage:")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "lock"
        check("missing file -> None", instance_lock.read_holder_pid(p) is None)
        p.write_text("4321")
        check("integer content -> the int", instance_lock.read_holder_pid(p) == 4321)
        p.write_text("4321\n")
        check("trailing whitespace tolerated", instance_lock.read_holder_pid(p) == 4321)
        p.write_text("not a pid")
        check("garbage -> None", instance_lock.read_holder_pid(p) is None)
        p.write_text("")
        check("empty -> None", instance_lock.read_holder_pid(p) is None)


def test_acquire_fresh_writes_our_pid():
    print("acquire: a fresh lock is acquired and records our PID + refreshes the heartbeat PID:")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        res = instance_lock.acquire(lock, hb)
        try:
            check("acquired", res["acquired"] is True)
            check("not a reclaim (lock was fresh)", res["reclaimed"] is False)
            check("holder pid is us", res["holder_pid"] == os.getpid())
            check("holder is alive", res["holder_alive"] is True)
            check("returns a live fd to hold", isinstance(res["fd"], int) and res["fd"] >= 0)
            check("lock file records our pid", instance_lock.read_holder_pid(lock) == os.getpid())
        finally:
            if isinstance(res.get("fd"), int):
                os.close(res["fd"])


def test_acquire_second_holder_refused():
    print("acquire: a SECOND acquire while the first fd is held is refused with truthful holder info:")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        first = instance_lock.acquire(lock, hb)
        try:
            check("first acquired", first["acquired"] is True)
            second = instance_lock.acquire(lock, hb)
            try:
                check("second refused", second["acquired"] is False)
                check("not a reclaim (holder is alive)", second["reclaimed"] is False)
                check("reports the live holder pid (us)", second["holder_pid"] == os.getpid())
                check("reports holder alive", second["holder_alive"] is True)
                check("no fd handed out on refusal", second["fd"] is None)
            finally:
                if isinstance(second.get("fd"), int):
                    os.close(second["fd"])
        finally:
            if isinstance(first.get("fd"), int):
                os.close(first["fd"])


def test_acquire_reclaims_dead_holder():
    print("acquire: a lock held by a DEAD pid is reclaimed (this is the respawn-storm fix):")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        # Plant a lock pointing at a dead PID, with NO live flock holder.
        lock.write_text("99999999")
        res = instance_lock.acquire(lock, hb)
        try:
            check("acquired (the flock was free)", res["acquired"] is True)
            check("reclaimed flag set", res["reclaimed"] is True)
            check("holder pid rewritten to us", res["holder_pid"] == os.getpid())
            check("holder alive", res["holder_alive"] is True)
            check("lock file now records our pid", instance_lock.read_holder_pid(lock) == os.getpid())
        finally:
            if isinstance(res.get("fd"), int):
                os.close(res["fd"])


def test_acquire_reconciles_heartbeat_pid():
    print("acquire: an existing heartbeat with a stale PID is reconciled to ours (split-brain heal):")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        hb.write_text(json.dumps({"ts": 1.0, "pid": 12345}))
        res = instance_lock.acquire(lock, hb)
        try:
            check("acquired", res["acquired"] is True)
            data = json.loads(hb.read_text())
            check("heartbeat pid reconciled to ours", data.get("pid") == os.getpid())
        finally:
            if isinstance(res.get("fd"), int):
                os.close(res["fd"])


def test_acquire_reclaim_stamps_fresh_heartbeat_ts():
    print("Bug D2: a reclaim stamps a FRESH heartbeat ts (epoch secs), not the dead holder's old ts —")
    print("        so the watchdog/healthcheck see a healthy just-started daemon, not a stale one:")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        # A dead holder left an OLD heartbeat tick (epoch secs) far in the past, and a lock body
        # naming its now-dead PID with NO live flock holder — the reclaim path.
        stale_ts = 1.0
        hb.write_text(json.dumps({"ts": stale_ts, "pid": 12345}))
        lock.write_text("99999999")  # dead PID, flock free
        before = time.time()
        res = instance_lock.acquire(lock, hb)
        after = time.time()
        try:
            check("reclaimed the dead-holder lock", res["reclaimed"] is True)
            data = json.loads(hb.read_text())
            check("heartbeat pid reconciled to ours", data.get("pid") == os.getpid())
            ts = data.get("ts")
            check("heartbeat ts is a number", isinstance(ts, (int, float)))
            # The fix: ts must be NOW (the daemon is alive as of the reclaim), not the dead holder's ts.
            check("heartbeat ts is NOT the dead holder's stale ts", ts != stale_ts)
            check("heartbeat ts is fresh (between before/after the acquire)",
                  isinstance(ts, (int, float)) and before <= ts <= after)
        finally:
            if isinstance(res.get("fd"), int):
                os.close(res["fd"])


def test_clear_holder_clears_when_we_are_the_holder():
    print("Bug D3: clear_holder empties the lock body when OUR pid is the recorded holder —")
    print("        a clean shutdown leaves no holder, so the watchdog can't mistake a recycled PID:")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        lock.write_text(str(os.getpid()))
        cleared = instance_lock.clear_holder(lock)
        check("reports it cleared", cleared is True)
        check("lock body no longer names a holder pid", instance_lock.read_holder_pid(lock) is None)


def test_clear_holder_leaves_a_foreign_holder_untouched():
    print("Bug D3: clear_holder is a no-op when ANOTHER pid holds the lock (never steal a live lock):")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        # Some other live process (use a PID that is not ours) holds the lock body.
        foreign = os.getpid() + 1
        lock.write_text(str(foreign))
        cleared = instance_lock.clear_holder(lock)
        check("reports it did NOT clear", cleared is False)
        check("foreign holder pid is preserved", instance_lock.read_holder_pid(lock) == foreign)


def test_clear_holder_missing_lock_is_noop():
    print("Bug D3: clear_holder never raises on a missing/garbage lock (fail-open):")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        check("missing lock → False, no raise", instance_lock.clear_holder(lock) is False)
        lock.write_text("not a pid")
        check("garbage lock → False, no raise", instance_lock.clear_holder(lock) is False)


def test_clear_holder_after_acquire_lets_watchdog_see_no_live_holder():
    print("Bug D3: after a clean acquire+clear, read_holder_pid is None — the watchdog's _holder_alive")
    print("        seam (read_holder_pid → is_pid_alive) sees NO holder, correctly treated as dead:")
    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        res = instance_lock.acquire(lock, hb)
        try:
            check("acquired + records our pid", instance_lock.read_holder_pid(lock) == os.getpid())
            instance_lock.clear_holder(lock)
            check("after clear, no holder pid recorded", instance_lock.read_holder_pid(lock) is None)
        finally:
            if isinstance(res.get("fd"), int):
                os.close(res["fd"])


if __name__ == "__main__":
    print("instance_lock tests\n")
    test_is_pid_alive()
    test_read_holder_pid()
    test_acquire_fresh_writes_our_pid()
    test_acquire_second_holder_refused()
    test_acquire_reclaims_dead_holder()
    test_acquire_reconciles_heartbeat_pid()
    test_acquire_reclaim_stamps_fresh_heartbeat_ts()
    test_clear_holder_clears_when_we_are_the_holder()
    test_clear_holder_leaves_a_foreign_holder_untouched()
    test_clear_holder_missing_lock_is_noop()
    test_clear_holder_after_acquire_lets_watchdog_see_no_live_holder()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
