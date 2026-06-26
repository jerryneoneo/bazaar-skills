#!/usr/bin/env python3
"""Registry data-integrity tests for data/marketplaces.json + the region/category filter.

    python3 tests/test_marketplaces.py

Validates the registry file itself (schema, active flows exist, stubs have no flow) and exercises
the documented region/category rule through the real bin/distribution.py (the single source of that
rule) for SG-vs-US sellers and the Poshmark category gate.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import distribution  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def load_registry():
    doc = json.loads((ROOT / "data" / "marketplaces.json").read_text())
    return {m["id"]: m for m in doc["marketplaces"]}


def test_registry_schema():
    print("registry schema:")
    reg = load_registry()
    required = {"id", "display_name", "regions", "categories", "fulfillment",
                "listing_flow", "connector", "status"}
    check("every entry has required fields", all(required <= set(m) for m in reg.values()))
    check("status is active|stub", all(m["status"] in ("active", "stub") for m in reg.values()))
    check("connector.type valid", all(m["connector"]["type"] in ("browser", "api", "mcp")
                                      for m in reg.values()))
    check("regions/categories non-empty lists",
          all(m["regions"] and m["categories"] for m in reg.values()))


def test_active_flows_exist():
    print("active listing flows exist on disk:")
    reg = load_registry()
    for mid, m in reg.items():
        if m["status"] == "active":
            check(f"{mid} flow file present",
                  m["listing_flow"] and (ROOT / m["listing_flow"]).exists())
        else:
            check(f"{mid} stub has null flow", m["listing_flow"] is None)


def test_expected_markets():
    print("the seven active markets this pass:")
    reg = load_registry()
    active = {mid for mid, m in reg.items() if m["status"] == "active"}
    for mid in ("fb", "carousell", "ebay", "mercari", "offerup", "poshmark", "craigslist"):
        check(f"{mid} active", mid in active)


def test_region_filter():
    print("region filter (distribution.region_match):")
    reg = load_registry()
    sg_offered = [mid for mid, m in reg.items()
                  if m["status"] == "active" and distribution.region_match(m, "SG")]
    us_offered = [mid for mid, m in reg.items()
                  if m["status"] == "active" and distribution.region_match(m, "US")]
    check("SG offers carousell", "carousell" in sg_offered)
    check("SG does NOT offer poshmark", "poshmark" not in sg_offered)
    check("US offers poshmark + mercari + offerup", {"poshmark", "mercari", "offerup"} <= set(us_offered))
    check("US does NOT offer carousell", "carousell" not in us_offered)


def test_category_gate():
    print("category gate (no furniture to Poshmark):")
    reg = load_registry()
    posh = reg["poshmark"]
    fb = reg["fb"]
    check("furniture excluded from Poshmark", not distribution.category_match(posh, "furniture"))
    check("fashion allowed on Poshmark", distribution.category_match(posh, "fashion"))
    check("furniture allowed on FB (wildcard)", distribution.category_match(fb, "furniture"))


def test_array_shim():
    print("array->object selection read-shim:")
    reg = load_registry()
    shimmed = distribution._normalize_selection(["fb", "carousell"], reg)
    check("legacy array becomes object", isinstance(shimmed, dict))
    check("each entry enabled", shimmed["fb"]["enabled"] and shimmed["carousell"]["enabled"])
    obj = {"fb": {"enabled": True}}
    check("object selection passes through", distribution._normalize_selection(obj, reg) is obj)


def test_classify_furniture_us():
    print("classify: a furniture item, US seller with Poshmark enabled:")
    reg = load_registry()
    selection = {"fb": {"enabled": True}, "poshmark": {"enabled": True}}
    sets = distribution.classify(reg, selection, "US", "furniture", {})
    check("fb is a cross-list candidate", "fb" in sets["cross_list_candidates"])
    check("poshmark dropped for category",
          any(d["id"] == "poshmark" and d["reason"] == "category" for d in sets["dropped_enabled"]))


if __name__ == "__main__":
    print("marketplace registry tests\n")
    test_registry_schema()
    test_active_flows_exist()
    test_expected_markets()
    test_region_filter()
    test_category_gate()
    test_array_shim()
    test_classify_furniture_us()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
