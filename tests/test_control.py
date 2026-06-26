#!/usr/bin/env python3
"""Tests for control.py — the runtime pause/correct flag.

Runnable with plain python (no pytest needed):

    python3 tests/test_control.py

Focus: the invariants that make pause trustworthy — a missing/garbage file reads as NOT paused
(never strands the agent), pause() stamps `since` only on the false->true edge (idempotent),
resume() preserves the corrections queue, mark_applied is exactly-once, and writes are atomic.
State is isolated per test via BAZAAR_DATA_DIR (the same seam pacing_gate uses).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import control  # noqa: E402

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _isolate(tmp):
    """Point control.py at a scratch data dir for the duration of a test."""
    os.environ["BAZAAR_DATA_DIR"] = tmp


def test_default_when_absent():
    print("default state when the file is absent:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        check("is_paused() is False with no file", control.is_paused() is False)
        check("state has empty corrections", control.state()["corrections"] == [])
        check("no file created by a pure read", not (Path(tmp) / "control.json").exists())


def test_pause_resume_roundtrip():
    print("pause -> resume round-trip:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        control.pause(source="telegram", reason="wrong price")
        check("paused after pause()", control.is_paused() is True)
        check("source recorded", control.state()["source"] == "telegram")
        check("since stamped", bool(control.state()["since"]))
        control.resume(source="cli")
        check("not paused after resume()", control.is_paused() is False)
        check("since cleared on resume", control.state()["since"] is None)


def test_pause_idempotent_since_edge():
    print("pause() stamps `since` only on the false->true edge:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        first = control.pause(source="telegram")["since"]
        second = control.pause(source="cli", reason="again")["since"]
        check("since unchanged on re-pause", first == second and first is not None)
        check("source/reason still update on re-pause",
              control.state()["source"] == "cli" and control.state()["reason"] == "again")


def test_resume_preserves_corrections():
    print("resume() leaves the corrections queue for the resume pass:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        control.pause(source="telegram")
        control.add_correction("list it at 80 not 60", source="telegram")
        control.resume(source="telegram")
        check("correction survives resume", len(control.state()["corrections"]) == 1)
        check("pending_corrections sees it", len(control.pending_corrections()) == 1)


def test_add_correction_unique_ids_and_target():
    print("add_correction: unique ids, works paused or running, target preserved:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        a = control.add_correction("note a", source="cli")  # running (not paused)
        control.pause(source="telegram")
        b = control.add_correction("note b", source="telegram",
                                   target={"scope": "thread", "ref": "carousell:123"})
        check("two corrections queued", len(control.state()["corrections"]) == 2)
        check("ids are unique", a["id"] != b["id"])
        check("target preserved", control.pending_corrections()[1]["target"]["ref"] == "carousell:123")
        try:
            control.add_correction("   ", source="cli")
            check("blank correction rejected", False)
        except ValueError:
            check("blank correction rejected", True)


def test_mark_applied_idempotent():
    print("mark_applied flips applied + is exactly-once:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rec = control.add_correction("set price to 80", source="telegram")
        control.mark_applied([rec["id"]])
        applied = [c for c in control.state()["corrections"] if c["id"] == rec["id"]][0]
        check("marked applied", applied["applied"] is True and applied["applied_ts"])
        check("no longer pending", control.pending_corrections() == [])
        # Re-applying is a no-op (the applied_ts does not change / no duplicate)
        before = control.state()["corrections"]
        control.mark_applied([rec["id"]])
        check("re-mark is a no-op", control.state()["corrections"] == before)


def test_tolerant_on_garbage():
    print("garbage file reads as NOT paused (never stranded):")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        (Path(tmp) / "control.json").write_text("{not valid json")
        check("garbage -> not paused", control.is_paused() is False)
        check("garbage -> default corrections", control.state()["corrections"] == [])


def test_cli_roundtrip():
    print("CLI pause/status/is-paused/resume (isolated via BAZAAR_DATA_DIR):")
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "BAZAAR_DATA_DIR": tmp}
        base = [sys.executable, str(ROOT / "bin" / "control.py")]
        pa = subprocess.run(base + ["pause", "--source", "claude-code"],
                            capture_output=True, text=True, env=env)
        check("pause exits 0", pa.returncode == 0)
        check("pause reports paused", json.loads(pa.stdout)["paused"] is True)
        ip = subprocess.run(base + ["is-paused"], capture_output=True, text=True, env=env)
        check("is-paused exits 0 when paused", ip.returncode == 0)
        st = subprocess.run(base + ["status"], capture_output=True, text=True, env=env)
        check("status shows source", json.loads(st.stdout)["source"] == "claude-code")
        co = subprocess.run(base + ["correct", "--text", "stop replying to that buyer",
                                    "--source", "telegram", "--scope", "thread", "--ref", "fb:9"],
                            capture_output=True, text=True, env=env)
        check("correct exits 0", co.returncode == 0 and json.loads(co.stdout)["ok"] is True)
        re = subprocess.run(base + ["resume", "--source", "telegram"],
                            capture_output=True, text=True, env=env)
        check("resume reports 1 pending correction", json.loads(re.stdout)["pending_corrections"] == 1)
        ip2 = subprocess.run(base + ["is-paused"], capture_output=True, text=True, env=env)
        check("is-paused exits 1 when not paused", ip2.returncode == 1)


if __name__ == "__main__":
    print("control.py tests\n")
    test_default_when_absent()
    test_pause_resume_roundtrip()
    test_pause_idempotent_since_edge()
    test_resume_preserves_corrections()
    test_add_correction_unique_ids_and_target()
    test_mark_applied_idempotent()
    test_tolerant_on_garbage()
    test_cli_roundtrip()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
