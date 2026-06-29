#!/usr/bin/env python3
"""Tests for channel_control.py — the deterministic paused-state drain.

    python3 tests/test_channel_control.py

Focus: the pure classification (process_events) applies the right control side-effects and acks for
/pause, /resume, and free-text corrections, and infer_target routes a correction at a known thread/
want/item when the text names it. State is isolated via BAZAAR_DATA_DIR.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import channel_control as cc  # noqa: E402
import control  # noqa: E402

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _ev(kind, text):
    return {"event_id": 1, "kind": kind, "text": text, "payload": {}, "ts": 1}


def test_pause_resume_commands():
    print("/pause and /resume drive the control flag:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        acks = cc.process_events([_ev("command", "/pause")])
        check("paused after /pause", control.is_paused() is True)
        check("pause ack returned", len(acks) == 1 and acks[0].startswith("⏸"))
        acks = cc.process_events([_ev("command", "/resume")])
        check("not paused after /resume", control.is_paused() is False)
        check("resume ack returned", acks[0].startswith("▶️"))


def test_freetext_becomes_correction():
    print("free text while paused → queued correction + ack:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        control.pause(source="telegram")
        acks = cc.process_events([_ev("text", "list it at 80 not 60")])
        pend = control.pending_corrections()
        check("one correction queued", len(pend) == 1)
        check("text captured", pend[0]["text"] == "list it at 80 not 60")
        check("ack echoes the note", "📝" in acks[0] and "80 not 60" in acks[0])


def test_photo_correction():
    print("photo while paused is captured (caption or [photo]):")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        control.pause(source="telegram")
        cc.process_events([_ev("photo", "")])
        check("photo placeholder captured", control.pending_corrections()[0]["text"] == "[photo]")


def test_target_inference():
    print("infer_target routes a correction at a known thread/want when named:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        (Path(tmp) / "buyer_threads").mkdir()
        (Path(tmp) / "buyer_threads" / "carousell:2145160641.json").write_text("{}")
        (Path(tmp) / "wants").mkdir()
        (Path(tmp) / "wants" / "iphone-5s-black.json").write_text("{}")
        t1 = cc.infer_target("stop replying on carousell:2145160641 please")
        check("thread ref matched", t1 == {"scope": "thread", "ref": "carousell:2145160641"})
        t2 = cc.infer_target("raise the budget on iphone-5s-black")
        check("want id matched", t2 == {"scope": "want", "ref": "iphone-5s-black"})
        check("no false match on unrelated text", cc.infer_target("just a normal note") is None)


def test_target_attached_to_correction():
    print("a named correction carries its target through process_events:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        (Path(tmp) / "threads").mkdir()
        (Path(tmp) / "threads" / "fb:998877.json").write_text("{}")
        control.pause(source="telegram")
        cc.process_events([_ev("text", "stop replying to fb:998877")])
        tgt = control.pending_corrections()[0]["target"]
        check("correction targeted at the thread", tgt == {"scope": "thread", "ref": "fb:998877"})


def test_batch_order_pause_then_correct():
    print("a mixed batch [/pause, note, /resume] is fully accounted for:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        acks = cc.process_events([
            _ev("command", "/pause"),
            _ev("text", "use 80 dollars"),
            _ev("command", "/resume"),
        ])
        check("three acks (pause, note, resume)", len(acks) == 3)
        check("correction queued for the resume pass", len(control.pending_corrections()) == 1)
        check("ended not paused", control.is_paused() is False)


def test_single_pause_ack_no_duplicate():
    print("dedup: two /pause in one batch yield EXACTLY ONE ack (the 7x 'holding here' spam bug):")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        acks = cc.process_events([_ev("command", "/pause"), _ev("command", "/pause")])
        check("paused after the batch", control.is_paused() is True)
        check("exactly ONE pause ack for two /pause", sum(a.startswith("⏸") for a in acks) == 1)
        # a later /pause in a separate batch (still paused, already acked) adds nothing
        acks2 = cc.process_events([_ev("command", "/pause")])
        check("re-pause while paused adds no ack", acks2 == [])


def test_resume_ack_reflects_pending_corrections():
    print("honest resume ack: 'applying now' only when corrections are queued, else 'back to work':")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        # paused with a queued correction → resume promises to apply it
        cc.process_events([_ev("command", "/pause"), _ev("text", "list kettle at 9 not 8")])
        acks = cc.process_events([_ev("command", "/resume")])
        check("resume WITH pending → 'applying your corrections'", "applying your corrections" in acks[0])
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        # paused with nothing queued → resume just confirms, no false promise
        cc.process_events([_ev("command", "/pause")])
        acks = cc.process_events([_ev("command", "/resume")])
        check("resume with NONE pending → ACK_RESUME_CLEAN", acks[0] == cc.ACK_RESUME_CLEAN)
        check("clean resume ack still starts with ▶️", acks[0].startswith("▶️"))


def test_corrections_pass_due():
    print("corrections_pass_due: force an apply pass only when not-paused + pending + rate-limit met:")
    check("not paused, 1 pending, 200s → due", cc.corrections_pass_due(False, 1, 200) is True)
    check("paused → never due", cc.corrections_pass_due(True, 1, 200) is False)
    check("0 pending → not due", cc.corrections_pass_due(False, 0, 200) is False)
    check("pending but too soon (10s) → not due (no hot-loop)", cc.corrections_pass_due(False, 1, 10) is False)


def test_drain_catchall_acks_external_pause_once():
    print("drain catch-all: a flag flipped OUTSIDE a /pause event (LLM pass / loop fast-path) still"
          " gets exactly ONE ack, immune to the self-kill race:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BAZAAR_DATA_DIR"] = tmp
        control.pause(source="telegram")          # set by something other than a /pause event
        sent = []
        orig_poll, orig_send = cc._poll_events, cc._send
        cc._poll_events = lambda env: []          # no events in this batch
        cc._send = lambda text, env: sent.append(text)
        try:
            cc.drain(env={})
            check("catch-all sends exactly one ⏸ ack", sum(s.startswith("⏸") for s in sent) == 1)
            sent.clear()
            cc.drain(env={})                       # a second drain must NOT re-ack
            check("second drain sends no duplicate ack", sent == [])
        finally:
            cc._poll_events, cc._send = orig_poll, orig_send


if __name__ == "__main__":
    print("channel_control.py tests\n")
    test_pause_resume_commands()
    test_freetext_becomes_correction()
    test_photo_correction()
    test_target_inference()
    test_target_attached_to_correction()
    test_batch_order_pause_then_correct()
    test_single_pause_ack_no_duplicate()
    test_resume_ack_reflects_pending_corrections()
    test_corrections_pass_due()
    test_drain_catchall_acks_external_pause_once()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
