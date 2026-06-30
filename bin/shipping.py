#!/usr/bin/env python3
"""shipping.py — deterministic P2P delivery fee → the total the buyer pays.

Like floor_gate, this owns a MONEY computation so the model never improvises a fee.
The buyer's total = list_price + delivery_fee + size_surcharge, computed here from the
seller's zone table. Everything ships P2P — there is no meetup/offline fallback, so a
destination outside all serviceable zones returns covered=false (politely decline).

The seller's EXACT origin address lives in seller_config.json and is read only here for
the distance/zone calc — it is never returned to the caller or shown to a buyer.

Usage:
    python3 shipping.py --item <item_id> --dest-area "<area>" [--dest-km <float>]
Output (stdout, JSON):
    {"zone","delivery_fee","size_surcharge","total_fee","price","buyer_total","currency","covered"}
    unserviceable / no match -> covered=false, delivery_fee/total_fee/buyer_total = null

Exit codes: 0 ok · 2 bad input · 3 config/item data missing or invalid.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Data dir is relocatable via SELLY_DATA_DIR (used by tests for isolation), matching the
# convention in control.py / pacing_gate.py / ui_cache.py.
DATA_DIR = Path(os.environ.get("SELLY_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
CONFIG_PATH = DATA_DIR / "seller_config.json"
ITEMS_DIR = DATA_DIR / "items"

CATCH_ALL = "__else__"


def _load_json(path, label):
    if not path.exists():
        raise FileNotFoundError(f"{label} not found at {path}")
    return json.loads(path.read_text())


def resolve_zone(zones, dest_area, dest_km):
    """First matching zone wins. Area match (incl. __else__ catch-all) and distance-band
    match are evaluated in the order the seller listed them. Returns the zone dict or None."""
    area_key = (dest_area or "").strip().lower()
    for zone in zones:
        match = zone.get("match", {})
        areas = [a.lower() for a in match.get("areas", [])]
        if area_key and area_key in areas:
            return zone
        if CATCH_ALL in areas:
            return zone
        if "max_km" in match and dest_km is not None and dest_km <= match["max_km"]:
            return zone
    return None


def compute(zones, size_surcharge, price, size_bucket, currency, dest_area, dest_km):
    """Pure fee computation. buyer_total = price + delivery_fee + size_surcharge."""
    zone = resolve_zone(zones, dest_area, dest_km)
    surcharge = size_surcharge.get(size_bucket, 0)

    # No zone matched, or the matched zone is explicitly unserviceable (fee is null).
    if zone is None or zone.get("fee") is None:
        return {
            "zone": zone["zone"] if zone else None,
            "delivery_fee": None,
            "size_surcharge": surcharge,
            "total_fee": None,
            "price": price,
            "buyer_total": None,
            "currency": currency,
            "covered": False,
        }

    delivery_fee = zone["fee"]
    total_fee = delivery_fee + surcharge
    return {
        "zone": zone["zone"],
        "delivery_fee": delivery_fee,
        "size_surcharge": surcharge,
        "total_fee": total_fee,
        "price": price,
        "buyer_total": price + total_fee,
        "currency": currency,
        "covered": True,
    }


def run(item_id, dest_area, dest_km):
    config = _load_json(CONFIG_PATH, "seller_config.json")
    item = _load_json(ITEMS_DIR / f"{item_id}.json", f"item {item_id!r}")

    shipping = config.get("shipping", {})
    zones = shipping.get("zones")
    if not isinstance(zones, list) or not zones:
        raise ValueError("seller_config.shipping.zones missing or empty")
    size_surcharge = shipping.get("size_surcharge", {})

    price = item.get("list_price")
    if not isinstance(price, (int, float)):
        raise ValueError(f"item {item_id!r} missing numeric list_price")
    size_bucket = item.get("size_bucket", "small")
    currency = config.get("currency") or item.get("currency") or ""

    return compute(zones, size_surcharge, price, size_bucket, currency, dest_area, dest_km)


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="shipping.py", add_help=False)
    parser.add_argument("--item", required=True)
    parser.add_argument("--dest-area", default="")
    parser.add_argument("--dest-km", type=float, default=None)
    ns = parser.parse_args(argv[1:])
    if not ns.item.strip():
        raise ValueError("item is empty")
    if ns.dest_km is not None and ns.dest_km < 0:
        raise ValueError("dest-km must be >= 0")
    if not ns.dest_area.strip() and ns.dest_km is None:
        raise ValueError("provide --dest-area and/or --dest-km")
    return ns.item.strip(), ns.dest_area, ns.dest_km


def main(argv):
    try:
        item_id, dest_area, dest_km = _parse_args(argv)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        result = run(item_id, dest_area, dest_km)
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
