#!/usr/bin/env python3
"""Tests for the run-lock reclaim, reload debounce, and intent gate/dedupe in agent_daemon.py.

    python3 tests/test_agent_daemon_runlock.py

Regression coverage for the "Cross-list intent-line loop" incident: a bare RUN_LOCK.exists() check
treated a lock left by a DEAD pid (orphaned by a reload-storm overlap) as held forever, so every
seller/maint pass was skipped, the channel event was never consumed, and a pre-ack intent line
re-fired every tick. We test the PURE decision helpers (no subprocess / no real kills):

  • _runlock_verdict     — dead holder / own pid / garbage → reclaim; a live pass → skip; hung orphan
                           past the TTL → reclaim.
  • _read_runlock        — new JSON body, legacy bare-int body, empty/garbage → {}.
  • _pgid_alive          — None/garbage/dead → False; our own group → True.
  • _reload_due          — debounce: reload only when the source stayed changed-and-stable; an edit
                           burst or a reverted edit does NOT reload.
  • _intent_signature /  — a re-peek of the SAME pending event dedupes; a distinct event does not.
    _same_intent_event
  • _intent_already_sent / _mark_intent_sent — persisted dedupe round-trip.
  • _seller_pass_can_run — gate: free/stale lock → True (a pass will run to back the intent); a live
                           pass holds it → False (suppress the redundant, loop-prone intent).
  • _distribution_awaiting_decision — recognizes a parked cross-list offer.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import agent_daemon as ad  # noqa: E402  (must be import-side-effect-free)

_fail = []

# A pid/pgid that is (essentially) certain not to exist — probes the "dead holder" branch.
DEAD_PID = 2147483646
LIVE_PGID = os.getpgrp()  # this test's own process group is alive for the run's duration


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _swap(attr, value, body):
    """Temporarily set agent_daemon.<attr> = value for the duration of body(), then restore."""
    saved = getattr(ad, attr)
    setattr(ad, attr, value)
    try:
        body()
    finally:
        setattr(ad, attr, saved)


# --- _runlock_verdict --------------------------------------------------------------------------

def test_verdict_empty_body_reclaims():
    check("empty body → reclaim", ad._runlock_verdict({}, os.getpid(), 1000.0) == "reclaim")


def test_verdict_dead_holder_reclaims():
    # The observed bug: a lock left by a dead daemon pid, no live pass group → must reclaim, not skip.
    body = {"daemon_pid": DEAD_PID, "pgid": None, "ts": 1000.0}
    check("dead daemon_pid → reclaim", ad._runlock_verdict(body, os.getpid(), 1001.0) == "reclaim")


def test_verdict_our_own_pid_reclaims():
    # run_pass is sequential, so a lock recording OUR pid with no live pass is a leak → reclaim.
    body = {"daemon_pid": os.getpid(), "pgid": None, "ts": 1000.0}
    check("own daemon_pid, no pass → reclaim", ad._runlock_verdict(body, os.getpid(), 1000.5) == "reclaim")


def test_verdict_live_other_daemon_skips():
    # A DIFFERENT live daemon holding the lock (shouldn't happen under INSTANCE_LOCK) → skip, don't steal.
    body = {"daemon_pid": os.getpid(), "pgid": None, "ts": 1000.0}
    other = os.getpid() + 1  # make my_pid differ so the daemon_pid reads as "not us" but alive
    check("live other daemon → skip", ad._runlock_verdict(body, other, 1000.5) == "skip")


def test_verdict_live_pass_group_skips():
    body = {"daemon_pid": os.getpid(), "pgid": LIVE_PGID, "ts": 1000.0}
    check("fresh live pass group → skip", ad._runlock_verdict(body, os.getpid(), 1000.5) == "skip")


def test_verdict_hung_pass_group_past_ttl_reclaims():
    # A pass group that blew the 900s deadline is a hung orphan → reclaim (run_pass then kills it).
    body = {"daemon_pid": os.getpid(), "pgid": LIVE_PGID, "ts": 1000.0}
    now = 1000.0 + ad.RUN_LOCK_TTL_SEC + 5
    check("aged live pass group → reclaim", ad._runlock_verdict(body, os.getpid(), now) == "reclaim")


def test_verdict_no_ts_treated_as_aged():
    # A legacy/bare-int lock has ts=0; with no live pass it must reclaim (never skip forever).
    body = {"daemon_pid": DEAD_PID, "pgid": None, "ts": 0}
    check("ts=0 dead holder → reclaim", ad._runlock_verdict(body, os.getpid(), 9_999_999.0) == "reclaim")


# --- _read_runlock -----------------------------------------------------------------------------

def test_read_runlock_json_body():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rl"
        p.write_text(json.dumps({"daemon_pid": 5, "pgid": 6, "ts": 7}))
        _swap("RUN_LOCK", p, lambda: check(
            "json body parsed", ad._read_runlock() == {"daemon_pid": 5, "pgid": 6, "ts": 7}))


def test_read_runlock_legacy_int_body():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rl"
        p.write_text("31852")  # pre-fix bare-int body
        _swap("RUN_LOCK", p, lambda: check(
            "legacy int body → daemon_pid", ad._read_runlock() == {"daemon_pid": 31852, "pgid": None, "ts": 0}))


def test_read_runlock_empty_and_garbage():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rl"
        p.write_text("   ")
        _swap("RUN_LOCK", p, lambda: check("empty body → {}", ad._read_runlock() == {}))
        p.write_text("not-a-pid")
        _swap("RUN_LOCK", p, lambda: check("garbage body → {}", ad._read_runlock() == {}))


def test_read_runlock_absent():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "does-not-exist"
        _swap("RUN_LOCK", p, lambda: check("absent → {}", ad._read_runlock() == {}))


# --- _pgid_alive -------------------------------------------------------------------------------

def test_pgid_alive():
    check("None → dead", ad._pgid_alive(None) is False)
    check("string → dead", ad._pgid_alive("123") is False)
    check("non-positive → dead", ad._pgid_alive(0) is False)
    check("huge/dead pgid → dead", ad._pgid_alive(DEAD_PID) is False)
    check("own group → alive", ad._pgid_alive(LIVE_PGID) is True)


# --- _reload_due (debounce) --------------------------------------------------------------------

def test_reload_due_no_change():
    reload_now, last, stable = ad._reload_due(100, 100, 100, None, 500.0, 15)
    check("unchanged → no reload", reload_now is False)
    check("unchanged → stable stays None", stable is None)


def test_reload_due_first_detection_waits():
    # Fingerprint just diverged → arm the stability clock, do NOT reload yet.
    reload_now, last, stable = ad._reload_due(100, 100, 200, None, 500.0, 15)
    check("first change → no reload", reload_now is False)
    check("first change → last updated", last == 200)
    check("first change → clock armed", stable == 500.0)


def test_reload_due_reloads_after_stable_window():
    # Same changed fingerprint, debounce elapsed → reload.
    reload_now, last, stable = ad._reload_due(100, 200, 200, 500.0, 516.0, 15)
    check("stable past window → reload", reload_now is True)


def test_reload_due_burst_resets_clock():
    # Still-changing fingerprint (edit burst): clock resets, no reload even past the window vs first change.
    reload_now, last, stable = ad._reload_due(100, 200, 300, 500.0, 520.0, 15)
    check("burst change → no reload", reload_now is False)
    check("burst change → clock reset to now", stable == 520.0)


def test_reload_due_revert_cancels():
    # Fingerprint returned to the startup baseline (edit reverted) → cancel the pending reload.
    reload_now, last, stable = ad._reload_due(100, 200, 100, 500.0, 999.0, 15)
    check("revert → no reload", reload_now is False)
    check("revert → clock cleared", stable is None)


# --- intent signature / dedupe -----------------------------------------------------------------

def test_intent_signature_stable_and_distinct():
    a = {"latest_ts": 111, "latest_action": {"ref": "close-x", "choice": "yes"}, "latest_text": "hi"}
    a2 = {"latest_ts": 111, "latest_action": {"ref": "close-x", "choice": "yes"}, "latest_text": "hi"}
    b = {"latest_ts": 222, "latest_action": {"ref": "close-x", "choice": "yes"}, "latest_text": "hi"}
    check("same event → same signature", ad._intent_signature(a) == ad._intent_signature(a2))
    check("newer ts → different signature", ad._intent_signature(a) != ad._intent_signature(b))


def test_same_intent_event():
    check("identical sig → dedupe", ad._same_intent_event("111|r|yes|hi", "111|r|yes|hi") is True)
    check("different sig → not dedupe", ad._same_intent_event("111|r|yes|hi", "222|r|yes|hi") is False)
    check("empty sig → never dedupe", ad._same_intent_event("|||", "|||") is False)


def test_intent_state_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "intent_state.json"
        peek = {"latest_ts": 999, "latest_action": {"ref": "r1", "choice": "yes"}, "latest_text": "x"}
        other = {"latest_ts": 1000, "latest_action": {}, "latest_text": "y"}

        def body():
            check("first ack not deduped", ad._intent_already_sent(peek) is False)
            ad._mark_intent_sent(peek)
            check("repeat same event deduped", ad._intent_already_sent(peek) is True)
            check("distinct event not deduped", ad._intent_already_sent(other) is False)
        _swap("INTENT_STATE", p, body)


# --- _seller_pass_can_run (gate) ---------------------------------------------------------------

def test_seller_pass_can_run_gate():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rl"

        def absent():
            check("no lock → pass can run (intent backed)", ad._seller_pass_can_run() is True)
        _swap("RUN_LOCK", p, absent)

        p.write_text(json.dumps({"daemon_pid": DEAD_PID, "pgid": None, "ts": 1.0}))
        def stale():
            check("stale lock → pass can run (reclaim)", ad._seller_pass_can_run() is True)
        _swap("RUN_LOCK", p, stale)

        p.write_text(json.dumps({"daemon_pid": os.getpid(), "pgid": LIVE_PGID, "ts": 9_999_999_999.0}))
        def live():
            check("live pass holds lock → gate suppresses intent", ad._seller_pass_can_run() is False)
        _swap("RUN_LOCK", p, live)


# --- _distribution_awaiting_decision -----------------------------------------------------------

def test_distribution_awaiting_decision():
    with tempfile.TemporaryDirectory() as d:
        data = Path(d) / "data"
        data.mkdir()
        sp = data / "distribution_session.json"

        def awaiting():
            sp.write_text(json.dumps({"active": True, "step": "awaiting_distribute"}))
            check("active + awaiting_distribute → True", ad._distribution_awaiting_decision() is True)
            sp.write_text(json.dumps({"active": True, "step": "scan"}))
            check("active but other step → False", ad._distribution_awaiting_decision() is False)
            sp.write_text(json.dumps({"active": False, "step": "awaiting_distribute"}))
            check("inactive → False", ad._distribution_awaiting_decision() is False)
        _swap("SELLER_DIR", Path(d), awaiting)

    # missing file → fail-open False
    _swap("SELLER_DIR", Path("/nonexistent-selly-dir"),
          lambda: check("missing session → False", ad._distribution_awaiting_decision() is False))


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items()
                           if n.startswith("test_") and callable(f)):
        fn()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
