#!/usr/bin/env python3
"""Tests for control.py — the runtime pause/correct flag.

Runnable with plain python (no pytest needed):

    python3 tests/test_control.py

Focus: the invariants that make pause trustworthy — a missing/garbage file reads as NOT paused
(never strands the agent), pause() stamps `since` only on the false->true edge (idempotent),
resume() preserves the corrections queue, mark_applied is exactly-once, and writes are atomic.
State is isolated per test via SELLY_DATA_DIR (the same seam pacing_gate uses).
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
    os.environ["SELLY_DATA_DIR"] = tmp


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
    print("CLI pause/status/is-paused/resume (isolated via SELLY_DATA_DIR):")
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SELLY_DATA_DIR": tmp}
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


def test_mark_applied_cli():
    print("mark-applied CLI exists (the corrections recipe shells to it; it was missing before):")
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SELLY_DATA_DIR": tmp}
        base = [sys.executable, str(ROOT / "bin" / "control.py")]
        co = subprocess.run(base + ["correct", "--text", "list kettle at 9 not 8", "--source", "telegram"],
                            capture_output=True, text=True, env=env)
        cid = json.loads(co.stdout)["id"]
        ma = subprocess.run(base + ["mark-applied", cid], capture_output=True, text=True, env=env)
        check("mark-applied exits 0", ma.returncode == 0)
        check("mark-applied reports the id applied", json.loads(ma.stdout)["applied"] == [cid])
        check("no pending corrections left", json.loads(ma.stdout)["pending_corrections"] == 0)
        st = subprocess.run(base + ["status"], capture_output=True, text=True, env=env)
        rec = [c for c in json.loads(st.stdout)["corrections"] if c["id"] == cid][0]
        check("correction marked applied in state", rec["applied"] is True)


def test_ack_sent_one_shot_claim():
    print("ack_sent one-shot: claim_pause_ack succeeds EXACTLY once per pause episode; resume re-arms:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        check("not paused -> claim returns False", control.claim_pause_ack() is False)
        control.pause(source="telegram")
        check("ack_sent armed (False) on the false->true edge", control.state()["ack_sent"] is False)
        check("first claim succeeds", control.claim_pause_ack() is True)
        check("ack_sent durably stamped True", control.state()["ack_sent"] is True)
        check("second claim returns False (no duplicate ack)", control.claim_pause_ack() is False)
        # re-pause of an already-paused agent preserves ack_sent (never re-acks mid-episode)
        control.pause(source="telegram")
        check("re-pause preserves ack_sent", control.state()["ack_sent"] is True)
        check("claim after re-pause still False", control.claim_pause_ack() is False)
        # resume re-arms the one-shot for the NEXT episode
        control.resume(source="cli")
        check("resume clears ack_sent", control.state()["ack_sent"] is False)
        control.pause(source="telegram")
        check("new pause episode re-arms the claim", control.claim_pause_ack() is True)


def test_resume_stays_pure_no_marketplace_logic():
    print("resume() is a DUMB pause record: it flips only pause fields and never touches catch-up/"
          "marketplace state (the catchup orphan heal lives in the daemon, not here):")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        # A stale catch-up sweep sits beside the control file; resume() must not read or rewrite it.
        catchup = Path(tmp) / "catchup_session.json"
        catchup.write_text('{"active": true, "phase": "sweep"}')
        control.pause(source="telegram")
        control.add_correction("relist at 90", source="telegram")
        new_state = control.resume(source="cli")
        check("only pause fields flip (paused/since/ack_sent)",
              new_state["paused"] is False and new_state["since"] is None
              and new_state["ack_sent"] is False)
        check("corrections preserved (resume pass drains them)",
              len(new_state["corrections"]) == 1)
        check("no catch-up/marketplace key leaks into control state",
              not any(k in new_state for k in ("catchup", "active", "phase", "markets_pending")))
        check("the catch-up session file is left untouched by resume()",
              catchup.read_text() == '{"active": true, "phase": "sweep"}')


def test_claim_pause_ack_tolerant_on_garbage():
    print("ack_sent is cosmetic-only: a garbage file reads NOT paused and claim returns False"
          " (can never strand the agent):")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        (Path(tmp) / "control.json").write_text("{not valid json")
        check("garbage -> not paused", control.is_paused() is False)
        check("garbage -> claim returns False", control.claim_pause_ack() is False)


if __name__ == "__main__":
    print("control.py tests\n")
    test_default_when_absent()
    test_pause_resume_roundtrip()
    test_pause_idempotent_since_edge()
    test_resume_preserves_corrections()
    test_resume_stays_pure_no_marketplace_logic()
    test_add_correction_unique_ids_and_target()
    test_mark_applied_idempotent()
    test_tolerant_on_garbage()
    test_ack_sent_one_shot_claim()
    test_claim_pause_ack_tolerant_on_garbage()
    test_cli_roundtrip()
    test_mark_applied_cli()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
