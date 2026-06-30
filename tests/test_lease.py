#!/usr/bin/env python3
"""Tests for lease.py — per-resource leases that replace the single global .daemon.runlock.

Runnable with plain python:  python3 tests/test_lease.py

THE invariant: under concurrency, many processes racing to acquire ONE resource → exactly one
wins. Plus: stale-TTL reclaim (a crashed holder's lease is recoverable), heartbeat extends a live
lease, release frees it, and a non-holder can't steal a live lease without --force.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import lease  # noqa: E402

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
TTL = 120

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _rec(holder, heartbeat_dt):
    return {"holder": holder, "mode": "buyer", "acquired_at": NOW.isoformat(),
            "heartbeat_at": heartbeat_dt.isoformat(), "ttl_sec": TTL, "resource": "market:carousell"}


def test_free_and_stale_pure():
    print("pure: free / stale logic:")
    check("no record is free", lease.is_free(None, NOW, TTL) is True)
    check("holder=null is free", lease.is_free({"holder": None}, NOW, TTL) is True)
    fresh = _rec("A", NOW)
    check("held + fresh heartbeat is NOT free", lease.is_free(fresh, NOW, TTL) is False)
    stale = _rec("A", NOW - timedelta(seconds=TTL + 5))
    check("held + heartbeat older than ttl is stale → free", lease.is_free(stale, NOW, TTL) is True)
    check("is_stale agrees", lease.is_stale(stale, NOW, TTL) is True)
    check("fresh is not stale", lease.is_stale(fresh, NOW, TTL) is False)


def test_acquire_then_blocks():
    print("acquire blocks a second holder:")
    with tempfile.TemporaryDirectory() as d:
        a = lease.acquire(Path(d), "market:carousell", "A", "buyer", TTL, NOW)
        check("first holder acquires", a["acquired"] is True)
        b = lease.acquire(Path(d), "market:carousell", "B", "buy", TTL, NOW)
        check("second holder is blocked", b["acquired"] is False)
        check("blocked result reports the live holder", b.get("holder") == "A")


def test_release_frees():
    print("release frees the lease:")
    with tempfile.TemporaryDirectory() as d:
        lease.acquire(Path(d), "market:fb", "A", "buyer", TTL, NOW)
        rel = lease.release(Path(d), "market:fb", "A", now=NOW)
        check("holder can release", rel["released"] is True)
        b = lease.acquire(Path(d), "market:fb", "B", "buyer", TTL, NOW)
        check("freed lease is re-acquirable", b["acquired"] is True)


def test_stale_reclaim():
    print("stale lease is reclaimed:")
    with tempfile.TemporaryDirectory() as d:
        lease.acquire(Path(d), "market:fb", "A", "buyer", TTL, NOW)
        later = NOW + timedelta(seconds=TTL + 30)  # A never heartbeat → stale
        b = lease.acquire(Path(d), "market:fb", "B", "buyer", TTL, later)
        check("stale lease reclaimed by new holder", b["acquired"] is True)
        check("reclaim is flagged", b.get("stale_reclaimed") is True)


def test_heartbeat_extends_and_guards():
    print("heartbeat extends a live lease; wrong holder can't:")
    with tempfile.TemporaryDirectory() as d:
        lease.acquire(Path(d), "market:fb", "A", "buyer", TTL, NOW)
        mid = NOW + timedelta(seconds=TTL - 10)
        hb = lease.heartbeat(Path(d), "market:fb", "A", now=mid)
        check("holder heartbeat ok", hb["ok"] is True)
        # Now at NOW+TTL+5: without the heartbeat it would be stale; with it (last beat at mid),
        # mid+TTL = NOW+2*TTL-10, so still live → B is blocked.
        probe = NOW + timedelta(seconds=TTL + 5)
        b = lease.acquire(Path(d), "market:fb", "B", "buyer", TTL, probe)
        check("heartbeat kept the lease alive (B blocked)", b["acquired"] is False)
        bad = lease.heartbeat(Path(d), "market:fb", "Z", now=mid)
        check("non-holder heartbeat fails", bad["ok"] is False)


def test_release_wrong_holder():
    print("release guards on holder (force overrides):")
    with tempfile.TemporaryDirectory() as d:
        lease.acquire(Path(d), "market:fb", "A", "buyer", TTL, NOW)
        rel = lease.release(Path(d), "market:fb", "Z", now=NOW)
        check("non-holder release is a no-op", rel["released"] is False)
        forced = lease.release(Path(d), "market:fb", "Z", force=True, now=NOW)
        check("force release frees regardless of holder", forced["released"] is True)


def test_concurrent_acquire_single_winner():
    print("INVARIANT: concurrent acquires of one resource → exactly one winner:")
    n = 12
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "SELLY_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "lease.py"), "acquire",
                "--resource", "market:carousell", "--mode", "buyer",
                "--ttl", str(TTL), "--now", NOW.isoformat()]
        procs = [subprocess.Popen(base + ["--holder", f"H{i}"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(n)]
        wins = 0
        for p in procs:
            out, _ = p.communicate()
            if json.loads(out)["acquired"]:
                wins += 1
        check(f"exactly one of {n} concurrent acquires wins (got {wins})", wins == 1)


def test_cli_roundtrip():
    print("CLI acquire → status → release:")
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "SELLY_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "lease.py")]
        common = ["--now", NOW.isoformat()]
        acq = subprocess.run(base + ["acquire", "--resource", "channel", "--holder", "S1"] + common,
                             capture_output=True, text=True, env=env)
        check("acquire exits 0", acq.returncode == 0 and json.loads(acq.stdout)["acquired"] is True)
        st = subprocess.run(base + ["status", "--resource", "channel"] + common,
                            capture_output=True, text=True, env=env)
        check("status shows held", st.returncode == 0 and json.loads(st.stdout).get("held") is True)
        rel = subprocess.run(base + ["release", "--resource", "channel", "--holder", "S1"] + common,
                             capture_output=True, text=True, env=env)
        check("release exits 0", rel.returncode == 0 and json.loads(rel.stdout)["released"] is True)


def test_bad_input():
    print("input validation:")
    base = [sys.executable, str(ROOT / "bin" / "lease.py")]
    bad = [["acquire", "--holder", "A"], ["acquire", "--resource", ""], ["bogus", "--resource", "x"]]
    ok = all(subprocess.run(base + a, capture_output=True, text=True).returncode != 0 for a in bad)
    check("malformed input exits nonzero", ok)


if __name__ == "__main__":
    print("lease tests\n")
    test_free_and_stale_pure()
    test_acquire_then_blocks()
    test_release_frees()
    test_stale_reclaim()
    test_heartbeat_extends_and_guards()
    test_release_wrong_holder()
    test_concurrent_acquire_single_winner()
    test_cli_roundtrip()
    test_bad_input()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
