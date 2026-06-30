#!/usr/bin/env python3
"""Tests for shipping.py — deterministic delivery fee + buyer total.

    python3 tests/test_shipping.py

Pure functions are tested with inline zone tables (independent of the live config);
two CLI checks exercise the real files + input validation.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import shipping  # noqa: E402

# Inline table: an AREA zone listed BEFORE distance bands to prove ordering/precedence.
ZONES = [
    {"zone": "home-area", "match": {"areas": ["Bishan", "Toa Payoh"]}, "fee": 3},
    {"zone": "near", "match": {"max_km": 8}, "fee": 4},
    {"zone": "mid", "match": {"max_km": 20}, "fee": 7},
    {"zone": "far", "match": {"max_km": 9999}, "fee": 12},
    {"zone": "unserviceable", "match": {"areas": ["__else__"]}, "fee": None},
]
SURCHARGE = {"small": 0, "medium": 2, "large": 5, "bulky": 10}

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def comp(price, bucket, area="", km=None):
    return shipping.compute(ZONES, SURCHARGE, price, bucket, "SGD", area, km)


def test_zone_resolution():
    print("zone resolution (first match wins, area before distance):")
    check("named area matches its zone", comp(90, "small", area="Bishan")["zone"] == "home-area")
    check("area match beats distance", comp(90, "small", area="Bishan", km=3)["zone"] == "home-area")
    check("3km -> near", comp(90, "small", km=3)["zone"] == "near")
    check("15km -> mid", comp(90, "small", km=15)["zone"] == "mid")
    check("500km -> far", comp(90, "small", km=500)["zone"] == "far")
    check("unknown area, no km -> unserviceable", comp(90, "small", area="Mars")["zone"] == "unserviceable")


def test_buyer_total():
    print("buyer_total = price + delivery_fee + size_surcharge:")
    r = comp(90, "bulky", km=3)            # near=4 + bulky=10
    check("near + bulky fee = 14", r["total_fee"] == 14)
    check("buyer_total = 104", r["buyer_total"] == 104)
    check("covered true", r["covered"] is True)
    r2 = comp(50, "small", area="Bishan")  # home-area=3 + small=0
    check("home-area + small total = 53", r2["buyer_total"] == 53)


def test_unserviceable():
    print("unserviceable -> decline (no offline fallback):")
    r = comp(90, "medium", area="Mars")
    check("covered false", r["covered"] is False)
    check("delivery_fee null", r["delivery_fee"] is None)
    check("total_fee null", r["total_fee"] is None)
    check("buyer_total null", r["buyer_total"] is None)


def test_buyer_total_invariant():
    print("INVARIANT: covered -> buyer_total == price + total_fee, for many combos:")
    ok = True
    for price in range(10, 200, 7):
        for bucket in SURCHARGE:
            for km in (3, 15, 500):
                r = comp(price, bucket, km=km)
                if r["covered"] and r["buyer_total"] != r["price"] + r["total_fee"]:
                    ok = False
    check("buyer_total always equals price + total_fee", ok)


def test_cli_and_validation():
    print("CLI (hermetic fixture data dir via SELLY_DATA_DIR) + input validation:")
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / "items").mkdir()
        (data / "seller_config.json").write_text(json.dumps({
            "currency": "SGD",
            "shipping": {
                "zones": [
                    {"zone": "near", "match": {"max_km": 8}, "fee": 4},
                    {"zone": "far", "match": {"max_km": 9999}, "fee": 12},
                ],
                "size_surcharge": {"small": 0, "bulky": 10},
            },
        }))
        (data / "items" / "sample-ikea-desk.json").write_text(json.dumps({
            "item_id": "sample-ikea-desk", "list_price": 90,
            "size_bucket": "bulky", "currency": "SGD",
        }))
        env = {**os.environ, "SELLY_DATA_DIR": str(data)}

        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "shipping.py"),
             "--item", "sample-ikea-desk", "--dest-km", "5"],
            capture_output=True, text=True, env=env,
        )
        ok_cli = proc.returncode == 0
        if ok_cli:
            payload = json.loads(proc.stdout)
            # sample item is bulky (10), near band fee 4 -> total 14; list 90 -> 104
            ok_cli = payload["buyer_total"] == payload["price"] + payload["total_fee"] == 104
            # The exact origin address must never surface in shipping output.
            ok_cli = ok_cli and "570123" not in proc.stdout and "Bishan St" not in proc.stdout
        check("CLI computes against fixture config/item, no address leak", ok_cli)

        bad = [
            ["--item", "sample-ikea-desk"],          # no area and no km
            ["--item", "", "--dest-km", "5"],        # empty item
            ["--item", "nope", "--dest-km", "5"],    # missing item file
            ["--item", "sample-ikea-desk", "--dest-km", "-1"],  # negative km
        ]
        ok_bad = True
        for args in bad:
            p = subprocess.run([sys.executable, str(ROOT / "bin" / "shipping.py"), *args],
                               capture_output=True, text=True, env=env)
            if p.returncode == 0:
                ok_bad = False
                print(f"    accepted bad input: {args}")
        check("malformed/missing input exits nonzero", ok_bad)


if __name__ == "__main__":
    print("shipping tests\n")
    test_zone_resolution()
    test_buyer_total()
    test_unserviceable()
    test_buyer_total_invariant()
    test_cli_and_validation()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
