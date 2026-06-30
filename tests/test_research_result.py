#!/usr/bin/env python3
"""Tests for bin/research_result.py — the background research worker's ONLY write path.

    python3 tests/test_research_result.py

The worker is sandboxed to this one command, so its contract matters: validate input, stamp the
batch id, write atomically, reject garbage. Writes go under data/research_results/ with a unique
test batch id and are cleaned up.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "research_results"
_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def run(batch, result):
    return subprocess.run([sys.executable, str(ROOT / "bin" / "research_result.py"),
                           "--batch", batch, "--result", result],
                          capture_output=True, text=True)


def test_writes_and_stamps():
    print("research_result: writes a validated result and stamps the batch id:")
    batch = "batch-test-rr-001"
    dest = RESULTS / f"{batch}.json"
    try:
        out = run(batch, json.dumps({"title": "Vtg Lamp", "comp_low": 10, "comp_med": 15,
                                     "comp_high": 22, "currency": "SGD"}))
        check("exit 0", out.returncode == 0)
        check("result file written", dest.exists())
        rec = json.loads(dest.read_text())
        check("fields preserved", rec.get("title") == "Vtg Lamp" and rec.get("comp_med") == 15)
        check("batch id stamped", rec.get("batch_id") == batch)
    finally:
        dest.unlink(missing_ok=True)
        try:
            RESULTS.rmdir()  # only succeeds if empty (no live results) — harmless otherwise
        except OSError:
            pass


def test_rejects_bad_input():
    print("research_result: rejects non-JSON and non-object results (exit 2, no write):")
    out = run("batch-test-rr-002", "not json")
    check("bad json → exit 2", out.returncode == 2)
    out2 = run("batch-test-rr-003", "[1,2,3]")
    check("array (non-object) → exit 2", out2.returncode == 2)
    check("no file for rejected input", not (RESULTS / "batch-test-rr-002.json").exists())
    out3 = run("../escape", json.dumps({"x": 1}))
    check("unsafe batch id → exit 2", out3.returncode == 2)


if __name__ == "__main__":
    print("research_result.py tests\n")
    test_writes_and_stamps()
    test_rejects_bad_input()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
