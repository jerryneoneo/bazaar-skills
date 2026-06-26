#!/usr/bin/env python3
"""budget_gate.py — the buyer-side trust boundary (mirror of floor_gate.py).

Decides accept / counter / walk for a SELLER's asking price against the buyer's
HIDDEN max budget. This is the ONLY code that reads data/budgets/<want_id>.json.

Contract: the max_budget value NEVER leaks as a concept in stdout. The model that
drives the conversation calls this script and sees only {decision, counter_price,
walk}. It is told what to offer, never the ceiling it cannot cross — so it has
nothing to leak. (Inverse of the seller's hidden floor: the seller never goes
below the floor; the buyer never goes above the max.)

Usage:
    python3 budget_gate.py <want_id> <seller_price> <round_number>
Output (stdout, JSON):
    {"decision": "counter", "counter_price": 88, "walk": false, "seller_price": 110, "round": 0}

Exit codes: 0 ok · 2 bad input · 3 budget data missing/invalid.
"""

import json
import math
import sys
from pathlib import Path

# Path to the budgets dir, resolved relative to this script (bin/ -> ../data/budgets).
BUDGETS_DIR = Path(__file__).resolve().parent.parent / "data" / "budgets"

VALID_DECISIONS = ("accept", "counter", "walk")


def load_budget_record(want_id):
    """Read the hidden budget record. Raises on missing/invalid — never returns junk."""
    path = BUDGETS_DIR / f"{want_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no budget record for want_id={want_id!r} at {path}")
    record = json.loads(path.read_text())
    target = record.get("target_price")
    max_budget = record.get("max_budget")
    if not isinstance(target, (int, float)) or not isinstance(max_budget, (int, float)):
        raise ValueError(f"budget record for {want_id!r} missing numeric target_price/max_budget")
    if target > max_budget:
        raise ValueError(f"target_price ({target}) exceeds max_budget ({max_budget}) for {want_id!r}")
    return {
        "target": target,
        "max_budget": max_budget,
        "step": record.get("auto_counter_step", 5),
        "max_rounds": record.get("auto_counter_rounds", 2),
        "opening_ratio": record.get("opening_ratio", 0.8),
        "give_up_polls": record.get("give_up_polls", 6),
        "currency": record.get("currency", ""),
    }


def decide(seller_price, round_number, target, max_budget, step, max_rounds):
    """Pure decision function. Returns (decision, counter_price | None, walk).

    Invariant enforced by tests: any counter_price returned is ALWAYS <= max_budget
    (and strictly below it whenever we are haggling up from below), and the
    max_budget value itself is never returned as "the ceiling".
    """
    has_rounds_left = round_number < max_rounds

    # At or below our target: take it (a great price).
    if seller_price <= target:
        return "accept", None, False

    # Within budget (target < price <= max): acceptable. Nudge DOWN while rounds remain.
    if seller_price <= max_budget:
        if has_rounds_left:
            counter = max(target, seller_price - step)
            if counter < seller_price:
                return "counter", int(counter), False
        return "accept", None, False

    # Above budget: counter UP toward (but strictly under) the ceiling; walk once spent.
    if has_rounds_left:
        midpoint = math.floor((target + max_budget) / 2)   # < max_budget since target < max
        counter = min(max_budget, midpoint)
        return "counter", int(counter), False
    return "walk", None, True


def run(want_id, seller_price, round_number):
    rec = load_budget_record(want_id)
    decision, counter_price, walk = decide(
        seller_price, round_number, rec["target"], rec["max_budget"], rec["step"], rec["max_rounds"]
    )
    if decision not in VALID_DECISIONS:
        raise AssertionError(f"invalid decision computed: {decision!r}")
    # Defensive: an offer must never exceed the max budget. Fail loud rather than leak the ceiling.
    if decision == "counter" and counter_price > rec["max_budget"]:
        raise AssertionError("counter_price above max_budget — refusing to emit")
    return {
        "decision": decision,
        "counter_price": counter_price,
        "walk": walk,
        "seller_price": seller_price,
        "round": round_number,
    }


def _parse_args(argv):
    if len(argv) != 4:
        raise ValueError("usage: budget_gate.py <want_id> <seller_price> <round_number>")
    want_id = argv[1].strip()
    if not want_id:
        raise ValueError("want_id is empty")
    try:
        seller_price = float(argv[2])
        round_number = int(argv[3])
    except ValueError as exc:
        raise ValueError(f"seller_price must be a number and round an int: {exc}") from exc
    if seller_price <= 0:
        raise ValueError("seller_price must be positive")
    if round_number < 0:
        raise ValueError("round_number must be >= 0")
    # Normalize whole-dollar prices to int for clean output.
    return want_id, (int(seller_price) if seller_price == int(seller_price) else seller_price), round_number


def main(argv):
    try:
        want_id, seller_price, round_number = _parse_args(argv)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    try:
        result = run(want_id, seller_price, round_number)
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
