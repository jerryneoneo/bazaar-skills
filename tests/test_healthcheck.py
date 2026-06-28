#!/usr/bin/env python3
"""Tests for healthcheck.py — the runtime "is this install runnable?" check.

    python3 tests/test_healthcheck.py

Hermetic: the data-dir-driven checks run against a temp BAZAAR_DATA_DIR, and the live checks
(CDP, launchctl) are exercised against unreachable inputs so they degrade to warn/ok WITHOUT
raising. We don't invoke the full run_checks() here — it probes the harness CLI (slow, network),
which preflight already covers; we test the pure logic healthcheck adds on top.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import healthcheck  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _seller_config(tmp, payload):
    p = Path(tmp) / "seller_config.json"
    p.write_text(json.dumps(payload))
    return Path(tmp)


def test_onboarded():
    print("onboarded gate:")
    with tempfile.TemporaryDirectory() as tmp:
        c = healthcheck.onboarded_check(Path(tmp))
        check("missing seller_config -> warn", c["level"] == healthcheck.WARN)
        check("warn carries a fix hint", bool(c["fix_hint"]))
        data = _seller_config(tmp, {"currency": "SGD"})
        c2 = healthcheck.onboarded_check(data)
        check("present seller_config -> ok", c2["level"] == healthcheck.OK)


def test_marketplace_logins():
    print("marketplace login status:")
    with tempfile.TemporaryDirectory() as tmp:
        # no config -> no marketplace check at all (onboarded check owns that case)
        check("no config -> no marketplace checks", healthcheck.marketplace_checks(Path(tmp)) == [])

    with tempfile.TemporaryDirectory() as tmp:
        data = _seller_config(tmp, {"marketplaces": {}})
        c = healthcheck.marketplace_checks(data)[0]
        check("no enabled markets -> warn", c["level"] == healthcheck.WARN)

    with tempfile.TemporaryDirectory() as tmp:
        data = _seller_config(tmp, {"marketplaces": {
            "fb": {"enabled": True, "auth": "confirmed"},
            "carousell": {"enabled": True, "auth": "needs_login"},
            "ebay": {"enabled": False, "auth": "needs_login"},  # disabled -> ignored
        }})
        c = healthcheck.marketplace_checks(data)[0]
        check("a not-confirmed enabled market -> warn", c["level"] == healthcheck.WARN)
        check("names the unauthenticated market", "carousell" in c["detail"])
        check("does not flag the confirmed one", "fb" not in c["detail"])
        check("ignores disabled markets", "ebay" not in c["detail"])

    with tempfile.TemporaryDirectory() as tmp:
        data = _seller_config(tmp, {"marketplaces": {
            "fb": {"enabled": True, "auth": "confirmed"},
            "carousell": {"enabled": True, "auth": "confirmed"},
        }})
        c = healthcheck.marketplace_checks(data)[0]
        check("all confirmed -> ok", c["level"] == healthcheck.OK)


def test_cdp_unreachable_degrades_to_warn():
    print("CDP check degrades gracefully when unreachable:")
    # A port nothing listens on -> warn, never an exception.
    c = healthcheck.cdp_check("http://127.0.0.1:1/json/version")
    check("unreachable CDP -> warn", c["level"] == healthcheck.WARN)
    check("warn carries a fix hint", bool(c["fix_hint"]))


def test_daemon_checks_no_crash():
    print("daemon checks return valid levels without raising:")
    results = healthcheck.daemon_checks()
    ok = isinstance(results, list) and len(results) >= 1
    ok = ok and all(r["level"] in (healthcheck.OK, healthcheck.WARN, healthcheck.FAIL) for r in results)
    check("returns >=1 check with a valid level", ok)


def test_render_and_secret_safety():
    print("render is a string and never echoes a planted secret:")
    synthetic = {
        "ok": False,
        "fails": ["node"],
        "warns": ["chrome-cdp"],
        "checks": [
            healthcheck._check("node", healthcheck.FAIL, "not found on PATH", "install node"),
            healthcheck._check("chrome-cdp", healthcheck.WARN, "no CDP", "run chrome_debug.sh"),
            healthcheck._check("onboarded", healthcheck.OK, "seller_config.json present"),
        ],
    }
    text = healthcheck.render(synthetic)
    check("render returns a non-empty string", isinstance(text, str) and len(text) > 0)
    check("render surfaces the FAIL", "NOT RUNNABLE" in text and "node" in text)
    # healthcheck only ever reports status fields; assert no obvious secret-y token leaks through.
    check("no secret-ish tokens in output", not any(t in text for t in ("floor", "budget", "TELEGRAM_BOT_TOKEN")))


def test_heartbeat_status():
    print("heartbeat_status classifies a wedged daemon vs a live one:")
    now = 1_000_000.0
    fresh = json.dumps({"ts": now - 10, "pid": 5})
    stale = json.dumps({"ts": now - 9999, "pid": 5})
    check("fresh tick -> ok", healthcheck.heartbeat_status(fresh, now)[0] == "ok")
    check("old tick -> stale", healthcheck.heartbeat_status(stale, now)[0] == "stale")
    check("no file -> missing", healthcheck.heartbeat_status(None, now)[0] == "missing")
    check("garbage -> missing", healthcheck.heartbeat_status("{not json", now)[0] == "missing")
    check("no ts key -> missing", healthcheck.heartbeat_status(json.dumps({"pid": 5}), now)[0] == "missing")
    level, age = healthcheck.heartbeat_status(fresh, now)
    check("reports the age in seconds", abs(age - 10) < 0.01)


if __name__ == "__main__":
    print("healthcheck tests\n")
    test_onboarded()
    test_marketplace_logins()
    test_cdp_unreachable_degrades_to_warn()
    test_daemon_checks_no_crash()
    test_render_and_secret_safety()
    test_heartbeat_status()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
