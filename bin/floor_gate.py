#!/usr/bin/env python3
"""floor_gate.py — the trust boundary.

Decides accept / counter / reject for a buyer's price offer against the
seller's HIDDEN floor. This is the ONLY code that reads data/floors/<item_id>.json.

Contract: the floor value NEVER appears in stdout. The model that drives the
conversation calls this script and sees only {decision, counter_price, hold_firm}.
It is told what to say, never the line it cannot cross — so it has nothing to leak.

Usage:
    python3 floor_gate.py <item_id> <buyer_offer> <round_number>
Output (stdout, JSON):
    {"decision": "counter", "counter_price": 80, "hold_firm": false, "offer": 70, "round": 0}

Exit codes: 0 ok · 2 bad input · 3 floor data missing/invalid.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # harden the hidden floor file to owner-only on read (it holds the secret floor)

# Path to the floors dir, resolved relative to this script (bin/ -> ../data/floors).
FLOORS_DIR = Path(__file__).resolve().parent.parent / "data" / "floors"

VALID_DECISIONS = ("accept", "counter", "reject")


def load_floor_record(item_id):
    """Read the hidden floor record. Raises on missing/invalid — never returns junk."""
    path = FLOORS_DIR / f"{item_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no floor record for item_id={item_id!r} at {path}")
    atomic_io.harden(path)  # the floor is confidential — never leave it world-readable
    record = json.loads(path.read_text())
    floor = record.get("floor")
    list_price = record.get("list_price")
    if not isinstance(floor, (int, float)) or not isinstance(list_price, (int, float)):
        raise ValueError(f"floor record for {item_id!r} missing numeric floor/list_price")
    if floor > list_price:
        raise ValueError(f"floor ({floor}) exceeds list_price ({list_price}) for {item_id!r}")
    return {
        "floor": floor,
        "list_price": list_price,
        "step": record.get("auto_counter_step", 10),
        "max_rounds": record.get("auto_counter_rounds", 2),
    }


def decide(offer, round_number, floor, list_price, step, max_rounds):
    """Pure decision function. Returns (decision, counter_price | None, hold_firm).

    Invariant enforced by tests: any counter_price returned is ALWAYS >= floor,
    and the floor value itself is never part of the return.
    """
    has_rounds_left = round_number < max_rounds

    # At or above asking: take it.
    if offer >= list_price:
        return "accept", None, False

    # At or above floor (but below asking): acceptable. Nudge up while rounds remain.
    if offer >= floor:
        if has_rounds_left:
            counter = min(list_price, offer + step)
            if counter > offer:
                return "counter", int(counter), False
        return "accept", None, False

    # Below floor: counter toward the middle but never below floor; reject once spent.
    if has_rounds_left:
        midpoint = math.ceil((offer + list_price) / 2)
        counter = max(floor, midpoint)
        return "counter", int(counter), False
    return "reject", None, True


def run(item_id, offer, round_number):
    rec = load_floor_record(item_id)
    decision, counter_price, hold_firm = decide(
        offer, round_number, rec["floor"], rec["list_price"], rec["step"], rec["max_rounds"]
    )
    if decision not in VALID_DECISIONS:
        raise AssertionError(f"invalid decision computed: {decision!r}")
    # Defensive: a counter must never undercut the floor. Fail loud rather than leak value.
    if decision == "counter" and counter_price < rec["floor"]:
        raise AssertionError("counter_price below floor — refusing to emit")
    return {
        "decision": decision,
        "counter_price": counter_price,
        "hold_firm": hold_firm,
        "offer": offer,
        "round": round_number,
    }


def _parse_args(argv):
    if len(argv) != 4:
        raise ValueError("usage: floor_gate.py <item_id> <buyer_offer> <round_number>")
    item_id = argv[1].strip()
    if not item_id:
        raise ValueError("item_id is empty")
    try:
        offer = float(argv[2])
        round_number = int(argv[3])
    except ValueError as exc:
        raise ValueError(f"offer must be a number and round an int: {exc}") from exc
    if offer <= 0:
        raise ValueError("buyer_offer must be positive")
    if round_number < 0:
        raise ValueError("round_number must be >= 0")
    # Normalize whole-dollar offers to int for clean output.
    return item_id, (int(offer) if offer == int(offer) else offer), round_number


def main(argv):
    try:
        item_id, offer, round_number = _parse_args(argv)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    try:
        result = run(item_id, offer, round_number)
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
