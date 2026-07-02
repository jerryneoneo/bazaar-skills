#!/usr/bin/env python3
"""bazaar_args.py — map a SELLY item record to carousell.ai (bazaar) create_listing arguments.

Emits ONLY the buyer-safe listing fields the bazaar MCP ``create_listing`` tool needs, with the money
converted to integer cents in Python (never in the prompt) — the same "deterministic money" rule
``shipping.py`` / ``negotiate.py`` follow. NEVER reads or emits the floor, an address, or the API key
(the key lives in the pass environment; the listing-flow recipe supplies it to the tool separately).

Usage:
    bazaar_args.py --item <item_id>
Output (stdout, JSON):
    {"title","description","price_cents","currency"}
Exit codes: 0 ok · 2 bad input · 3 item/config missing or invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ITEMS_DIR = DATA_DIR / "items"
CONFIG_PATH = DATA_DIR / "seller_config.json"

DEFAULT_CURRENCY = "SGD"


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found at {path}")
    return json.loads(path.read_text())


def to_price_cents(list_price: object) -> int:
    """Dollars (int/float/str) → integer cents, half-up. Rejects non-positive / non-numeric input."""
    try:
        dollars = Decimal(str(list_price))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"list_price is not numeric: {list_price!r}")
    if dollars <= 0:
        raise ValueError(f"list_price must be positive, got {dollars}")
    cents = (dollars * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def build_args(item: dict, config: dict) -> dict:
    """Pure map from an item record (+ seller config for the currency default) to create_listing args."""
    title = (item.get("title") or "").strip()
    if not title:
        raise ValueError("item has no title")
    if item.get("list_price") is None:
        raise ValueError("item has no list_price")
    currency = (item.get("currency") or config.get("currency") or DEFAULT_CURRENCY).strip()
    return {
        "title": title,
        "description": (item.get("description") or "").strip(),
        "price_cents": to_price_cents(item["list_price"]),
        "currency": currency,
    }


def run(item_id: str) -> dict:
    item = _load_json(ITEMS_DIR / f"{item_id}.json", f"item {item_id!r}")
    config = _load_json(CONFIG_PATH, "seller_config.json")
    return build_args(item, config)


def _parse_args(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(prog="bazaar_args.py")
    parser.add_argument("--item", required=True, help="item id (data/items/<id>.json)")
    ns = parser.parse_args(argv[1:])
    item_id = ns.item.strip()
    if not item_id:
        raise ValueError("provide --item <item_id>")
    return item_id


def main(argv: list[str]) -> int:
    try:
        item_id = _parse_args(argv)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        result = run(item_id)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
