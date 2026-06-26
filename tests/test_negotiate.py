#!/usr/bin/env python3
"""Adversarial tests for negotiate.py v2 (FCFS + bidding + Harry guard).

    python3 tests/test_negotiate.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import negotiate  # noqa: E402

LIST, FLOOR, STEP = 100, 85, 5
CFG = {"max_counters": 2, "min_offer_ratio": 0.6, "lowball_cap": 3}
ITEM = "__test_neg__"
FLOOR_F = ROOT / "data" / "floors" / f"{ITEM}.json"
ITEM_F = ROOT / "data" / "items" / f"{ITEM}.json"
LEDGER_F = ROOT / "data" / "negotiations" / f"{ITEM}.json"
_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def led(state="open", bidding=False, fr=None, buyers=None):
    return {"item_id": ITEM, "list_price": LIST, "state": state, "is_bidding": bidding,
            "front_runner": fr, "sold_to": None, "buyers": buyers or {}}


def buyer():
    return negotiate._blank_buyer("X")


def d(offer, tid="fb:A", ledger=None, b=None):
    return negotiate.decide(offer, tid, b or buyer(), ledger or led(), FLOOR, LIST, STEP, CFG)


def test_harry_guard():
    print("THE HARRY FIX — above-list never auto-accepts:")
    r = d(200)                       # $200 on a $100 item
    check("$200 → bid_lead (not accept)", r["decision"] == "bid_lead")
    check("needs seller confirmation", r["needs_seller_confirm"] is True)
    check("no 'accept' decision emitted", r["decision"] != "accept_fcfs")
    check("reports the leading amount", r.get("leading_amount") == 200)


def test_fcfs():
    print("FCFS at list:")
    check("at-list, open → accept_fcfs (auto)", d(100)["decision"] == "accept_fcfs")
    check("accept_fcfs needs NO confirm (≤list)", d(100)["needs_seller_confirm"] is False)
    fr = {"thread_id": "fb:OTHER", "amount": 100, "kind": "fcfs"}
    taken = d(100, tid="fb:ME", ledger=led(state="reserved_provisional", fr=fr))
    check("at-list, someone else holds → fcfs_taken", taken["decision"] == "fcfs_taken")


def test_bidding_bar():
    print("bidding reveals the bar:")
    fr = {"thread_id": "fb:TOP", "amount": 150, "kind": "bid"}
    L = led(state="bidding", bidding=True, fr=fr)
    out = d(120, tid="fb:LOW", ledger=L)        # above list but below top bid
    check("120 vs top 150 → bid_outbid", out["decision"] == "bid_outbid")
    check("reveals bar to beat (150)", out["bar_to_beat"] == 150)
    lead = d(160, tid="fb:NEW", ledger=L)
    check("160 beats 150 → bid_lead", lead["decision"] == "bid_lead")
    check("new lead needs confirm", lead["needs_seller_confirm"] is True)


def test_below_list_floor():
    print("below-list haggling stays floor-safe:")
    check("80 → counter", d(80)["decision"] == "counter")
    check("counter ≥ floor and < list", FLOOR <= d(80)["counter_price"] < LIST)
    check("lowball 50 → deflect (no number)", d(50)["decision"] == "deflect_lowball" and d(50).get("counter_price") is None)
    ok = True
    for offer in range(1, 100):
        r = d(offer)
        if r.get("counter_price") is not None and r["counter_price"] < FLOOR:
            ok = False
    check("no counter ever below floor", ok)


def test_stale_counter_respects_other_buyer():
    print("stale prior-counter accept must not undercut a higher active buyer:")
    b_other = negotiate._blank_buyer("B"); b_other["highest_offer"] = 90; b_other["status"] = "active"
    L = led(buyers={"fb:B": b_other})
    a = negotiate._blank_buyer("A"); a["last_counter"] = 85; a["rounds_used"] = 1
    r = negotiate.decide(85, "fb:A", a, L, FLOOR, LIST, STEP, CFG)
    # other_best=90 → effective_min=91; A meeting stale 85 must NOT lock the item at 85
    check("not accepted below rival's standing offer", r["decision"] != "accept_fcfs")
    price = r.get("counter_price") or r.get("accept_price") or 0
    check("any quoted price clears other_best + 1 (>=91)", price >= 91)


# ---- CLI / state transitions on a temp item ----
def setup():
    FLOOR_F.write_text(json.dumps({"item_id": ITEM, "list_price": LIST, "floor": FLOOR,
                                   "auto_counter_step": STEP, "auto_counter_rounds": 2, "currency": "SGD"}))
    ITEM_F.write_text(json.dumps({"item_id": ITEM, "list_price": LIST, "currency": "SGD",
                                  "listing_urls": {"fb": "https://fb/x", "carousell": "https://c/x"}}))
    LEDGER_F.unlink(missing_ok=True)


def teardown():
    for f in (FLOOR_F, ITEM_F, LEDGER_F):
        f.unlink(missing_ok=True)


def cli(*a):
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "negotiate.py"), *a],
                       capture_output=True, text=True)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {}), p.stdout + p.stderr


def test_cli_flow():
    print("CLI state machine + take-down + no floor leak:")
    setup()
    try:
        # A commits at list → FCFS provisional
        _, ra, _ = cli("offer", "--item", ITEM, "--thread", "fb:A", "--buyer", "A", "--offer", "100")
        check("A at list → accept_fcfs", ra["decision"] == "accept_fcfs")
        check("item reserved_provisional", ra["item_state"] == "reserved_provisional")
        # B bids above list → bidding, needs confirm (overtakes FCFS, pre-payment)
        _, rb, raw = cli("offer", "--item", ITEM, "--thread", "fb:B", "--buyer", "B", "--offer", "200")
        check("B $200 → bid_lead + confirm (Harry guard live)",
              rb["decision"] == "bid_lead" and rb["needs_seller_confirm"] is True)
        check("no 'floor' token in CLI output", "floor" not in raw.lower())
        # seller confirms B's bid → reserved for B, others outbid
        _, cb, _ = cli("confirm-bid", "--item", ITEM, "--thread", "fb:B")
        check("confirm-bid reserves for B", cb.get("reserved_for") == "fb:B")
        # payment done → sold + take down the OTHER platform (carousell, since B is fb)
        _, cs, _ = cli("confirm-sold", "--item", ITEM, "--thread", "fb:B")
        check("sold takes down carousell (other platform)",
              any(t["platform"] == "carousell" for t in cs["take_down"])
              and all(t["platform"] != "fb" for t in cs["take_down"]))
        check("A is closed out", "fb:A" in cs["close_threads"])
        # post-sale offer → sold
        _, rc, _ = cli("offer", "--item", ITEM, "--thread", "fb:C", "--buyer", "C", "--offer", "100")
        check("post-sale → sold", rc["decision"] == "sold")
    finally:
        teardown()


if __name__ == "__main__":
    print("negotiate v2 tests\n")
    test_harry_guard()
    test_fcfs()
    test_bidding_bar()
    test_below_list_floor()
    test_stale_counter_respects_other_buyer()
    test_cli_flow()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
