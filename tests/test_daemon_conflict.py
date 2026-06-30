#!/usr/bin/env python3
"""Tests for daemon_conflict.py — the interactive-vs-daemon single-consumer guard.

    python3 tests/test_daemon_conflict.py

Covers the pure conflict decision and the fail-open bound_channel reader (temp SELLY_DATA_DIR).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import daemon_conflict as dc  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_assess():
    print("assess (pure conflict rule):")
    check("daemon + telegram -> conflict", dc.assess(True, "telegram")["conflict"] is True)
    check("daemon + whatsapp -> conflict", dc.assess(True, "whatsapp")["conflict"] is True)
    check("daemon + console -> no conflict", dc.assess(True, "console")["conflict"] is False)
    check("daemon + unset channel -> no conflict", dc.assess(True, "")["conflict"] is False)
    check("no daemon + telegram -> no conflict", dc.assess(False, "telegram")["conflict"] is False)
    r = dc.assess(True, "telegram")
    check("conflict reason names the channel", "telegram" in r["reason"])
    check("conflict reason suggests stopping the daemon", "uninstall" in r["reason"])


def test_bound_channel():
    print("bound_channel reads seller then buyer config (fail-open):")
    orig = os.environ.get("SELLY_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SELLY_DATA_DIR"] = tmp
        try:
            check("no config -> ''", dc.bound_channel() == "")
            (Path(tmp) / "buyer_config.json").write_text(json.dumps(
                {"channel": {"adapter": "telegram"}}))
            check("falls back to buyer_config", dc.bound_channel() == "telegram")
            (Path(tmp) / "seller_config.json").write_text(json.dumps(
                {"channel": {"adapter": "console"}}))
            check("prefers seller_config", dc.bound_channel() == "console")
            (Path(tmp) / "seller_config.json").write_text("{not json")
            check("corrupt seller_config -> falls through to buyer", dc.bound_channel() == "telegram")
        finally:
            if orig is None:
                os.environ.pop("SELLY_DATA_DIR", None)
            else:
                os.environ["SELLY_DATA_DIR"] = orig


def test_exit_code():
    print("main() exit code reflects conflict:")
    orig_loaded, orig_chan = dc.agent_loaded, dc.bound_channel
    dc.agent_loaded = lambda: True
    dc.bound_channel = lambda: "telegram"
    try:
        check("conflict -> exit 1", dc.main(["daemon_conflict.py"]) == 1)
    finally:
        dc.agent_loaded, dc.bound_channel = orig_loaded, orig_chan
    dc.agent_loaded = lambda: False
    dc.bound_channel = lambda: "telegram"
    try:
        check("no daemon -> exit 0", dc.main(["daemon_conflict.py"]) == 0)
    finally:
        dc.agent_loaded, dc.bound_channel = orig_loaded, orig_chan


if __name__ == "__main__":
    print("daemon_conflict.py tests\n")
    test_assess()
    test_bound_channel()
    test_exit_code()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
