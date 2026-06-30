#!/usr/bin/env python3
"""Tests for journal_send.py — the deterministic send-bracketing helper (Fix A).

    python3 tests/test_journal_send.py

The Olaf split-brain: a reply lands on the marketplace, then the pass crashes BEFORE journaling, so
the thread file keeps a stale cursor and never records the outbound. journal_send brackets the send:
`intent` writes the intended outbound to thread_outbox BEFORE the browser send; `commit` folds the
inbound + outbound into the thread file ATOMICALLY (and acks the intent) AFTER send() returns.

Focus: intent writes a pending record + prints the id; commit appends inbound (deduped by in_msg_id)
+ outbound (out|<iso-ts>), advances the cursor, sets status, acks the intent; idempotent (a second
commit = no dup, no double-advance); immutable (the read dict is never mutated in place); fail-open
to a skeleton thread when the file is missing; --side buy vs sell targets the right directory.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import journal_send as js  # noqa: E402

CLI = [sys.executable, str(ROOT / "bin" / "journal_send.py")]

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _env(data_dir):
    return {**os.environ, "SELLY_DATA_DIR": str(data_dir)}


def _run(args, env=None):
    return subprocess.run(CLI + args, capture_output=True, text=True, env=env)


def _seed_thread(data_dir, sub, thread_id, **overrides):
    """Write a minimal thread file under data/<sub>/<thread_id>.json and return its path."""
    d = Path(data_dir) / sub
    d.mkdir(parents=True, exist_ok=True)
    obj = {
        "thread_id": thread_id,
        "item_id": "kettle",
        "buyer_handle": "Olaf",
        "cursor": {"last_handled_msg_id": "old|x", "last_handled_ts": "2026-06-28T00:00:00+08:00"},
        "status": "active",
        "transcript": [{"msg_id": "old|x", "dir": "in", "text": "old", "ts": "2026-06-28T00:00:00+08:00"}],
        "updated_at": "2026-06-28T00:00:00+08:00",
    }
    obj.update(overrides)
    path = d / f"{thread_id}.json"
    path.write_text(json.dumps(obj, indent=2))
    return path


def _intent(env, *, thread, market, in_msg, text, side="sell"):
    out = _run(["intent", "--thread", thread, "--market", market, "--in-msg", in_msg,
                "--text", text, "--side", side], env=env)
    return out, json.loads(out.stdout)["id"]


# ── intent ────────────────────────────────────────────────────────────────────────────────

def test_intent_writes_pending_record_and_prints_id():
    print("intent writes a pending thread_outbox record + prints the id:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        out, intent_id = _intent(env, thread="fb:olaf-1", market="fb",
                                 in_msg="12:20 PM|hello", text="Hi Olaf")
        check("intent exits 0", out.returncode == 0)
        check("prints an id", bool(intent_id))
        import thread_outbox as to
        ob = Path(d) / "thread_outbox.jsonl"
        recs = to.parse_records(ob.read_text())
        check("one pending intent recorded", len(recs) == 1)
        check("text carried on the intent", recs[0]["text"] == "Hi Olaf")
        check("status pending", recs[0]["status"] == "pending")


def test_intent_rejects_empty_text():
    print("intent validates non-empty text (exit 2):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        out = _run(["intent", "--thread", "fb:t", "--market", "fb", "--in-msg", "i",
                    "--text", "", "--side", "sell"], env=env)
        check("empty text exits 2", out.returncode == 2)


def test_intent_dedups_same_inbound():
    print("two intents for the SAME (thread, in_msg) reuse one pending record (no stranded copy):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _, id1 = _intent(env, thread="fb:vida", market="fb",
                         in_msg="1:50 PM|defects?", text="no defects")
        _, id2 = _intent(env, thread="fb:vida", market="fb",
                         in_msg="1:50 PM|defects?", text="no defects, all brand new")
        check("same intent id returned (deduped)", id1 == id2)
        import thread_outbox as to
        recs = to.parse_records((Path(d) / "thread_outbox.jsonl").read_text())
        check("exactly one pending record", len(recs) == 1)
        check("text refreshed to the latest", recs[0]["text"] == "no defects, all brand new")


def test_mark_sent_cli_flips_status():
    print("journal_send mark-sent flips the intent to sent_unverified:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _, intent_id = _intent(env, thread="fb:olaf-1", market="fb", in_msg="i", text="Hi")
        out = _run(["mark-sent", "--intent", intent_id], env=env)
        check("mark-sent exits 0", out.returncode == 0)
        check("reports marked", json.loads(out.stdout).get("marked") is True)
        import thread_outbox as to
        recs = to.parse_records((Path(d) / "thread_outbox.jsonl").read_text())
        check("status now sent_unverified", recs[0]["status"] == "sent_unverified")
        check("sent_ts stamped", bool(recs[0].get("sent_ts")))


def test_commit_after_mark_sent_still_folds():
    print("the full happy path intent -> mark-sent -> commit still folds + acks (commit finds the"
          " sent_unverified intent):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _, intent_id = _intent(env, thread="fb:olaf-1", market="fb",
                               in_msg="12:20 PM|hi", text="Hello!")
        _run(["mark-sent", "--intent", intent_id], env=env)  # send fired
        commit = _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id, "--side", "sell"], env=env)
        check("commit exits 0 after mark-sent", commit.returncode == 0)
        check("commit reports committed", json.loads(commit.stdout).get("committed") is True)
        obj = json.loads(path.read_text())
        out_rows = [r for r in obj["transcript"] if r["dir"] == "out"]
        check("outbound folded", any(r["text"] == "Hello!" for r in out_rows))
        check("cursor advanced", obj["cursor"]["last_handled_msg_id"] == "12:20 PM|hi")
        import thread_outbox as to
        check("intent acked (outbox empty)",
              to.parse_records((Path(d) / "thread_outbox.jsonl").read_text()) == [])


# ── commit ──────────────────────────────────────────────────────────────────────────────

def test_commit_folds_inbound_outbound_advances_cursor_acks():
    print("commit appends inbound + outbound, advances cursor, sets status, acks the intent:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        env2 = _env(d)
        out, intent_id = _intent(env2, thread="fb:olaf-1", market="fb",
                                 in_msg="12:20 PM|July 5 ok?", text="Yes, July 5 works!")
        commit = _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id,
                       "--status", "agreed", "--side", "sell"], env=env)
        check("commit exits 0", commit.returncode == 0)
        obj = json.loads(path.read_text())
        msg_ids = [r["msg_id"] for r in obj["transcript"]]
        check("inbound row appended", "12:20 PM|July 5 ok?" in msg_ids)
        out_rows = [r for r in obj["transcript"] if r["dir"] == "out"]
        check("outbound row appended", any(r["text"] == "Yes, July 5 works!" for r in out_rows))
        check("outbound msg_id is out|<iso-ts>", out_rows[-1]["msg_id"].startswith("out|"))
        check("cursor advanced to the inbound", obj["cursor"]["last_handled_msg_id"] == "12:20 PM|July 5 ok?")
        check("status set", obj["status"] == "agreed")
        check("updated_at refreshed", obj["updated_at"] != "2026-06-28T00:00:00+08:00")
        import thread_outbox as to
        ob = Path(d) / "thread_outbox.jsonl"
        check("intent acked (outbox empty)", to.parse_records(ob.read_text()) == [])


def test_commit_idempotent_no_dup_no_double_advance():
    print("commit is idempotent: a second commit of the same outbound = no dup, no double-advance:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _, intent_id = _intent(env, thread="fb:olaf-1", market="fb",
                               in_msg="12:20 PM|hi", text="Hello!")
        first = _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id, "--side", "sell"], env=env)
        after_first = json.loads(path.read_text())
        n_first = len(after_first["transcript"])
        # Re-run the SAME commit (the intent is already acked / gone). Must be a clean no-op.
        second = _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id, "--side", "sell"], env=env)
        after_second = json.loads(path.read_text())
        check("first commit ok", first.returncode == 0)
        check("second commit does not error", second.returncode == 0)
        check("no duplicate rows added", len(after_second["transcript"]) == n_first)
        check("inbound present exactly once",
              [r["msg_id"] for r in after_second["transcript"]].count("12:20 PM|hi") == 1)


def test_outbound_msg_id_is_deterministic_from_intent():
    print("the outbound msg_id is derived from the intent id (stable across retries, not wall-clock):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _, intent_id = _intent(env, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi", text="Hello!")
        _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id, "--side", "sell"], env=env)
        obj = json.loads(path.read_text())
        out_rows = [r for r in obj["transcript"] if r["dir"] == "out"]
        check("exactly one outbound row", len(out_rows) == 1)
        check("outbound msg_id keyed by intent id", out_rows[0]["msg_id"] == f"out|{intent_id}")


def test_commit_dedups_inbound_already_present():
    print("commit dedups the inbound row by in_msg_id (skip if already present):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        # Inbound already in the transcript (e.g. a prior crashed pass folded it).
        path = _seed_thread(d, "threads", "fb:olaf-1", transcript=[
            {"msg_id": "old|x", "dir": "in", "text": "old", "ts": "2026-06-28T00:00:00+08:00"},
            {"msg_id": "12:20 PM|hi", "dir": "in", "text": "hi", "ts": "2026-06-28T01:00:00+08:00"},
        ])
        _, intent_id = _intent(env, thread="fb:olaf-1", market="fb",
                               in_msg="12:20 PM|hi", text="Hello!")
        _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id, "--side", "sell"], env=env)
        obj = json.loads(path.read_text())
        check("inbound not duplicated",
              [r["msg_id"] for r in obj["transcript"]].count("12:20 PM|hi") == 1)
        check("outbound still appended",
              any(r["dir"] == "out" and r["text"] == "Hello!" for r in obj["transcript"]))


def test_commit_immutable_input_dict_unchanged():
    print("commit is immutable (builds a new dict; the on-disk read is never mutated in place):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        original = path.read_text()
        # Exercise the in-process pure builder directly so we can assert the input is untouched.
        src = json.loads(original)
        new_obj = js.fold_commit(
            src,
            in_msg_id="12:20 PM|hi",
            in_text="hi",
            out_text="Hello!",
            status="agreed",
            now_iso="2026-06-28T10:00:00+08:00",
        )
        check("returns a different object", new_obj is not src)
        check("input transcript length unchanged (no in-place append)",
              len(src["transcript"]) == 1)
        check("input cursor unchanged", src["cursor"]["last_handled_msg_id"] == "old|x")
        check("new object advanced the cursor", new_obj["cursor"]["last_handled_msg_id"] == "12:20 PM|hi")


def test_commit_fail_open_skeleton_when_thread_missing():
    print("commit fail-opens to a skeleton thread structure when the file is missing:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        # No thread file seeded.
        _, intent_id = _intent(env, thread="fb:ghost-1", market="fb",
                               in_msg="12:20 PM|hi", text="Hello!")
        commit = _run(["commit", "--thread", "fb:ghost-1", "--intent", intent_id, "--side", "sell"], env=env)
        check("commit exits 0 even with no thread file", commit.returncode == 0)
        path = Path(d) / "threads" / "fb:ghost-1.json"
        check("thread file created", path.exists())
        obj = json.loads(path.read_text())
        check("thread_id set on the skeleton", obj.get("thread_id") == "fb:ghost-1")
        check("outbound folded in",
              any(r["dir"] == "out" and r["text"] == "Hello!" for r in obj["transcript"]))


def test_side_buy_vs_sell_targets_right_dir():
    print("--side buy targets data/buyer_threads/, --side sell targets data/threads/:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _seed_thread(d, "buyer_threads", "carousell:9")
        _, intent_id = _intent(env, thread="carousell:9", market="carousell",
                               in_msg="2pm|deal?", text="Could you do $50?", side="buy")
        commit = _run(["commit", "--thread", "carousell:9", "--intent", intent_id, "--side", "buy"], env=env)
        check("buy commit exits 0", commit.returncode == 0)
        buy_path = Path(d) / "buyer_threads" / "carousell:9.json"
        sell_path = Path(d) / "threads" / "carousell:9.json"
        check("wrote to buyer_threads", buy_path.exists())
        check("did NOT write to threads", not sell_path.exists())


def test_commit_side_optional_derived_from_intent():
    print("commit no longer REQUIRES --side: it derives the side from the intent record:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _seed_thread(d, "buyer_threads", "carousell:9")
        # Enqueue a BUY intent, then commit with NO --side at all.
        _, intent_id = _intent(env, thread="carousell:9", market="carousell",
                               in_msg="2pm|deal?", text="Could you do $50?", side="buy")
        commit = _run(["commit", "--thread", "carousell:9", "--intent", intent_id], env=env)
        check("commit with no --side exits 0", commit.returncode == 0)
        buy_path = Path(d) / "buyer_threads" / "carousell:9.json"
        sell_path = Path(d) / "threads" / "carousell:9.json"
        check("derived side buy -> wrote to buyer_threads", buy_path.exists())
        check("did NOT mis-file to threads (sell)", not sell_path.exists())


def test_invalid_side_value_exits_2():
    print("a commit with a malformed --side value still exits 2 (bad args):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        out = _run(["commit", "--thread", "fb:t", "--intent", "abc", "--side", "bogus"], env=env)
        check("malformed --side exits 2", out.returncode == 2)


# ── BUG A3: commit trusts the intent's recorded side, not the CLI --side ─────────────────────

def test_buy_intent_commit_without_side_files_to_buyer_threads():
    print("a buy intent committed with NO --side is journaled to data/buyer_threads/ (from the"
          " intent's side) — never silently mis-filed into the sell tree:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _seed_thread(d, "buyer_threads", "carousell:9")
        _, intent_id = _intent(env, thread="carousell:9", market="carousell",
                               in_msg="2pm|deal?", text="Could you do $50?", side="buy")
        commit = _run(["commit", "--thread", "carousell:9", "--intent", intent_id], env=env)
        check("commit exits 0", commit.returncode == 0)
        buy_path = Path(d) / "buyer_threads" / "carousell:9.json"
        sell_path = Path(d) / "threads" / "carousell:9.json"
        check("buyer reply landed in buyer_threads", buy_path.exists())
        check("nothing written to the sell tree", not sell_path.exists())
        obj = json.loads(buy_path.read_text())
        check("the buy reply was folded", any(r.get("text") == "Could you do $50?"
                                              for r in obj["transcript"]))
        check("the BUYER thread cursor advanced (not a sell thread)",
              obj["cursor"]["last_handled_msg_id"] == "2pm|deal?")


def test_commit_side_mismatch_errors_not_misfiles():
    print("a buy intent committed with --side sell ERRORS (exit 3) rather than mis-filing to sell:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _seed_thread(d, "buyer_threads", "carousell:9")
        _, intent_id = _intent(env, thread="carousell:9", market="carousell",
                               in_msg="2pm|deal?", text="Could you do $50?", side="buy")
        commit = _run(["commit", "--thread", "carousell:9", "--intent", intent_id, "--side", "sell"],
                      env=env)
        check("contradictory --side exits non-zero", commit.returncode == 3)
        sell_path = Path(d) / "threads" / "carousell:9.json"
        check("did NOT write the buy reply into the sell tree", not sell_path.exists())
        # The intent is left intact (not acked) so a corrected commit can still journal it.
        import thread_outbox as to
        ob = Path(d) / "thread_outbox.jsonl"
        check("intent left pending after the rejected commit",
              len(to.parse_records(ob.read_text())) == 1)


def test_commit_matching_side_still_works():
    print("a matching --side (buy on a buy intent) commits normally:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        _seed_thread(d, "buyer_threads", "carousell:9")
        _, intent_id = _intent(env, thread="carousell:9", market="carousell",
                               in_msg="2pm|deal?", text="Could you do $50?", side="buy")
        commit = _run(["commit", "--thread", "carousell:9", "--intent", intent_id, "--side", "buy"],
                      env=env)
        check("matching --side commits ok", commit.returncode == 0)
        check("wrote to buyer_threads", (Path(d) / "buyer_threads" / "carousell:9.json").exists())


# ── BUG A2: commit acks the intent inside the thread lock (no double-journal) ────────────────

def test_commit_acks_intent_under_the_thread_lock():
    print("commit acks the intent INSIDE the thread-file lock (fold + ack are atomic vs reconcile):")
    # A reconcile that grabs the thread lock between commit's fold-write and its ack would see the
    # intent still pending and fold an unconfirmed duplicate. Guard: the ack must happen before the
    # lock is released. We assert it by patching atomic_io.locked so that, WHILE the lock is held,
    # the intent is already gone from the outbox.
    import importlib
    import atomic_io
    import thread_outbox as to
    js_mod = importlib.import_module("journal_send")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        os.environ["SELLY_DATA_DIR"] = d
        try:
            _seed_thread(d, "threads", "fb:olaf-1")
            # Enqueue an intent in-process so the same process state is observable under the lock.
            from datetime import datetime, timezone
            rec = to.enqueue("fb:olaf-1", "fb", "Hello!", "12:20 PM|hi",
                             datetime.now(timezone.utc), side="sell")
            intent_id = rec["id"]

            observed = {"pending_while_locked": None}
            real_locked = atomic_io.locked

            from contextlib import contextmanager

            @contextmanager
            def spy_locked(path):
                with real_locked(path):
                    yield
                    # Lock still held here — by now the ack MUST have run if it is inside the lock.
                    observed["pending_while_locked"] = to.peek(thread_id="fb:olaf-1")["count"]

            atomic_io.locked = spy_locked
            try:
                js_mod.run_commit("fb:olaf-1", intent_id, "sell", None, None)
            finally:
                atomic_io.locked = real_locked

            check("intent already acked before the lock was released",
                  observed["pending_while_locked"] == 0)
        finally:
            os.environ.pop("SELLY_DATA_DIR", None)


def test_commit_then_reconcile_yields_one_outbound_no_unconfirmed():
    print("a commit immediately followed by a reconcile of the same intent = ONE outbound row,"
          " no unconfirmed dup, no false notify:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _, intent_id = _intent(env, thread="fb:olaf-1", market="fb",
                               in_msg="12:20 PM|hi", text="Yes!")
        commit = _run(["commit", "--thread", "fb:olaf-1", "--intent", intent_id, "--side", "sell"],
                      env=env)
        check("commit exits 0", commit.returncode == 0)
        # Now run reconcile (the daemon does this before every pass). The intent is acked, so there
        # is nothing to fold; even if a stale pending leftover existed, the confirmed-row guard skips.
        rec = subprocess.run([sys.executable, str(ROOT / "bin" / "journal_reconcile.py")],
                             capture_output=True, text=True, env=env)
        check("reconcile exits 0", rec.returncode == 0)
        obj = json.loads(path.read_text())
        out_rows = [r for r in obj["transcript"] if r.get("dir") == "out"]
        check("exactly one outbound row", len(out_rows) == 1)
        check("the outbound is the confirmed row", out_rows[0]["msg_id"] == f"out|{intent_id}")
        check("no unconfirmed dup", not any(r.get("unconfirmed") for r in obj["transcript"]))
        chan = Path(d) / "channel_outbox.jsonl"
        notes = ([json.loads(line) for line in chan.read_text().splitlines() if line.strip()]
                 if chan.exists() else [])
        check("no false verify notify", notes == [])


if __name__ == "__main__":
    print("journal_send tests\n")
    test_intent_writes_pending_record_and_prints_id()
    test_intent_rejects_empty_text()
    test_intent_dedups_same_inbound()
    test_mark_sent_cli_flips_status()
    test_commit_after_mark_sent_still_folds()
    test_commit_folds_inbound_outbound_advances_cursor_acks()
    test_commit_idempotent_no_dup_no_double_advance()
    test_outbound_msg_id_is_deterministic_from_intent()
    test_commit_dedups_inbound_already_present()
    test_commit_immutable_input_dict_unchanged()
    test_commit_fail_open_skeleton_when_thread_missing()
    test_side_buy_vs_sell_targets_right_dir()
    test_commit_side_optional_derived_from_intent()
    test_invalid_side_value_exits_2()
    test_buy_intent_commit_without_side_files_to_buyer_threads()
    test_commit_side_mismatch_errors_not_misfiles()
    test_commit_matching_side_still_works()
    test_commit_acks_intent_under_the_thread_lock()
    test_commit_then_reconcile_yields_one_outbound_no_unconfirmed()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
