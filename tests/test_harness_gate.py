#!/usr/bin/env python3
"""Tests for the harness_run completion gate (Track A3). Plain python:

    python3 tests/test_harness_gate.py

A gated pass (channel/buyer/buy) must NOT end owning a never-fired send. After the pass exits,
run_pass peeks the thread_outbox for PENDING (status=pending) intents in this pass's scope and, if any
remain un-fired, returns REDRIVE_SIGNAL so the daemon schedules a prioritized re-drive. The decision
logic (`_undriven_intents`) is the testable unit:
  • flags a stranded pending intent (the vida silent drop),
  • scopes to the marketplace `resource` when set,
  • is lease-guarded (a concurrent worker's in-flight intent is never mistaken for stranded),
  • ignores sent_unverified intents (those go through the verify path, not re-drive).
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import harness_run  # noqa: E402
import lease  # noqa: E402
import thread_outbox as to  # noqa: E402

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _enqueue(thread, market, text, in_msg="i", sent=False):
    rec = to.enqueue(thread, market, text, in_msg, datetime.now(timezone.utc), side="sell")
    if sent:
        to.mark_sent(rec["id"])
    return rec["id"]


def test_signal_constants_distinct():
    print("REDRIVE_SIGNAL is a distinct, non-zero signal:")
    check("REDRIVE_SIGNAL defined", hasattr(harness_run, "REDRIVE_SIGNAL"))
    check("non-zero", harness_run.REDRIVE_SIGNAL != 0)
    check("distinct from CAP_HIT_SIGNAL", harness_run.REDRIVE_SIGNAL != harness_run.CAP_HIT_SIGNAL)


def test_gated_modes_cover_sending_passes():
    print("the gate covers every pass that can send a marketplace message:")
    for mode in ("channel", "buyer", "buy"):
        check(f"{mode} is gated", mode in harness_run.GATED_MODES)
    check("maint (background, no inbound replies) is NOT gated", "maint" not in harness_run.GATED_MODES)


def test_flags_stranded_pending_intent():
    print("a stranded pending intent (no live lease) is flagged for re-drive:")
    with tempfile.TemporaryDirectory() as d:
        os.environ["BAZAAR_DATA_DIR"] = d
        try:
            _enqueue("fb:vida", "fb", "no defects, all brand new", in_msg="1:50 PM|defects?")
            undriven = harness_run._undriven_intents("")
            check("one stranded intent flagged", len(undriven) == 1)
            check("it is the vida thread", undriven[0]["thread_id"] == "fb:vida")
        finally:
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_scopes_to_resource_market():
    print("the gate scopes to the pass's marketplace (resource):")
    with tempfile.TemporaryDirectory() as d:
        os.environ["BAZAAR_DATA_DIR"] = d
        try:
            _enqueue("fb:a", "fb", "x")
            _enqueue("carousell:b", "carousell", "y")
            check("resource=fb sees only fb", len(harness_run._undriven_intents("fb")) == 1)
            check("resource=fb intent is the fb one",
                  harness_run._undriven_intents("fb")[0]["market"] == "fb")
            check("unscoped sees both", len(harness_run._undriven_intents("")) == 2)
        finally:
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_live_lease_intent_not_flagged():
    print("a pending intent whose market holds a LIVE lease is NOT flagged (in-flight, not stranded):")
    with tempfile.TemporaryDirectory() as d:
        os.environ["BAZAAR_DATA_DIR"] = d
        try:
            _enqueue("fb:vida", "fb", "in flight")
            lease.acquire(Path(d), "market:fb", "worker-live", "buyer", lease.AGENT_MARKET_TTL_SEC)
            check("live-lease intent guarded (not flagged)",
                  harness_run._undriven_intents("fb") == [])
        finally:
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_sent_unverified_not_flagged():
    print("a sent_unverified intent is NOT flagged (the send fired; verify path owns it):")
    with tempfile.TemporaryDirectory() as d:
        os.environ["BAZAAR_DATA_DIR"] = d
        try:
            _enqueue("fb:olaf", "fb", "sent but uncommitted", sent=True)
            check("sent_unverified not treated as a never-fired send",
                  harness_run._undriven_intents("fb") == [])
        finally:
            os.environ.pop("BAZAAR_DATA_DIR", None)


if __name__ == "__main__":
    print("harness_run completion-gate tests\n")
    test_signal_constants_distinct()
    test_gated_modes_cover_sending_passes()
    test_flags_stranded_pending_intent()
    test_scopes_to_resource_market()
    test_live_lease_intent_not_flagged()
    test_sent_unverified_not_flagged()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
