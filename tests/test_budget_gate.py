#!/usr/bin/env python3
"""Adversarial tests for budget_gate.py — the buyer-side hidden-ceiling boundary.

    python3 tests/test_budget_gate.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import budget_gate  # noqa: E402

TARGET, MAX, STEP, ROUNDS = 80, 100, 5, 2
WANT = "__test_budget__"
BUDGET_F = ROOT / "data" / "budgets" / f"{WANT}.json"
_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def dec(price, rnd=0):
    return budget_gate.decide(price, rnd, TARGET, MAX, STEP, ROUNDS)


def test_accept_at_or_below_target():
    print("accept at/below target:")
    d, c, w = dec(75)
    check("75 (<target) → accept", d == "accept")
    check("accept emits no counter", c is None)
    check("accept never walks", w is False)
    check("80 (==target) → accept", dec(80)[0] == "accept")


def test_within_budget_nudges_down():
    print("within budget: nudge down, then accept once spent:")
    d, c, w = dec(95)
    check("95 (within max) → counter", d == "counter")
    check("counter sits below the ask", c < 95)
    check("counter never below target", c >= TARGET)
    check("counter never above max", c <= MAX)
    check("95 at last round → accept", dec(95, ROUNDS)[0] == "accept")


def test_above_budget_climbs_then_walks():
    print("above budget: climb under the ceiling, then walk:")
    d, c, w = dec(130)
    check("130 (>max) → counter (climb)", d == "counter")
    check("climb stays strictly under the ceiling", c < MAX)
    check("130 at last round → walk", dec(130, ROUNDS)[0] == "walk")
    check("walk emits no number", dec(130, ROUNDS)[1] is None)


def test_never_exceeds_ceiling():
    print("secrecy: no offer across the whole range exceeds the hidden max:")
    ok = True
    for price in range(1, 2 * MAX + 1):
        for rnd in range(0, ROUNDS + 1):
            _, c, _ = dec(price, rnd)
            if c is not None and c > MAX:
                ok = False
    check("no counter ever exceeds max", ok)


# ---- CLI / secrecy on a temp budget ----
def setup():
    BUDGET_F.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_F.write_text(json.dumps({"want_id": WANT, "target_price": TARGET, "max_budget": MAX,
                                    "auto_counter_step": STEP, "auto_counter_rounds": ROUNDS,
                                    "opening_ratio": 0.8, "currency": "SGD"}))


def teardown():
    BUDGET_F.unlink(missing_ok=True)


def cli(*a):
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "budget_gate.py"), *a],
                       capture_output=True, text=True)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {}), p.stdout + p.stderr


def test_cli_and_no_leak():
    print("CLI + the ceiling never appears in output:")
    setup()
    try:
        rc, out, raw = cli(WANT, "110", "0")             # 110 > max → climb
        check("exit 0", rc == 0)
        check("110 → counter (climb under ceiling)", out["decision"] == "counter")
        check("emitted counter <= max", out["counter_price"] <= MAX)
        check("no max/budget/ceiling/floor token in output",
              not any(t in raw.lower() for t in ("max", "budget", "ceiling", "floor")))
        rc2, _, _ = cli("__nope__", "50", "0")
        check("missing budget record → exit 3", rc2 == 3)
        rc3, _, _ = cli(WANT, "-5", "0")
        check("negative price → exit 2", rc3 == 2)
    finally:
        teardown()


if __name__ == "__main__":
    print("budget_gate tests\n")
    test_accept_at_or_below_target()
    test_within_budget_nudges_down()
    test_above_budget_climbs_then_walks()
    test_never_exceeds_ceiling()
    test_cli_and_no_leak()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
