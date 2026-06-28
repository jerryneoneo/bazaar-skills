#!/usr/bin/env python3
"""negotiate.py v2 — negotiation engine with FCFS + bidding (single inventory).

Per-listing state machine, spanning BOTH platforms (one physical item):

  • BELOW list  → negotiate: counter toward list (never below floor), anti-probing (cap +
    sticky), lowball deflection, and never accept below another active buyer's standing offer.
  • AT list     → FCFS: the first buyer to commit at list holds it PROVISIONALLY (until payment).
                  Auto-closeable (close gate = auto for ≤ list). Others are told it's pending.
  • ABOVE list  → BIDDING: highest offer leads, but a leading bid is NEVER auto-accepted —
                  it returns needs_seller_confirm=True so the seller approves it first (this is
                  the guard that would have caught the hallucinated "Harry $200" close). Other
                  buyers are told the bar to beat.
  • SOLD        → final once payment/handover confirmed (then the other listing is taken down).

Secret discipline unchanged: the floor lives only in data/floors/<id>.json, is read only here,
and never appears in output, the ledger, replies, or prompts.

Usage:
  negotiate.py offer        --item ID --thread fb:123 --buyer NAME --offer 120
  negotiate.py confirm-bid  --item ID --thread fb:123     # seller approves a leading bid → reserve
  negotiate.py confirm-sold --item ID --thread fb:123     # payment done → sold + take-down targets
  negotiate.py release      --item ID
  negotiate.py status       --item ID
Exit: 0 ok · 2 bad input · 3 data missing.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # crash-safe writes + cross-process per-item lock (FCFS single-inventory guard)
import floor_gate  # reuse load_floor_record (floor stays here, never leaves)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEDGER_DIR = DATA_DIR / "negotiations"
CONFIG_PATH = DATA_DIR / "config.json"
ITEMS_DIR = DATA_DIR / "items"

DEFAULTS = {"max_counters": 2, "min_offer_ratio": 0.6, "lowball_cap": 3}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _cfg():
    """Resolve negotiation knobs: explicit config.json value > style firmness preset > DEFAULTS.

    The firmness-controlled knobs (max_counters/min_offer_ratio/lowball_cap) are normally absent
    from config.json so the user's data/style.json firmness drives them; a knob pinned in config
    still wins (power-user escape hatch). Fail-open to DEFAULTS if style.py is unavailable."""
    c = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    try:
        import style  # local bin/ module — single source of truth for firmness
        return style.resolve_knobs(c)
    except Exception:  # noqa: BLE001 — never let style resolution break a live negotiation
        return {k: c.get(k, v) for k, v in DEFAULTS.items()}


def _ledger_path(item_id):
    return LEDGER_DIR / f"{item_id}.json"


def load_ledger(item_id, list_price, currency):
    path = _ledger_path(item_id)
    if path.exists():
        led = json.loads(path.read_text())
        led["list_price"] = list_price
        led.setdefault("buyers", {})
        return led
    return {"item_id": item_id, "list_price": list_price, "currency": currency,
            "state": "open", "is_bidding": False, "front_runner": None,
            "sold_to": None, "buyers": {}, "updated_at": _now()}


def save_ledger(led):
    """Atomic write (tmp + os.replace). Callers MUST hold atomic_io.locked(_ledger_path(item_id))
    across the load->mutate->save so two processes can't both win one physical item (FCFS)."""
    led["updated_at"] = _now()
    atomic_io.write_json(_ledger_path(led["item_id"]), led)


def _blank_buyer(handle):
    return {"buyer_handle": handle, "offers": [], "highest_offer": 0, "rounds_used": 0,
            "last_counter": None, "lowball_count": 0, "status": "active"}


def _other_best(led, thread_id):
    best = 0
    for tid, b in led["buyers"].items():
        if tid != thread_id and b.get("status") not in ("passed", "lost"):
            best = max(best, b.get("highest_offer", 0))
    return best


def decide_below_list(offer, buyer, effective_min, list_price, step, max_counters,
                      min_offer_ratio, lowball_cap):
    """< list haggling: counter toward list, never below effective_min, capped + sticky.

    Prices use int() throughout: the marketplace convention here is whole-dollar offers, so int()
    is exact (no rounding loss). If sub-dollar offers are ever supported, revisit these casts."""
    last, rounds = buyer["last_counter"], buyer["rounds_used"]
    if last is not None and offer >= last and offer >= effective_min:   # met counter AND beats other buyers
        return "accept_fcfs", int(offer), False, f"accept:{int(offer)}"
    if rounds >= max_counters:                                   # spent → hold at lowest quoted
        hold = max(last if last is not None else list_price, effective_min)
        return "hold_firm", int(hold), True, f"hold:{int(hold)}"
    if offer < min_offer_ratio * list_price:                     # lowball → no number, no concession
        if buyer["lowball_count"] + 1 >= lowball_cap:
            return "hold_firm", int(last if last is not None else list_price), True, "disengage"
        return "deflect_lowball", None, False, "decline_lowball"
    target = list_price - step * (rounds + 1)
    ceiling = last if last is not None else list_price
    counter = max(effective_min, min(target, ceiling))
    if counter <= offer:
        return "accept_fcfs", int(offer), False, f"accept:{int(offer)}"
    return "counter", int(counter), False, f"counter:{int(counter)}"


def decide(offer, thread_id, buyer, led, floor, list_price, step, cfg):
    """Top-level routing. Returns a dict (floor value never included)."""
    if led["state"] == "sold":
        return {"decision": "sold", "needs_seller_confirm": False, "message_intent": "item_sold"}

    fr = led.get("front_runner")
    top_bid = fr["amount"] if (fr and fr.get("kind") == "bid") else list_price

    # ABOVE LIST → bidding (never auto-accept; seller confirms the leader)
    if offer > list_price:
        current_top = top_bid if led["is_bidding"] else list_price
        if offer > current_top:
            return {"decision": "bid_lead", "leading_amount": int(offer),
                    "bar_to_beat": int(current_top), "needs_seller_confirm": True,
                    "message_intent": f"bid_lead:{int(offer)}"}
        return {"decision": "bid_outbid", "bar_to_beat": int(current_top),
                "needs_seller_confirm": False, "message_intent": f"beat_bar:{int(current_top)}"}

    # AT LIST → FCFS commit
    if offer == list_price:
        if led["is_bidding"]:
            return {"decision": "bid_outbid", "bar_to_beat": int(top_bid),
                    "needs_seller_confirm": False, "message_intent": f"beat_bar:{int(top_bid)}"}
        if fr is None or fr["thread_id"] == thread_id:
            return {"decision": "accept_fcfs", "accept_price": int(list_price),
                    "needs_seller_confirm": False, "message_intent": f"accept:{int(list_price)}"}
        return {"decision": "fcfs_taken", "needs_seller_confirm": False,
                "message_intent": "pending_fcfs"}

    # BELOW LIST → negotiate (also blocked by bidding/another front-runner)
    if led["is_bidding"]:
        return {"decision": "bid_outbid", "bar_to_beat": int(top_bid),
                "needs_seller_confirm": False, "message_intent": f"beat_bar:{int(top_bid)}"}
    if fr is not None and fr["thread_id"] != thread_id:
        return {"decision": "fcfs_taken", "needs_seller_confirm": False,
                "message_intent": "pending_fcfs"}
    effective_min = max(floor, (_other_best(led, thread_id) + 1) if _other_best(led, thread_id) else floor)
    dec, counter, hold, intent = decide_below_list(
        offer, buyer, effective_min, list_price, step,
        cfg["max_counters"], cfg["min_offer_ratio"], cfg["lowball_cap"])
    if counter is not None and counter < floor:                  # defensive (mirrors floor_gate)
        raise AssertionError("price below floor — refusing to emit")
    res = {"decision": dec, "counter_price": counter, "hold_firm": hold,
           "needs_seller_confirm": False, "message_intent": intent}
    if dec == "accept_fcfs":                 # one contract with the at-list branch (line 139):
        res["accept_price"] = int(counter)   # callers read res["accept_price"] for every accept
    return res


def run_offer(item_id, thread_id, handle, offer):
    # Lock spans the whole read-modify-write so two concurrent buyers (FB + Carousell, or two
    # market workers) can never both win one physical item — the FCFS single-inventory guarantee.
    with atomic_io.locked(_ledger_path(item_id)):
        rec = floor_gate.load_floor_record(item_id)
        floor, list_price, step = rec["floor"], rec["list_price"], rec["step"]
        item_path = ITEMS_DIR / f"{item_id}.json"
        currency = json.loads(item_path.read_text()).get("currency", "") if item_path.exists() else ""
        led = load_ledger(item_id, list_price, currency)

        buyer = led["buyers"].get(thread_id) or _blank_buyer(handle)
        buyer["buyer_handle"] = handle
        buyer["offers"].append({"amount": offer, "ts": _now()})
        buyer["highest_offer"] = max(buyer["highest_offer"], int(offer))

        res = decide(offer, thread_id, buyer, led, floor, list_price, step, _cfg())
        d = res["decision"]

        if d == "counter":
            buyer["rounds_used"] += 1
            buyer["last_counter"] = res["counter_price"]
        elif d == "deflect_lowball":
            buyer["lowball_count"] += 1
        elif d == "hold_firm" and res["message_intent"] == "disengage":
            buyer["status"] = "passed"
        elif d == "accept_fcfs":                       # ≤ list commit → provisional FCFS hold (auto)
            buyer["status"] = "front_runner"
            led["front_runner"] = {"thread_id": thread_id, "amount": res["accept_price"],
                                   "kind": "fcfs", "since": _now()}
            led["state"] = "reserved_provisional"
        elif d == "bid_lead":                          # > list → leading bid, awaits seller confirm
            buyer["status"] = "leading_bid"
            led["is_bidding"] = True
            led["state"] = "bidding"
            led["front_runner"] = {"thread_id": thread_id, "amount": res["leading_amount"],
                                   "kind": "bid", "since": _now()}
        elif d == "bid_outbid":
            led["is_bidding"] = True
            led["state"] = "bidding"

        led["buyers"][thread_id] = buyer
        save_ledger(led)
        res["item_state"] = led["state"]
        res["currency"] = currency
        return res


def run_confirm_bid(item_id, thread_id):
    """Seller approved the leading bid → reserve it for that buyer (now closeable)."""
    with atomic_io.locked(_ledger_path(item_id)):
        rec = floor_gate.load_floor_record(item_id)
        led = load_ledger(item_id, rec["list_price"], "")
        fr = led.get("front_runner")
        if not fr or fr["thread_id"] != thread_id:
            return {"error": "thread is not the current leading bid", "item_state": led["state"]}
        led["state"] = "reserved_provisional"
        led["buyers"][thread_id]["status"] = "won"
        for tid, b in led["buyers"].items():
            if tid != thread_id and b.get("status") not in ("passed",):
                b["status"] = "outbid"
        save_ledger(led)
        return {"reserved_for": thread_id, "amount": fr["amount"], "item_state": led["state"],
                "tell_others": "outbid"}


def run_confirm_sold(item_id, thread_id):
    with atomic_io.locked(_ledger_path(item_id)):
        rec = floor_gate.load_floor_record(item_id)
        led = load_ledger(item_id, rec["list_price"], "")
        led["state"] = "sold"
        led["sold_to"] = thread_id
        item_path = ITEMS_DIR / f"{item_id}.json"
        urls = json.loads(item_path.read_text()).get("listing_urls", {}) if item_path.exists() else {}
        won_platform = thread_id.split(":", 1)[0] if ":" in thread_id else None
        take_down = [{"platform": p, "url": u} for p, u in urls.items() if u and p != won_platform]
        close = [t for t, b in led["buyers"].items() if t != thread_id and b.get("status") not in ("lost", "passed")]
        for t in close:
            led["buyers"][t]["status"] = "lost"
        save_ledger(led)
        return {"item_state": "sold", "take_down": take_down, "close_threads": close}


def run_release(item_id):
    with atomic_io.locked(_ledger_path(item_id)):
        rec = floor_gate.load_floor_record(item_id)
        led = load_ledger(item_id, rec["list_price"], "")
        led["state"] = "bidding" if led["is_bidding"] else "open"
        led["front_runner"] = None
        for b in led["buyers"].values():
            if b.get("status") in ("front_runner", "won"):
                b["status"] = "active"
        save_ledger(led)
        return {"item_state": led["state"]}


def run_status(item_id):
    rec = floor_gate.load_floor_record(item_id)
    led = load_ledger(item_id, rec["list_price"], "")
    return {"item_state": led["state"], "is_bidding": led["is_bidding"],
            "front_runner": led.get("front_runner"),
            "buyers": {t: {"status": b["status"], "highest_offer": b["highest_offer"]}
                       for t, b in led["buyers"].items()}}


def _parse(argv):
    p = argparse.ArgumentParser(prog="negotiate.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("offer")
    o.add_argument("--item", required=True); o.add_argument("--thread", required=True)
    o.add_argument("--buyer", default=""); o.add_argument("--offer", required=True, type=float)
    for name in ("confirm-bid", "confirm-sold", "release", "status"):
        s = sub.add_parser(name)
        s.add_argument("--item", required=True)
        if name in ("confirm-bid", "confirm-sold"):
            s.add_argument("--thread", required=True)
    return p.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse(argv)
    except SystemExit:
        return 2
    try:
        if ns.cmd == "offer":
            if ns.offer <= 0:
                raise ValueError("offer must be positive")
            out = run_offer(ns.item.strip(), ns.thread.strip(), ns.buyer.strip(), ns.offer)
        elif ns.cmd == "confirm-bid":
            out = run_confirm_bid(ns.item.strip(), ns.thread.strip())
        elif ns.cmd == "confirm-sold":
            out = run_confirm_sold(ns.item.strip(), ns.thread.strip())
        elif ns.cmd == "release":
            out = run_release(ns.item.strip())
        else:
            out = run_status(ns.item.strip())
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr); return 2
    except (FileNotFoundError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr); return 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
