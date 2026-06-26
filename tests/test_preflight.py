#!/usr/bin/env python3
"""Tests for bin/preflight.py — the read-only dependency checks + platform abstraction.

    python3 tests/test_preflight.py

Tests the pure check helpers and that the platform module resolves, without invoking the slow
`claude -p` auth probe.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import preflight  # noqa: E402
from platforms import get_platform  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_check_exe():
    print("_check_exe:")
    ok = preflight._check_exe("python3", "needed")
    check("present binary ok", ok["ok"] and ok["detail"] and ok["fix_hint"] == "")
    missing = preflight._check_exe("definitely_not_a_real_binary_xyz", "needed")
    check("missing binary not ok", not missing["ok"])
    check("missing carries a fix hint", bool(missing["fix_hint"]))


def test_platform_resolves():
    print("platform module:")
    plat = get_platform()
    check("has a name", bool(plat.name))
    check("runtime_dir is a path", str(plat.runtime_dir()).endswith("bazaar-skills"))
    hints = plat.path_hints()
    check("path_hints covers python3", "python3" in hints)
    check("supervisor kind set", plat.supervisor_kind() in ("launchd", "task-scheduler"))


def test_chrome_check_shape():
    print("_check_chrome shape:")
    c = preflight._check_chrome()
    check("has name/ok/fix_hint", {"name", "ok", "detail", "fix_hint"} <= set(c))


if __name__ == "__main__":
    print("preflight.py tests\n")
    test_check_exe()
    test_platform_resolves()
    test_chrome_check_shape()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
