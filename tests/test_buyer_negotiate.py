#!/usr/bin/env python3
"""Adversarial tests for buyer_negotiate.py (inverse of negotiate.py).

    python3 tests/test_buyer_negotiate.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import buyer_negotiate  # noqa: E402

TARGET, MAX, STEP, ROUNDS, RATIO = 80, 100, 5, 2, 0.8
WANT = "__test_buyneg__"
BUDGET_F = ROOT / "data" / "budgets" / f"{WANT}.json"
LEDGER_F = ROOT / "data" / "buyer_negotiations" / f"{WANT}.json"
_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def seller(last=None, rounds=0):
    s = buyer_negotiate._blank_seller("X", 120)
    s["last_offer"] = last
    s["rounds_used"] = rounds
    return s


def led(state="shopping", committed=None):
    return {"want_id": WANT, "currency": "SGD", "state": state,
            "committed_thread": committed, "sellers": {}}


def dr(price, s=None, ledger=None, tid="fb:A"):
    return buyer_negotiate.decide_reply(price, s or seller(), ledger or led(), tid,
                                        TARGET, MAX, STEP, ROUNDS)


def test_open_below_list():
    print("opening offer is below list and under the ceiling:")
    r = buyer_negotiate.decide_open(120, TARGET, MAX, RATIO)
    check("listed 120 → opening_offer", r["decision"] == "opening_offer")
    check("opening below list", r["offer_price"] < 120)
    check("opening at/under target", r["offer_price"] <= TARGET)
    check("opening under the ceiling", r["offer_price"] <= MAX)


def test_accept_within_budget():
    print("accept once the seller is within budget:")
    check("78 (<target) → accept", dr(78)["decision"] == "accept")
    check("accept price is the seller's", dr(78)["accept_price"] == 78)
    # within max but above target, rounds spent → accept rather than keep pushing
    spent = dr(95, seller(last=80, rounds=ROUNDS))
    check("95 within max, rounds spent → accept", spent["decision"] == "accept")
    check("accept never above ceiling", spent["accept_price"] <= MAX)


def test_climb_then_walk_above_budget():
    print("above budget: climb under ceiling, then walk:")
    climb = dr(130, seller(last=80, rounds=0))
    check("130 (>max) → counter (climb)", climb["decision"] == "counter")
    check("climb stays strictly under the ceiling", climb["offer_price"] < MAX)
    check("climb moves up from our last offer", climb["offer_price"] > 80)
    walk = dr(130, seller(last=80, rounds=ROUNDS))
    check("130, rounds spent → walk_away", walk["decision"] == "walk_away")
    check("walk emits no number", "offer_price" not in walk and "accept_price" not in walk)


def test_seller_meets_our_offer():
    print("seller meets/beats our standing offer → accept at the lower number:")
    r = dr(80, seller(last=85, rounds=1))
    check("seller 80 vs our 85 → accept", r["decision"] == "accept")
    check("accept at the lower (80)", r["accept_price"] == 80)


def test_stand_down_when_committed_elsewhere():
    print("committed elsewhere → stand down:")
    r = dr(90, seller(), led(state="committed", committed="fb:OTHER"), tid="fb:A")
    check("committed to another thread → stand_down", r["decision"] == "stand_down")


def test_never_exceeds_ceiling():
    print("secrecy: no emitted price ever exceeds the hidden max:")
    ok = True
    for price in range(1, 2 * MAX + 1):
        for last in (None, 80, 95):
            for rounds in range(0, ROUNDS + 1):
                r = dr(price, seller(last=last, rounds=rounds))
                for key in ("offer_price", "accept_price"):
                    if r.get(key) is not None and r[key] > MAX:
                        ok = False
    check("no offer/accept across the sweep exceeds max", ok)


# ---- CLI state machine + close-others + no leak ----
def setup():
    BUDGET_F.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_F.write_text(json.dumps({"want_id": WANT, "target_price": TARGET, "max_budget": MAX,
                                    "auto_counter_step": STEP, "auto_counter_rounds": ROUNDS,
                                    "opening_ratio": RATIO, "currency": "SGD"}))
    LEDGER_F.unlink(missing_ok=True)


def teardown():
    BUDGET_F.unlink(missing_ok=True)
    LEDGER_F.unlink(missing_ok=True)


def cli(*a):
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "buyer_negotiate.py"), *a],
                       capture_output=True, text=True)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {}), p.stdout + p.stderr


def test_cli_flow():
    print("CLI: open two sellers, close a deal, stand the other down, never leak the ceiling:")
    setup()
    raws = []
    try:
        _, oa, r1 = cli("open", "--want", WANT, "--thread", "fb:A", "--seller", "A", "--listed", "120")
        raws.append(r1)
        check("open A → opening_offer below list", oa["decision"] == "opening_offer" and oa["offer_price"] < 120)
        _, ob, r2 = cli("open", "--want", WANT, "--thread", "fb:B", "--seller", "B", "--listed", "110")
        raws.append(r2)
        check("open B → opening_offer", ob["decision"] == "opening_offer")
        _, c1, r3 = cli("seller-reply", "--want", WANT, "--thread", "fb:A", "--price", "95")
        raws.append(r3)
        check("A asks 95 (within max) → counter under ceiling", c1["decision"] == "counter" and c1["offer_price"] <= MAX)
        _, ac, r4 = cli("seller-reply", "--want", WANT, "--thread", "fb:A", "--price", "88")
        raws.append(r4)
        check("A drops to 88 (meets our climb) → accept", ac["decision"] == "accept" and ac["accept_price"] <= MAX)
        _, cm, r5 = cli("accept", "--want", WANT, "--thread", "fb:A")
        raws.append(r5)
        check("accept commits A", cm["want_state"] == "committed" and cm["committed_thread"] == "fb:A")
        check("accept closes the other pursued seller (fb:B)", "fb:B" in cm["close_threads"])
        _, sd, r6 = cli("seller-reply", "--want", WANT, "--thread", "fb:B", "--price", "100")
        raws.append(r6)
        check("B replies after commit → stand_down", sd["decision"] == "stand_down")
        check("no max/budget/floor/ceiling token across all CLI output",
              not any(t in "".join(raws).lower() for t in ("max", "budget", "floor", "ceiling")))
    finally:
        teardown()


def test_seed_then_resume_monotonic():
    print("seed adopts a user-started thread; resume never lowers below the user's offer, no leak:")
    setup()
    raws = []
    try:
        # The user already offered 80 by hand; the seller last asked 95.
        _, sd, r0 = cli("seed", "--want", WANT, "--thread", "fb:S", "--seller", "S",
                        "--listed", "120", "--our-last", "80", "--seller-ask", "95", "--currency", "SGD")
        raws.append(r0)
        check("seed emits NO offer (decision=seeded)", sd.get("decision") == "seeded")
        check("seed records the user's prior offer as our standing offer", sd.get("our_last") == 80)
        # Seller now replies 90 (within max=100, above target=80) → must not undercut the user's 80.
        _, c1, r1 = cli("seller-reply", "--want", WANT, "--thread", "fb:S", "--price", "90")
        raws.append(r1)
        if c1["decision"] == "counter":
            check("counter never below the user's standing 80", c1["offer_price"] >= 80)
            check("counter under the ceiling", c1["offer_price"] <= MAX)
        else:
            check("accept within budget", c1["decision"] == "accept" and c1["accept_price"] <= MAX)
        check("no max/budget/floor/ceiling token across seed+resume output",
              not any(t in "".join(raws).lower() for t in ("max", "budget", "floor", "ceiling")))
    finally:
        teardown()


if __name__ == "__main__":
    print("buyer_negotiate tests\n")
    test_open_below_list()
    test_accept_within_budget()
    test_climb_then_walk_above_budget()
    test_seller_meets_our_offer()
    test_stand_down_when_committed_elsewhere()
    test_never_exceeds_ceiling()
    test_seed_then_resume_monotonic()
    test_cli_flow()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
