#!/usr/bin/env python3
"""Tests for bin/bazaar_args.py — item record → carousell.ai create_listing args (deterministic).

    python3 tests/test_bazaar_args.py

Money must convert to integer cents in Python (half-up); the currency default falls back to the seller
config and then SGD; missing title/price fail closed (never guessed).
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import bazaar_args as b  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def raises(fn, exc=ValueError):
    try:
        fn()
        return False
    except exc:
        return True


def test_price_cents():
    print("to_price_cents (dollars → integer cents, half-up):")
    check("int dollars", b.to_price_cents(15) == 1500)
    check("float .50", b.to_price_cents(15.5) == 1550)
    check("string dollars", b.to_price_cents("40") == 4000)
    check("half-up at .005", b.to_price_cents("10.005") == 1001)
    check("float noise handled via Decimal(str())", b.to_price_cents(19.99) == 1999)
    check("zero rejected", raises(lambda: b.to_price_cents(0)))
    check("negative rejected", raises(lambda: b.to_price_cents(-5)))
    check("non-numeric rejected", raises(lambda: b.to_price_cents("free")))


def test_build_args():
    print("build_args (item + config → tool args):")
    item = {"title": "IKEA Elloven stand", "description": "ship P2P", "list_price": 15, "currency": "SGD"}
    args = b.build_args(item, {"currency": "SGD"})
    check("title mapped", args["title"] == "IKEA Elloven stand")
    check("description mapped", args["description"] == "ship P2P")
    check("price_cents mapped", args["price_cents"] == 1500)
    check("currency from item", args["currency"] == "SGD")
    check("currency falls back to config",
          b.build_args({"title": "x", "list_price": 5}, {"currency": "USD"})["currency"] == "USD")
    a3 = b.build_args({"title": "x", "list_price": 5}, {})
    check("currency defaults to SGD", a3["currency"] == "SGD")
    check("missing description → empty string", a3["description"] == "")
    check("missing title fails", raises(lambda: b.build_args({"list_price": 5}, {})))
    check("blank title fails", raises(lambda: b.build_args({"title": "  ", "list_price": 5}, {})))
    check("missing price fails", raises(lambda: b.build_args({"title": "x"}, {})))
    check("null price fails", raises(lambda: b.build_args({"title": "x", "list_price": None}, {})))


def test_run_and_cli():
    print("run() + CLI exit codes (against a temp item, real files untouched):")
    tmp = Path(tempfile.mkdtemp())
    items = tmp / "items"
    items.mkdir()
    (items / "demo-item.json").write_text(json.dumps(
        {"title": "Vintage Camera", "description": "good condition", "list_price": 45, "currency": "SGD"}))
    (tmp / "seller_config.json").write_text(json.dumps({"currency": "SGD"}))
    orig_items, orig_cfg = b.ITEMS_DIR, b.CONFIG_PATH
    b.ITEMS_DIR, b.CONFIG_PATH = items, tmp / "seller_config.json"
    try:
        out = b.run("demo-item")
        check("run maps title", out["title"] == "Vintage Camera")
        check("run maps price_cents ($45 → 4500)", out["price_cents"] == 4500)
        check("valid item → exit 0", b.main(["bazaar_args.py", "--item", "demo-item"]) == 0)
        check("missing item → exit 3", b.main(["bazaar_args.py", "--item", "nope"]) == 3)
    finally:
        b.ITEMS_DIR, b.CONFIG_PATH = orig_items, orig_cfg


if __name__ == "__main__":
    print("bazaar_args tests\n")
    test_price_cents()
    test_build_args()
    test_run_and_cli()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
