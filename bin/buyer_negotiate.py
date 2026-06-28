#!/usr/bin/env python3
"""buyer_negotiate.py — buyer-side negotiation engine (mirror/inverse of negotiate.py).

Per-want state machine, spanning MANY sellers (one thing the buyer wants, pursued
across several listings/threads at once — the exact inverse of negotiate.py's
one-item-many-buyers ledger):

  • OPEN a thread → make an aggressive opening offer BELOW the listed price (near the
    buyer's target), never above the hidden max budget.
  • SELLER REPLIES with a price → climb UP toward their ask but stay strictly under the
    secret max_budget; accept the moment their price is within budget (optionally nudge
    once to save a little first); walk away (politely, no number) if they stay firm above
    budget after the allotted rounds.
  • ACCEPT → commit this thread, mark the deal pending the human's payment, and return the
    other pursued threads to close (inverse of the seller's confirm-sold take-down list).

Secret discipline (inverse of the floor): the max budget lives only in
data/budgets/<want_id>.json, is read ONLY via budget_gate.load_budget_record, and never
appears in output, the ledger, replies, or prompts. No emitted offer ever exceeds it.

Usage:
  buyer_negotiate.py open         --want ID --thread fb:123 --seller NAME --listed 120 [--ask N]
  buyer_negotiate.py seller-reply --want ID --thread fb:123 --price 110
  buyer_negotiate.py accept       --want ID --thread fb:123   # commit a struck deal → close others
  buyer_negotiate.py walk         --want ID --thread fb:123
  buyer_negotiate.py status       --want ID
Exit: 0 ok · 2 bad input · 3 data missing.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # crash-safe (tmp + os.replace) JSON writes
import budget_gate  # reuse load_budget_record (the max stays here, never leaves)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEDGER_DIR = DATA_DIR / "buyer_negotiations"


def _now():
    return datetime.now(timezone.utc).isoformat()


def load_ledger(want_id, currency):
    path = LEDGER_DIR / f"{want_id}.json"
    if path.exists():
        led = json.loads(path.read_text())
        led.setdefault("sellers", {})
        return led
    return {"want_id": want_id, "currency": currency, "state": "shopping",
            "committed_thread": None, "sellers": {}, "updated_at": _now()}


def save_ledger(led):
    led["updated_at"] = _now()
    atomic_io.write_json(LEDGER_DIR / f"{led['want_id']}.json", led)


def _blank_seller(handle, listed_price):
    return {"seller_handle": handle, "listed_price": listed_price, "our_offers": [],
            "our_highest_offer": 0, "seller_lowest_ask": None, "rounds_used": 0,
            "last_offer": None, "agreed_price": None, "status": "negotiating"}


def decide_open(listed_price, target, max_budget, opening_ratio):
    """Compute the aggressive opening offer (below list, near target, under the ceiling)."""
    base = opening_ratio * listed_price
    if target is not None:
        base = min(target, base)
    opening = max(1, min(int(round(base)), max_budget))
    # If the listing is already at/under what we'd open with and within budget, just take it.
    if listed_price <= max_budget and opening >= listed_price:
        return {"decision": "accept", "accept_price": int(listed_price),
                "message_intent": f"accept:{int(listed_price)}"}
    return {"decision": "opening_offer", "offer_price": opening,
            "message_intent": f"open:{opening}"}


def run_seed(want_id, thread_id, handle, listed_price, our_last, seller_ask, rounds, currency):
    """Seed a seller ledger entry for a thread the USER already started by hand, WITHOUT
    emitting any offer. Records the user's prior offer as our standing offer so the next
    real `seller-reply` climbs monotonically from there and never lowers or re-opens it.

    Reads no budget record (writes ledger state only) — usable before/independent of the
    secret budget. If the user made no clear numeric offer, pass our_last=None.
    """
    led = load_ledger(want_id, currency)
    seller = led["sellers"].get(thread_id) or _blank_seller(handle, listed_price)
    if handle:
        seller["seller_handle"] = handle
    if listed_price is not None:
        seller["listed_price"] = listed_price
    if seller_ask is not None:
        seller["seller_lowest_ask"] = seller_ask if seller["seller_lowest_ask"] is None \
            else min(seller["seller_lowest_ask"], seller_ask)
    if our_last is not None:
        amt = int(our_last)
        seller["our_offers"].append({"amount": amt, "ts": _now(), "source": "user_prior"})
        seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
        seller["last_offer"] = amt
    if rounds is not None:
        seller["rounds_used"] = max(seller["rounds_used"], int(rounds))
    seller["status"] = "negotiating"
    led["sellers"][thread_id] = seller
    save_ledger(led)
    return {"decision": "seeded", "thread": thread_id, "our_last": seller["last_offer"],
            "seller_lowest_ask": seller["seller_lowest_ask"], "rounds_used": seller["rounds_used"],
            "want_state": led["state"], "currency": led.get("currency", currency)}


def decide_reply(seller_price, seller, led, thread_id, target, max_budget, step, max_rounds):
    """Seller stated `seller_price`. Returns a dict; max_budget never included."""
    if led["state"] == "committed" and led.get("committed_thread") != thread_id:
        return {"decision": "stand_down", "message_intent": "bought_elsewhere"}

    last = seller["last_offer"]
    rounds = seller["rounds_used"]

    # Seller met or beat our standing offer → commit at the lower number.
    if last is not None and seller_price <= last:
        deal = int(min(seller_price, last))
        return {"decision": "accept", "accept_price": deal, "message_intent": f"accept:{deal}"}

    # At or below our target → a great price, take it.
    if seller_price <= target:
        return {"decision": "accept", "accept_price": int(seller_price),
                "message_intent": f"accept:{int(seller_price)}"}

    # Within budget (target < price <= max) → nudge DOWN once if rounds remain, else accept.
    if seller_price <= max_budget:
        if rounds < max_rounds:
            proposed = max(target, seller_price - step)
            if last is not None:
                proposed = max(proposed, last)          # never lower our own offer (monotonic)
            proposed = min(proposed, max_budget)
            if proposed < seller_price:
                return {"decision": "counter", "offer_price": int(proposed),
                        "message_intent": f"counter:{int(proposed)}"}
        return {"decision": "accept", "accept_price": int(seller_price),
                "message_intent": f"accept:{int(seller_price)}"}

    # Above budget → climb toward them, capped STRICTLY under the ceiling; walk once spent.
    if rounds >= max_rounds:
        return {"decision": "walk_away", "message_intent": "walk"}
    base = last if last is not None else 0
    proposed = base + step * (rounds + 1)
    proposed = max(proposed, base)
    proposed = min(proposed, max_budget - 1)            # stay strictly under the ceiling
    proposed = min(proposed, seller_price - 1)          # still under their ask while haggling
    if last is not None and proposed <= last:
        return {"decision": "hold", "offer_price": int(last), "message_intent": f"hold:{int(last)}"}
    return {"decision": "counter", "offer_price": int(proposed),
            "message_intent": f"counter:{int(proposed)}"}


def _guard(res, max_budget):
    """Defensive: no emitted price ever exceeds the max budget (mirror floor_gate)."""
    for key in ("offer_price", "accept_price"):
        if res.get(key) is not None and res[key] > max_budget:
            raise AssertionError("offer above max_budget — refusing to emit")
    return res


def run_open(want_id, thread_id, handle, listed_price, ask):
    rec = budget_gate.load_budget_record(want_id)
    led = load_ledger(want_id, rec["currency"])
    seller = led["sellers"].get(thread_id) or _blank_seller(handle, listed_price)
    seller["seller_handle"] = handle
    seller["listed_price"] = listed_price
    if ask is not None:
        seller["seller_lowest_ask"] = ask if seller["seller_lowest_ask"] is None \
            else min(seller["seller_lowest_ask"], ask)

    res = _guard(decide_open(listed_price, rec["target"], rec["max_budget"], rec["opening_ratio"]),
                 rec["max_budget"])

    if res["decision"] == "opening_offer":
        amt = res["offer_price"]
        seller["our_offers"].append({"amount": amt, "ts": _now()})
        seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
        seller["last_offer"] = amt
    elif res["decision"] == "accept":
        seller["status"] = "deal_pending"
        seller["agreed_price"] = res["accept_price"]

    led["sellers"][thread_id] = seller
    save_ledger(led)
    res["want_state"] = led["state"]
    res["currency"] = rec["currency"]
    return res


def run_seller_reply(want_id, thread_id, seller_price):
    rec = budget_gate.load_budget_record(want_id)
    led = load_ledger(want_id, rec["currency"])
    seller = led["sellers"].get(thread_id) or _blank_seller("", seller_price)
    seller["seller_lowest_ask"] = seller_price if seller["seller_lowest_ask"] is None \
        else min(seller["seller_lowest_ask"], seller_price)

    res = _guard(decide_reply(seller_price, seller, led, thread_id, rec["target"],
                              rec["max_budget"], rec["step"], rec["max_rounds"]), rec["max_budget"])
    d = res["decision"]

    if d == "counter":
        amt = res["offer_price"]
        seller["our_offers"].append({"amount": amt, "ts": _now()})
        seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
        seller["last_offer"] = amt
        seller["rounds_used"] += 1
    elif d == "accept":
        seller["status"] = "deal_pending"
        seller["agreed_price"] = res["accept_price"]
    elif d == "walk_away":
        seller["status"] = "walked"
    # "hold" and "stand_down" leave seller state as-is.

    led["sellers"][thread_id] = seller
    save_ledger(led)
    res["want_state"] = led["state"]
    res["currency"] = rec["currency"]
    return res


def run_accept(want_id, thread_id):
    """Commit a struck deal on this thread → close the other pursued sellers."""
    rec = budget_gate.load_budget_record(want_id)
    led = load_ledger(want_id, rec["currency"])
    seller = led["sellers"].get(thread_id)
    if not seller:
        return {"error": "no such thread on this want", "want_state": led["state"]}
    led["state"] = "committed"
    led["committed_thread"] = thread_id
    seller["status"] = "committed"
    close = [t for t, s in led["sellers"].items()
             if t != thread_id and s.get("status") not in ("walked", "lost", "unavailable")]
    for t in close:
        led["sellers"][t]["status"] = "lost"
    save_ledger(led)
    deal_price = seller.get("agreed_price") or seller.get("last_offer")
    return {"committed_thread": thread_id, "deal_price": deal_price,
            "close_threads": close, "want_state": "committed", "currency": rec["currency"]}


def run_walk(want_id, thread_id):
    rec = budget_gate.load_budget_record(want_id)
    led = load_ledger(want_id, rec["currency"])
    seller = led["sellers"].get(thread_id)
    if not seller:
        return {"error": "no such thread on this want", "want_state": led["state"]}
    seller["status"] = "walked"
    save_ledger(led)
    return {"thread": thread_id, "want_state": led["state"]}


def run_status(want_id):
    rec = budget_gate.load_budget_record(want_id)
    led = load_ledger(want_id, rec["currency"])
    return {"want_state": led["state"], "committed_thread": led.get("committed_thread"),
            "sellers": {t: {"status": s["status"], "our_highest_offer": s["our_highest_offer"],
                            "seller_lowest_ask": s["seller_lowest_ask"]}
                        for t, s in led["sellers"].items()}}


def _parse(argv):
    p = argparse.ArgumentParser(prog="buyer_negotiate.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("--want", required=True)
    o.add_argument("--thread", required=True)
    o.add_argument("--seller", default="")
    o.add_argument("--listed", required=True, type=float)
    o.add_argument("--ask", type=float, default=None)
    r = sub.add_parser("seller-reply")
    r.add_argument("--want", required=True)
    r.add_argument("--thread", required=True)
    r.add_argument("--price", required=True, type=float)
    sd = sub.add_parser("seed")            # adopt a thread the user started by hand (no offer emitted)
    sd.add_argument("--want", required=True)
    sd.add_argument("--thread", required=True)
    sd.add_argument("--seller", default="")
    sd.add_argument("--listed", type=float, default=None)
    sd.add_argument("--our-last", dest="our_last", type=float, default=None)
    sd.add_argument("--seller-ask", dest="seller_ask", type=float, default=None)
    sd.add_argument("--rounds", type=int, default=None)
    sd.add_argument("--currency", default="")
    for name in ("accept", "walk", "status"):
        s = sub.add_parser(name)
        s.add_argument("--want", required=True)
        if name in ("accept", "walk"):
            s.add_argument("--thread", required=True)
    return p.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse(argv)
    except SystemExit:
        return 2
    try:
        if ns.cmd == "open":
            if ns.listed <= 0:
                raise ValueError("listed price must be positive")
            out = run_open(ns.want.strip(), ns.thread.strip(), ns.seller.strip(), ns.listed, ns.ask)
        elif ns.cmd == "seller-reply":
            if ns.price <= 0:
                raise ValueError("price must be positive")
            out = run_seller_reply(ns.want.strip(), ns.thread.strip(), ns.price)
        elif ns.cmd == "seed":
            for label, val in (("--listed", ns.listed), ("--our-last", ns.our_last),
                               ("--seller-ask", ns.seller_ask)):
                if val is not None and val <= 0:
                    raise ValueError(f"{label} must be positive")
            out = run_seed(ns.want.strip(), ns.thread.strip(), ns.seller.strip(),
                           ns.listed, ns.our_last, ns.seller_ask, ns.rounds, ns.currency.strip())
        elif ns.cmd == "accept":
            out = run_accept(ns.want.strip(), ns.thread.strip())
        elif ns.cmd == "walk":
            out = run_walk(ns.want.strip(), ns.thread.strip())
        else:
            out = run_status(ns.want.strip())
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except (FileNotFoundError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
