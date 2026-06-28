#!/usr/bin/env python3
"""Tests for thread_outbox — the per-thread intent log (data/thread_outbox.jsonl).

    python3 tests/test_thread_outbox.py

Mirrors channel_outbox's discipline (flock sidecar + atomic rewrite + tolerant parse) but keyed by
thread: one record per INTENDED outbound, written BEFORE the browser send and acked AFTER it lands.
Focus: enqueue appends a complete pending record keyed by thread_id; peek filters by thread_id and
age; ack/fail flip status; a corrupt line is skipped (fail-open); concurrent enqueues never corrupt
the file.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import thread_outbox as to  # noqa: E402

NOW_UTC = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
CLI = [sys.executable, str(ROOT / "bin" / "thread_outbox.py")]

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _run(args, env=None):
    return subprocess.run(CLI + args, capture_output=True, text=True, env=env)


def _env(data_dir):
    return {**os.environ, "BAZAAR_DATA_DIR": str(data_dir)}


# ── pure helpers ────────────────────────────────────────────────────────────────────────

def test_build_record_shape_and_immutable():
    print("build_record shape + immutability:")
    rec = to.build_record("fb:olaf-1", "fb", "Hi there", "12:20 PM|hello", NOW_UTC.isoformat())
    check("status pending", rec["status"] == "pending")
    check("thread_id preserved", rec["thread_id"] == "fb:olaf-1")
    check("market preserved", rec["market"] == "fb")
    check("text preserved", rec["text"] == "Hi there")
    check("in_msg_id preserved", rec["in_msg_id"] == "12:20 PM|hello")
    check("attempts starts at 0", rec["attempts"] == 0)
    check("has an id", bool(rec["id"]))
    check("ts is the supplied now", rec["ts"] == NOW_UTC.isoformat())
    check("two records get distinct ids",
          to.build_record("t", "fb", "a", "i", NOW_UTC.isoformat())["id"]
          != to.build_record("t", "fb", "b", "i", NOW_UTC.isoformat())["id"])


def test_parse_records_tolerant():
    print("parse_records tolerance + FIFO order:")
    text = "\n".join([
        json.dumps({"id": "a", "thread_id": "t1", "text": "1"}),
        "   ",                                       # blank -> skipped
        "{not json",                                 # torn -> skipped
        json.dumps({"thread_id": "t2", "text": "no id"}),  # missing id -> dropped
        json.dumps({"id": "b", "thread_id": "t2", "text": "2"}),
    ])
    records = to.parse_records(text)
    check("keeps only the two well-formed records", len(records) == 2)
    check("preserves append order (FIFO)", [r["id"] for r in records] == ["a", "b"])
    check("empty text -> empty list", to.parse_records("") == [])


# ── CLI (isolated data dir via BAZAAR_DATA_DIR) ───────────────────────────────────────────

def test_enqueue_appends_pending_keyed_by_thread():
    print("enqueue appends a pending record with all fields keyed by thread_id:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        out = _run(["enqueue", "--thread", "fb:olaf-1", "--market", "fb",
                    "--in-msg", "12:20 PM|hi", "--text", "Hi Olaf"], env=env)
        check("enqueue exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("enqueue returns an id", bool(payload.get("id")))
        snap = json.loads(_run(["peek"], env=env).stdout)
        check("one pending", snap["count"] == 1)
        rec = snap["pending"][0]
        check("thread_id keyed", rec["thread_id"] == "fb:olaf-1")
        check("market captured", rec["market"] == "fb")
        check("in_msg_id captured", rec["in_msg_id"] == "12:20 PM|hi")
        check("text captured", rec["text"] == "Hi Olaf")
        check("status pending", rec["status"] == "pending")


def test_peek_filters_by_thread_id():
    print("peek filters by thread_id:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _run(["enqueue", "--thread", "fb:a", "--market", "fb", "--in-msg", "i1", "--text", "x"], env=env)
        _run(["enqueue", "--thread", "fb:b", "--market", "fb", "--in-msg", "i2", "--text", "y"], env=env)
        snap = json.loads(_run(["peek", "--thread", "fb:a"], env=env).stdout)
        check("only the matching thread returned", snap["count"] == 1)
        check("right thread", snap["pending"][0]["thread_id"] == "fb:a")
        allsnap = json.loads(_run(["peek"], env=env).stdout)
        check("no filter returns both", allsnap["count"] == 2)


def test_peek_filters_by_age():
    print("peek filters by older_than_sec:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        old_iso = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        fresh_iso = datetime.now(timezone.utc).isoformat()
        _run(["enqueue", "--thread", "fb:old", "--market", "fb", "--in-msg", "i", "--text", "x",
              "--now", old_iso], env=env)
        _run(["enqueue", "--thread", "fb:new", "--market", "fb", "--in-msg", "i", "--text", "y",
              "--now", fresh_iso], env=env)
        snap = json.loads(_run(["peek", "--older-than-sec", "60"], env=env).stdout)
        check("only the aged record returned", snap["count"] == 1)
        check("the aged one is the old thread", snap["pending"][0]["thread_id"] == "fb:old")


def test_ack_and_fail_mutate_status():
    print("ack removes the record; fail increments attempts:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        rid = json.loads(_run(["enqueue", "--thread", "fb:t", "--market", "fb",
                               "--in-msg", "i", "--text", "x"], env=env).stdout)["id"]
        fail = json.loads(_run(["fail", "--id", rid], env=env).stdout)
        check("fail reports failed", fail.get("failed") is True)
        check("attempts incremented", fail.get("attempts") == 1)
        still = json.loads(_run(["peek"], env=env).stdout)
        check("still pending after a fail", still["count"] == 1)
        check("attempts persisted", still["pending"][0]["attempts"] == 1)
        ack = json.loads(_run(["ack", "--id", rid], env=env).stdout)
        check("ack reports acked", ack.get("acked") is True)
        check("empty after ack", json.loads(_run(["peek"], env=env).stdout)["count"] == 0)
        again = json.loads(_run(["ack", "--id", "nope"], env=env).stdout)
        check("ack of unknown id is a no-op", again.get("acked") is False)


def test_corrupt_line_skipped_fail_open():
    print("a corrupt line is skipped (tolerant parse, fail-open):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        rid = json.loads(_run(["enqueue", "--thread", "fb:t", "--market", "fb",
                               "--in-msg", "i", "--text", "good"], env=env).stdout)["id"]
        path = Path(d) / "thread_outbox.jsonl"
        with path.open("a") as f:
            f.write("{this is not valid json\n")
        snap = json.loads(_run(["peek"], env=env).stdout)
        check("garbage line skipped, good record survives", snap["count"] == 1)
        check("the good record is intact", snap["pending"][0]["id"] == rid)


def test_two_enqueues_dont_corrupt():
    print("two enqueues don't corrupt the file (append-only, both readable):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        first = json.loads(_run(["enqueue", "--thread", "fb:a", "--market", "fb",
                                 "--in-msg", "i", "--text", "first"], env=env).stdout)
        before = (Path(d) / "thread_outbox.jsonl").read_text()
        second = json.loads(_run(["enqueue", "--thread", "fb:b", "--market", "fb",
                                  "--in-msg", "i", "--text", "second"], env=env).stdout)
        after = (Path(d) / "thread_outbox.jsonl").read_text()
        check("file grows by exactly one line",
              len(after.splitlines()) == len(before.splitlines()) + 1)
        check("the original first line is byte-for-byte preserved", after.startswith(before))
        check("distinct ids", first["id"] != second["id"])
        snap = json.loads(_run(["peek"], env=env).stdout)
        check("both readable in append order",
              [r["text"] for r in snap["pending"]] == ["first", "second"])


def test_bad_input_rejected():
    print("input validation:")
    bad = [
        ["enqueue", "--market", "fb", "--in-msg", "i", "--text", "x"],     # missing --thread
        ["enqueue", "--thread", "t", "--in-msg", "i", "--text", "x"],      # missing --market
        ["enqueue", "--thread", "t", "--market", "fb", "--text", "x"],     # missing --in-msg
        ["enqueue", "--thread", "t", "--market", "fb", "--in-msg", "i", "--text", ""],  # empty text
        ["ack"],                                                            # missing --id
        ["bogus"],                                                          # unknown command
    ]
    ok = True
    for args in bad:
        proc = _run(args)
        if proc.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed input exits nonzero", ok)


if __name__ == "__main__":
    print("thread_outbox tests\n")
    test_build_record_shape_and_immutable()
    test_parse_records_tolerant()
    test_enqueue_appends_pending_keyed_by_thread()
    test_peek_filters_by_thread_id()
    test_peek_filters_by_age()
    test_ack_and_fail_mutate_status()
    test_corrupt_line_skipped_fail_open()
    test_two_enqueues_dont_corrupt()
    test_bad_input_rejected()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
