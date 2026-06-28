#!/usr/bin/env python3
"""Tests for atomic_io.py — crash-safe writes, owner-only hardening, and the per-file lock used by
the money/state ledgers.

    python3 tests/test_atomic_io.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import atomic_io  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def test_write_json_roundtrip_and_atomic():
    print("write_json writes valid JSON, creates parents, leaves no temp file:")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nested" / "ledger.json"
        atomic_io.write_json(path, {"state": "open", "amount": 96})
        check("file exists", path.exists())
        check("content round-trips", json.loads(path.read_text()) == {"state": "open", "amount": 96})
        check("trailing newline kept", path.read_text().endswith("}\n"))
        check("no leftover .tmp", not (path.parent / "ledger.json.tmp").exists())


def test_write_json_overwrite_is_clean():
    print("write_json replaces an existing file wholesale (no merge/append):")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.json"
        atomic_io.write_json(path, {"v": 1})
        atomic_io.write_json(path, {"v": 2})
        check("latest value wins", json.loads(path.read_text()) == {"v": 2})


def test_write_json_mode_applied():
    print("write_json applies an explicit mode before the rename:")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "secret.json"
        atomic_io.write_json(path, {"k": "v"}, mode=0o600)
        check("file is 0600", (path.stat().st_mode & 0o777) == 0o600)


def test_harden_tightens_world_readable():
    print("harden tightens a world-readable file and is a no-op when already tight / missing:")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "floor.json"
        path.write_text("{}")
        os.chmod(path, 0o644)
        atomic_io.harden(path)
        check("0644 -> 0600", (path.stat().st_mode & 0o777) == 0o600)
        atomic_io.harden(path)  # idempotent
        check("stays 0600", (path.stat().st_mode & 0o777) == 0o600)
        missing = Path(d) / "nope.json"
        raised = False
        try:
            atomic_io.harden(missing)
        except Exception:  # noqa: BLE001 — must not raise on a missing file
            raised = True
        check("missing file is a safe no-op", not raised)


def test_locked_smoke():
    print("locked acquires + releases without error and creates the lock file:")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "led.json"
        with atomic_io.locked(path):
            atomic_io.write_json(path, {"ok": True})
        check("wrote under the lock", json.loads(path.read_text()) == {"ok": True})
        check("lock file created alongside", (Path(d) / "led.json.lock").exists())
        # re-acquire after release must not deadlock
        with atomic_io.locked(path):
            pass
        check("re-acquire after release does not block", True)


if __name__ == "__main__":
    print("atomic_io tests\n")
    test_write_json_roundtrip_and_atomic()
    test_write_json_overwrite_is_clean()
    test_write_json_mode_applied()
    test_harden_tightens_world_readable()
    test_locked_smoke()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
