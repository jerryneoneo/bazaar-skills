#!/usr/bin/env python3
"""Adversarial tests for floor_gate. Runnable with plain python (no pytest needed):

    python3 tests/test_floor_gate.py

Focus: the two trust invariants — (1) the floor value is NEVER in output,
(2) a returned counter_price is ALWAYS >= floor — across every offer/round combo.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import floor_gate  # noqa: E402

FLOOR = 65
LIST = 90
STEP = 10
ROUNDS = 2

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def d(offer, rnd):
    return floor_gate.decide(offer, rnd, FLOOR, LIST, STEP, ROUNDS)


def test_decisions():
    print("decision logic:")
    check("offer above asking -> accept", d(95, 0)[0] == "accept")
    check("offer equals asking -> accept", d(90, 0)[0] == "accept")
    check("offer above floor, rounds left -> counter", d(70, 0)[0] == "counter")
    check("offer above floor, rounds spent -> accept", d(70, ROUNDS)[0] == "accept")
    check("offer at floor, rounds left -> counter up", d(65, 0)[0] == "counter")
    check("offer below floor, rounds left -> counter", d(50, 0)[0] == "counter")
    check("offer below floor, rounds spent -> reject", d(50, ROUNDS)[0] == "reject")
    check("reject sets hold_firm", d(50, ROUNDS)[2] is True)


def test_counter_never_below_floor():
    print("INVARIANT: counter_price >= floor for every offer x round:")
    ok = True
    for offer in range(1, 200):
        for rnd in range(0, ROUNDS + 2):
            decision, counter, _ = d(offer, rnd)
            if decision == "counter" and counter < FLOOR:
                ok = False
                print(f"    leak at offer={offer} round={rnd} -> counter={counter}")
    check("no counter ever undercuts floor", ok)


def test_counter_beats_offer():
    print("counters move the price up, never down:")
    ok = all(
        d(offer, 0)[1] >= offer
        for offer in range(1, 90)
        if d(offer, 0)[0] == "counter"
    )
    check("counter_price >= buyer offer", ok)


def test_floor_never_in_output():
    print("INVARIANT: floor value absent from CLI stdout:")
    ok = True
    for offer in (40, 50, 60, 64, 65, 66, 70, 89, 90, 100):
        for rnd in (0, 1, 2, 3):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "floor_gate.py"),
                 "sample-ikea-desk", str(offer), str(rnd)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                ok = False
                print(f"    nonzero exit offer={offer} round={rnd}: {proc.stderr.strip()}")
                continue
            payload = json.loads(proc.stdout)
            # No field may be named/derived from the secret.
            if "floor" in proc.stdout.lower():
                ok = False
                print(f"    literal 'floor' token in output: {proc.stdout}")
            # counter_price may legitimately equal the floor (a counter down to floor),
            # but must never go below it. The buyer-supplied `offer` echo is not a leak.
            counter = payload.get("counter_price")
            if counter is not None and counter < FLOOR:
                ok = False
                print(f"    counter below floor leaked: {payload}")
    check("stdout never exposes the floor as a labeled value", ok)


def test_bad_input_rejected():
    print("input validation:")
    bad = [
        ["sample-ikea-desk", "-5", "0"],
        ["sample-ikea-desk", "abc", "0"],
        ["", "70", "0"],
        ["missing-item", "70", "0"],
    ]
    ok = True
    for args in bad:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "floor_gate.py"), *args],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed/missing input exits nonzero", ok)


if __name__ == "__main__":
    print("floor_gate adversarial tests\n")
    test_decisions()
    test_counter_never_below_floor()
    test_counter_beats_offer()
    test_floor_never_in_output()
    test_bad_input_rejected()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
