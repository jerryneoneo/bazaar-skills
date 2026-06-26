#!/usr/bin/env python3
"""Tests for distribution.py — the generalized marketplace filter (3 sets).

    python3 tests/test_distribution.py

Pure classification is tested with an inline registry/selection (independent of live data);
two CLI checks exercise the real files + input validation, and assert no floor/address leak.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import distribution  # noqa: E402

# Inline registry mirroring data/marketplaces.json's shape: a global all-category platform,
# a region-scoped one, a fashion-only active platform, and a fashion-only STUB.
REGISTRY = {
    "fb": {"id": "fb", "display_name": "Facebook Marketplace", "regions": ["*"],
           "categories": ["*"], "status": "active"},
    "carousell": {"id": "carousell", "display_name": "Carousell", "regions": ["SG", "MY"],
                  "categories": ["*"], "status": "active"},
    "ebay": {"id": "ebay", "display_name": "eBay", "regions": ["US", "*"],
             "categories": ["*"], "status": "active"},
    "poshmark": {"id": "poshmark", "display_name": "Poshmark", "regions": ["US", "AU"],
                 "categories": ["fashion", "apparel"], "status": "active"},
    "depop": {"id": "depop", "display_name": "Depop", "regions": ["*"],
              "categories": ["fashion", "apparel"], "status": "stub"},
}

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def cls(selection, region, tag, listing_urls=None):
    return distribution.classify(REGISTRY, selection, region, tag, listing_urls or {})


def test_electronics_sg():
    print("SG electronics, fb+carousell enabled, both already listed:")
    sel = {"fb": {"enabled": True}, "carousell": {"enabled": True}}
    r = cls(sel, "SG", "electronics", {"fb": "u", "carousell": "u"})
    check("already_listed = fb+carousell", set(r["already_listed"]) == {"fb", "carousell"})
    check("nothing to cross-list", r["cross_list_candidates"] == [])
    # ebay is global+all-category and not enabled -> a setup recommendation; poshmark/depop are fashion-only.
    rec = {x["id"] for x in r["recommend_setup"]}
    check("ebay recommended for setup", "ebay" in rec)
    check("poshmark NOT recommended (electronics)", "poshmark" not in rec)


def test_cross_list_candidate():
    print("SG electronics listed only on fb, carousell enabled but not yet listed:")
    sel = {"fb": {"enabled": True}, "carousell": {"enabled": True}}
    r = cls(sel, "SG", "electronics", {"fb": "u"})
    check("carousell is a cross-list candidate", r["cross_list_candidates"] == ["carousell"])
    check("already_listed = fb only", r["already_listed"] == ["fb"])


def test_fashion_recommends_poshmark_and_depop():
    print("US fashion, only fb enabled+listed -> recommend poshmark (active) + depop (stub):")
    sel = {"fb": {"enabled": True}}
    r = cls(sel, "US", "fashion", {"fb": "u"})
    rec = {x["id"]: x["status"] for x in r["recommend_setup"]}
    check("poshmark recommended (active)", rec.get("poshmark") == "active")
    check("depop recommended (stub)", rec.get("depop") == "stub")
    check("ebay recommended (global)", "ebay" in rec)


def test_dropped_enabled_category():
    print("enabled fashion-only platform is dropped for a non-fashion item:")
    sel = {"fb": {"enabled": True}, "poshmark": {"enabled": True}}
    r = cls(sel, "US", "electronics", {})
    dropped = {d["id"]: d["reason"] for d in r["dropped_enabled"]}
    check("poshmark dropped for category", dropped.get("poshmark") == "category")
    check("fb is a cross-list candidate", "fb" in r["cross_list_candidates"])


def test_array_selection_shim():
    print("legacy ARRAY selection is normalized to the object shape:")
    sel = distribution._normalize_selection(["fb", "carousell"], REGISTRY)
    check("array -> enabled fb", sel["fb"]["enabled"] is True)
    r = cls(sel, "SG", "electronics", {})
    check("both become cross-list candidates", set(r["cross_list_candidates"]) == {"fb", "carousell"})


def test_already_listed_never_recommended():
    print("a platform already listed is never a candidate nor a recommendation:")
    sel = {"fb": {"enabled": True}}
    r = cls(sel, "US", "fashion", {"fb": "u", "poshmark": "u"})
    rec = {x["id"] for x in r["recommend_setup"]}
    check("poshmark already listed -> not recommended", "poshmark" not in rec)
    check("poshmark in already_listed", "poshmark" in r["already_listed"])


def test_cli_and_validation():
    print("CLI (real files) + input validation + no secret leak:")
    if (ROOT / "data" / "items" / "sony-wh1000xm5-silver.json").exists():
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "distribution.py"),
             "--item", "sony-wh1000xm5-silver"],
            capture_output=True, text=True,
        )
        ok_cli = proc.returncode == 0
        if ok_cli:
            payload = json.loads(proc.stdout)
            ok_cli = "fb" in payload["already_listed"] and "carousell" in payload["already_listed"]
            # No floor (85) and no exact address must ever surface in distribution output.
            ok_cli = ok_cli and "Sample Road" not in proc.stdout and "000000" not in proc.stdout
        check("CLI classifies real item, no floor/address leak", ok_cli)
    else:
        print("  [SKIP] CLI live-item check — no local item fixture (run after listing something)")

    bad = [
        [],                                  # neither --item nor --category-tag
        ["--item", "nope"],                  # missing item file
        ["--category-tag", ""],              # empty category tag, no item
    ]
    ok_bad = True
    for args in bad:
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "distribution.py"), *args],
                           capture_output=True, text=True)
        if p.returncode == 0:
            ok_bad = False
            print(f"    accepted bad input: {args}")
    check("malformed/missing input exits nonzero", ok_bad)


if __name__ == "__main__":
    print("distribution tests\n")
    test_electronics_sg()
    test_cross_list_candidate()
    test_fashion_recommends_poshmark_and_depop()
    test_dropped_enabled_category()
    test_array_selection_shim()
    test_already_listed_never_recommended()
    test_cli_and_validation()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
