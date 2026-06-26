#!/usr/bin/env python3
"""Adversarial tests for checkout.py — runnable with plain python:

    python3 tests/test_checkout.py

checkout.py issues the (mock) carousell.ai checkout link at close. The link itself
is stubbed today (the checkout rail is a separate workstream); the guarantees we test are
the ones that must hold no matter who issues the real link later:

  (1) the floor is NEVER echoed — value or token — on ANY path. checkout re-validates
      the agreed price against the hidden floor (the same trust boundary as floor_gate),
      so it reads the floor but must leak nothing.
  (2) a link is issued only at/above floor; a below-floor price is rejected.
  (3) sale_id is deterministic (idempotent re-issue) and state is written immutably.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import checkout  # noqa: E402

ITEM = "sample-ikea-desk"  # floor 65, list 90 — see data/floors/sample-ikea-desk.json
FLOOR = 65
LIST = 90
THREAD = "carousell:TEST-checkout"
CHECKOUTS_DIR = ROOT / "data" / "checkouts"

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "checkout.py"), *args],
        capture_output=True, text=True,
    )


def _cleanup():
    """Remove this test's checkout records (deterministic sale_ids keep this safe)."""
    if not CHECKOUTS_DIR.exists():
        return
    for f in CHECKOUTS_DIR.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        if "TEST" in str(rec.get("thread_id", "")):
            f.unlink()


def test_issue_shape():
    print("issue at list -> issued link:")
    res = checkout.issue(ITEM, THREAD, LIST)
    check("status issued", res["status"] == "issued")
    check("has sale_id", bool(res.get("sale_id")))
    check("url is carousell.ai checkout",
          res["checkout_url"].startswith("https://carousell.ai/checkout/"))
    check("url ends with sale_id", res["checkout_url"].endswith(res["sale_id"]))
    check("currency carried", res.get("currency") == "SGD")
    check("price carried", res.get("price") == LIST)


def test_issue_at_floor_ok():
    print("issue exactly at floor -> issued (floor is acceptable, not below):")
    res = checkout.issue(ITEM, THREAD, FLOOR)
    check("at-floor price issues", res["status"] == "issued")


def test_deterministic_sale_id():
    print("same deal -> same sale_id, record never mutated on re-issue:")
    a = checkout.issue(ITEM, THREAD, LIST)
    b = checkout.issue(ITEM, THREAD, LIST)
    check("sale_id stable", a["sale_id"] == b["sale_id"])
    check("issued_at preserved on re-issue", a["issued_at"] == b["issued_at"])


def test_below_floor_rejected():
    print("price below floor -> rejected, nothing about the floor leaks:")
    proc = _cli("issue", "--item", ITEM, "--thread", THREAD, "--price", str(FLOOR - 10))
    blob = proc.stdout + proc.stderr
    check("nonzero exit below floor", proc.returncode != 0)
    check("no 'floor' token in reject output", "floor" not in blob.lower())
    check("no floor value in reject output", str(FLOOR) not in blob)


def test_floor_never_in_output():
    print("INVARIANT: floor absent from issued output AND the state file:")
    proc = _cli("issue", "--item", ITEM, "--thread", THREAD, "--price", str(LIST))
    check("issue exits 0", proc.returncode == 0)
    payload = json.loads(proc.stdout)
    check("no 'floor' token in stdout", "floor" not in proc.stdout.lower())
    state_blob = (CHECKOUTS_DIR / f"{payload['sale_id']}.json").read_text()
    check("no 'floor' token in state file", "floor" not in state_blob.lower())
    # The floor must not be STORED as a labeled field. We assert the record holds only
    # buyer-visible/safe fields (a bare `str(FLOOR) in blob` check is meaningless — a hash
    # or timestamp can contain those digits, and a legit at-floor close has price == floor).
    rec = json.loads(state_blob)
    safe_keys = {"status", "sale_id", "checkout_url", "item_id",
                 "thread_id", "price", "currency", "issued_at"}
    check("state record exposes only safe fields (no secret floor field)",
          set(rec.keys()) == safe_keys)


def test_state_file_written():
    print("state recorded immutably under data/checkouts:")
    res = checkout.issue(ITEM, THREAD, LIST)
    path = CHECKOUTS_DIR / f"{res['sale_id']}.json"
    check("state file exists", path.exists())
    rec = json.loads(path.read_text())
    check("records item_id", rec["item_id"] == ITEM)
    check("records thread_id", rec["thread_id"] == THREAD)
    check("records checkout_url", rec["checkout_url"] == res["checkout_url"])


def test_bad_input_rejected():
    print("input validation:")
    bad = [
        ["issue", "--item", ITEM, "--thread", THREAD, "--price", "-5"],
        ["issue", "--item", ITEM, "--thread", THREAD, "--price", "abc"],
        ["issue", "--item", "", "--thread", THREAD, "--price", "80"],
        ["issue", "--item", "missing-item-xyz", "--thread", THREAD, "--price", "80"],
        ["issue", "--item", ITEM, "--price", "80"],  # missing --thread
    ]
    ok = True
    for args in bad:
        proc = _cli(*args)
        if proc.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed/missing input exits nonzero", ok)


if __name__ == "__main__":
    print("checkout adversarial tests\n")
    _cleanup()
    test_issue_shape()
    test_issue_at_floor_ok()
    test_deterministic_sale_id()
    test_below_floor_rejected()
    test_floor_never_in_output()
    test_state_file_written()
    test_bad_input_rejected()
    _cleanup()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
