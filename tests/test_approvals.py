#!/usr/bin/env python3
"""Approval/autonomy config tests.

    python3 tests/test_approvals.py

Validates the data/config.json approvals block, the "Harry guard" (above_list_bids never auto),
and install.py's harness-permission autonomy presets (layer 2). The business-approval presets +
migration shim live as prose in skills/bazaar-config.md; here we test the shipped config + the
real install.py constants.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import install  # noqa: E402

_failures = []

VALID = {"auto", "confirm", "escalate"}
STEPS = {"listing_description", "listing_platforms", "price_floor", "publish", "buyer_replies",
         "offers", "above_list_bids", "mark_sold"}  # distribution is optional/newer


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def load_approvals():
    return json.loads((ROOT / "data" / "config.json").read_text())["approvals"]


def test_config_block():
    print("config.json approvals block:")
    ap = load_approvals()
    check("has preset", ap.get("preset") in ("hands-free", "balanced", "all-steps"))
    steps = ap.get("steps", {})
    check("covers the core steps", STEPS <= set(steps))
    check("all values valid", all(v in VALID for v in steps.values()))


def test_harry_guard():
    print("Harry guard — above_list_bids never auto:")
    steps = load_approvals()["steps"]
    check("above_list_bids is not auto", steps.get("above_list_bids") in ("confirm", "escalate"))


def test_takeover_hard_floor():
    print("takeover gate is present and never auto (step into the user's own chats):")
    steps = load_approvals()["steps"]
    check("takeover present", "takeover" in steps)
    check("takeover is not auto", steps.get("takeover") in ("confirm", "escalate"))


def test_autonomy_presets():
    print("install.py autonomy allow-lists (harness layer):")
    a = install.AUTONOMY_ALLOW
    check("three levels", set(a) == {"hands-free", "balanced", "all-steps"})
    check("hands-free includes data writes", any("Write(data" in t for t in a["hands-free"]))
    check("all-steps omits blanket data writes",
          not any(t.startswith("Write(data") for t in a["all-steps"]))
    check("all include the Playwright tools",
          all(set(install.PLAYWRIGHT_TOOLS) <= set(lst) for lst in a.values()))


def test_migration_shim_reference():
    """A faithful re-implementation of the documented shim (bazaar-config.md) — guards the contract."""
    print("migration shim (autonomy_mode -> steps) reference:")

    def shim(cfg):
        la = cfg.get("listing_autonomy", "")
        auto_list = "auto" if la.startswith("auto") else "confirm"
        return {
            "offers": "auto" if cfg.get("autonomy_mode") == "auto" else "confirm",
            "listing_description": auto_list,
            "listing_platforms": auto_list,
            "publish": auto_list,
            "price_floor": "confirm",
            "buyer_replies": "auto",
            "above_list_bids": "escalate",
            "mark_sold": "confirm",
        }

    legacy = {"autonomy_mode": "auto", "listing_autonomy": "auto_anomaly"}
    derived = shim(legacy)
    check("auto autonomy_mode -> offers auto", derived["offers"] == "auto")
    check("auto listing_autonomy -> publish auto", derived["publish"] == "auto")
    check("above_list_bids always escalate", derived["above_list_bids"] == "escalate")
    assist = shim({"autonomy_mode": "assist", "listing_autonomy": "confirm"})
    check("assist -> offers confirm", assist["offers"] == "confirm")


if __name__ == "__main__":
    print("approval / autonomy config tests\n")
    test_config_block()
    test_harry_guard()
    test_takeover_hard_floor()
    test_autonomy_presets()
    test_migration_shim_reference()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
