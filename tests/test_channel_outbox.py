#!/usr/bin/env python3
"""Tests for channel_outbox — the single-writer control-channel outbox.

Runnable with plain python (no pytest needed):

    python3 tests/test_channel_outbox.py

Focus: the ONE invariant that makes the queue safe — when many workers enqueue at once,
every notice lands as exactly one valid JSONL line with a unique id, none lost or
interleaved. Plus FIFO order from peek, ack removing only its target, immutability of the
pure helpers, and boundary input validation.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import channel_outbox as co  # noqa: E402

NOW_UTC = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
CLI = [sys.executable, str(ROOT / "bin" / "channel_outbox.py")]

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
    rec = co.build_record("notify", "hello", NOW_UTC.isoformat(), ref="thread-1", source="sell-run")
    check("status is pending", rec["status"] == "pending")
    check("kind preserved", rec["kind"] == "notify")
    check("text preserved", rec["text"] == "hello")
    check("ref preserved", rec["ref"] == "thread-1")
    check("source preserved", rec["source"] == "sell-run")
    check("has an id", bool(rec["id"]))
    check("ts is the supplied now", rec["ts"] == NOW_UTC.isoformat())
    empty = co.build_record("say", "hi", NOW_UTC.isoformat())
    check("absent ref normalizes to None", empty["ref"] is None)
    check("absent source normalizes to None", empty["source"] is None)
    check("two records get distinct ids",
          co.build_record("say", "a", NOW_UTC.isoformat())["id"]
          != co.build_record("say", "b", NOW_UTC.isoformat())["id"])


def test_new_id_unique():
    print("new_id uniqueness:")
    ids = {co.new_id() for _ in range(1000)}
    check("1000 ids are all unique", len(ids) == 1000)


def test_parse_records_tolerant_and_fifo():
    print("parse_records tolerance + FIFO order:")
    text = "\n".join([
        json.dumps({"id": "a", "ts": "t1", "kind": "notify", "text": "1"}),
        "   ",                                  # blank -> skipped
        "{not json",                            # torn line -> skipped
        json.dumps({"kind": "notify", "text": "no id"}),  # missing id -> dropped
        json.dumps({"id": "b", "ts": "t2", "kind": "say", "text": "2"}),
    ])
    records = co.parse_records(text)
    check("keeps only the two well-formed records", len(records) == 2)
    check("preserves append order (FIFO)", [r["id"] for r in records] == ["a", "b"])
    check("empty text -> empty list", co.parse_records("") == [])


def test_select_pending_limit_and_immutable():
    print("select_pending limit + immutability:")
    records = [{"id": str(i)} for i in range(5)]
    first_two = co.select_pending(records, 2)
    check("limit returns first N in order", [r["id"] for r in first_two] == ["0", "1"])
    check("limit<=0 returns all", len(co.select_pending(records, 0)) == 5)
    # mutating the returned copies must not touch the source
    first_two[0]["id"] = "MUTATED"
    check("returned records are copies (source untouched)", records[0]["id"] == "0")


def test_remove_id_immutable_and_targeted():
    print("remove_id targeted + immutability:")
    records = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    kept, removed = co.remove_id(records, "b")
    check("reports removal", removed is True)
    check("removes only the target", [r["id"] for r in kept] == ["a", "c"])
    check("does NOT mutate the input list", [r["id"] for r in records] == ["a", "b", "c"])
    _, missing = co.remove_id(records, "zzz")
    check("absent id reports not-removed", missing is False)


def test_serialize_roundtrip():
    print("serialize round-trips through parse:")
    records = [co.build_record("notify", "x", NOW_UTC.isoformat()),
               co.build_record("say", "y", NOW_UTC.isoformat())]
    text = co.serialize(records)
    check("ends with a trailing newline", text.endswith("\n"))
    check("round-trips back to the same ids",
          [r["id"] for r in co.parse_records(text)] == [r["id"] for r in records])
    check("empty list serializes to empty string", co.serialize([]) == "")


# ── CLI (isolated data dir via BAZAAR_DATA_DIR) ───────────────────────────────────────────

def test_cli_enqueue_peek_ack_flow():
    print("CLI enqueue -> peek -> ack flow:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        out = _run(["enqueue", "--kind", "notify", "--text", "buyer asked about shipping",
                    "--ref", "thread-9", "--source", "sell-run"], env=env)
        check("enqueue exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("enqueue reports enqueued", payload.get("enqueued") is True)
        rec_id = payload.get("id")
        check("enqueue returns an id", bool(rec_id))

        peek = _run(["peek"], env=env)
        check("peek exits 0", peek.returncode == 0)
        snap = json.loads(peek.stdout)
        check("peek shows one pending", snap["count"] == 1)
        rec = snap["pending"][0]
        check("pending record carries the text", rec["text"] == "buyer asked about shipping")
        check("pending record status pending", rec["status"] == "pending")
        check("pending record ref threaded", rec["ref"] == "thread-9")

        # peek must NOT remove
        peek2 = _run(["peek"], env=env)
        check("peek is read-only (still one pending)", json.loads(peek2.stdout)["count"] == 1)

        ack = _run(["ack", "--id", rec_id], env=env)
        check("ack exits 0", ack.returncode == 0)
        check("ack reports acked", json.loads(ack.stdout)["acked"] is True)
        check("outbox empty after ack", json.loads(_run(["peek"], env=env).stdout)["count"] == 0)


def test_cli_peek_fifo_order():
    print("FIFO: peek returns records in append order:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        for i in range(5):
            _run(["enqueue", "--kind", "notify", "--text", f"msg-{i}"], env=env)
        snap = json.loads(_run(["peek"], env=env).stdout)
        check("five pending", snap["count"] == 5)
        check("texts in FIFO append order",
              [r["text"] for r in snap["pending"]] == [f"msg-{i}" for i in range(5)])
        limited = json.loads(_run(["peek", "--limit", "3"], env=env).stdout)
        check("limit returns first 3 in order",
              [r["text"] for r in limited["pending"]] == ["msg-0", "msg-1", "msg-2"])
        check("limit count matches", limited["count"] == 3)


def test_cli_ack_removes_only_target():
    print("ack removes only the targeted id:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        ids = []
        for i in range(3):
            out = _run(["enqueue", "--kind", "say", "--text", f"t-{i}"], env=env)
            ids.append(json.loads(out.stdout)["id"])
        ack = _run(["ack", "--id", ids[1]], env=env)
        check("ack reports acked", json.loads(ack.stdout)["acked"] is True)
        snap = json.loads(_run(["peek"], env=env).stdout)
        remaining = [r["id"] for r in snap["pending"]]
        check("two remain", len(remaining) == 2)
        check("targeted id gone", ids[1] not in remaining)
        check("others survive, order preserved", remaining == [ids[0], ids[2]])
        # acking an unknown id is a no-op (idempotent ack)
        again = _run(["ack", "--id", "does-not-exist"], env=env)
        check("ack of unknown id reports not-acked", json.loads(again.stdout)["acked"] is False)
        check("no-op ack leaves the two intact",
              len(json.loads(_run(["peek"], env=env).stdout)["pending"]) == 2)


def test_concurrent_enqueue_no_loss_no_interleave():
    print("INVARIANT: concurrent enqueues all land, ids unique, lines valid JSON:")
    n_workers = 10
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        base = CLI + ["enqueue", "--kind", "notify"]
        # Launch all at once; flock must serialize each append so no line tears/interleaves.
        procs = [subprocess.Popen(base + ["--text", f"worker-{i}", "--source", f"w{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env)
                 for i in range(n_workers)]
        returned_ids = []
        for p in procs:
            out, _ = p.communicate()
            check_payload = json.loads(out)
            returned_ids.append(check_payload["id"])
        check("every enqueue returned an id", len([i for i in returned_ids if i]) == n_workers)
        check("returned ids unique", len(set(returned_ids)) == n_workers)

        # The persisted file: exactly n_workers lines, each valid JSON, ids unique.
        raw = (Path(d) / "channel_outbox.jsonl").read_text()
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        check(f"exactly {n_workers} lines persisted (got {len(lines)})", len(lines) == n_workers)
        parsed_ok = True
        file_ids = []
        texts = set()
        for ln in lines:
            try:
                obj = json.loads(ln)
            except ValueError:
                parsed_ok = False
                continue
            file_ids.append(obj.get("id"))
            texts.add(obj.get("text"))
        check("every persisted line is valid JSON (no torn/interleaved writes)", parsed_ok)
        check("persisted ids all unique", len(set(file_ids)) == n_workers)
        check("all worker texts present (none lost)",
              texts == {f"worker-{i}" for i in range(n_workers)})

        # And peek agrees the count is exactly n_workers.
        snap = json.loads(_run(["peek"], env=env).stdout)
        check("peek sees all enqueued", snap["count"] == n_workers)


def test_enqueue_immutable_atomic_append():
    print("enqueue is immutable + atomic (append never rewrites existing lines):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        first = json.loads(_run(["enqueue", "--kind", "notify", "--text", "first"], env=env).stdout)
        before = (Path(d) / "channel_outbox.jsonl").read_text()
        second = json.loads(_run(["enqueue", "--kind", "say", "--text", "second"], env=env).stdout)
        after = (Path(d) / "channel_outbox.jsonl").read_text()
        check("file grows by exactly one line", len(after.splitlines()) == len(before.splitlines()) + 1)
        check("the original first line is byte-for-byte preserved", after.startswith(before))
        check("first and second have distinct ids", first["id"] != second["id"])
        # no stray temp/lock garbage left as outbox content
        snap = json.loads(_run(["peek"], env=env).stdout)
        check("both records readable in order",
              [r["text"] for r in snap["pending"]] == ["first", "second"])


def test_now_clamp_rejects_time_travel():
    print("hardening: --now far from wall clock is rejected:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        future = _run(["enqueue", "--kind", "notify", "--text", "x",
                       "--now", "2099-01-01T00:00:00Z"], env=env)
        check("future --now exits nonzero", future.returncode != 0)
        past = _run(["enqueue", "--kind", "notify", "--text", "x",
                     "--now", "2000-01-01T00:00:00Z"], env=env)
        check("distant-past --now exits nonzero", past.returncode != 0)


def test_fail_deadletters_after_max():
    print("fail bounds retries → dead-letter (no infinite retry of a poison notice):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        rec_id = json.loads(_run(["enqueue", "--kind", "notify", "--text", "poison"], env).stdout)["id"]
        for i in range(co.MAX_SEND_ATTEMPTS - 1):
            out = json.loads(_run(["fail", "--id", rec_id], env).stdout)
            check(f"attempt {i+1}: not yet dead-lettered", out["failed"] and not out["dead_lettered"])
        still = json.loads(_run(["peek"], env).stdout)
        check("still in the live queue before the cap", still["count"] == 1)
        final = json.loads(_run(["fail", "--id", rec_id], env).stdout)
        check("final failure dead-letters it", final["dead_lettered"] is True)
        drained = json.loads(_run(["peek"], env).stdout)
        check("live queue empty after dead-letter", drained["count"] == 0)
        dl = Path(d) / "channel_outbox.deadletter.jsonl"
        check("dead-letter file holds the poison record", dl.exists() and rec_id in dl.read_text())
        check("fail requires --id (exit 2)", _run(["fail"], env).returncode == 2)


def test_bad_input_rejected():
    print("input validation:")
    bad = [
        ["enqueue", "--text", "no kind"],                      # missing --kind
        ["enqueue", "--kind", "bogus", "--text", "x"],         # invalid kind
        ["enqueue", "--kind", "notify", "--text", ""],         # empty text
        ["enqueue", "--kind", "notify"],                        # no text at all
        ["ack"],                                                # missing --id
        ["ack", "--id", ""],                                    # empty --id
        ["bogus"],                                              # unknown command
    ]
    ok = True
    for args in bad:
        proc = _run(args)
        if proc.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed input exits nonzero", ok)


if __name__ == "__main__":
    print("channel_outbox tests\n")
    test_build_record_shape_and_immutable()
    test_new_id_unique()
    test_parse_records_tolerant_and_fifo()
    test_select_pending_limit_and_immutable()
    test_remove_id_immutable_and_targeted()
    test_serialize_roundtrip()
    test_cli_enqueue_peek_ack_flow()
    test_cli_peek_fifo_order()
    test_cli_ack_removes_only_target()
    test_concurrent_enqueue_no_loss_no_interleave()
    test_enqueue_immutable_atomic_append()
    test_now_clamp_rejects_time_travel()
    test_fail_deadletters_after_max()
    test_bad_input_rejected()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
