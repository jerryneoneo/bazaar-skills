#!/usr/bin/env python3
"""Tests for journal_reconcile.py — drain crash orphans (Fix A).

    python3 tests/test_journal_reconcile.py

A crashed pass can leave an un-acked intent in thread_outbox: the reply may already be on the
marketplace, but the thread file never recorded it. Reconcile folds each stale intent into the thread
file as an UNCONFIRMED row, advances the cursor past the inbound so the next pass will NOT auto-resend,
enqueues a channel_outbox notify asking the seller to verify it landed, and acks the intent. It NEVER
re-sends to the marketplace. Single-flight run-lock means an un-acked intent older than GRACE_SEC with
no live pass is almost certainly an orphan.

Focus: a stale intent is folded as unconfirmed:true with the cursor advanced + a notify enqueued +
the intent acked; reconcile never re-sends; a fresh (<grace) intent is left untouched; an empty outbox
is a no-op; reconcile never raises on a bad record.
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

import journal_reconcile as jr  # noqa: E402
import lease  # noqa: E402
import thread_outbox as to  # noqa: E402

CLI = [sys.executable, str(ROOT / "bin" / "journal_reconcile.py")]

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _env(data_dir):
    return {**os.environ, "BAZAAR_DATA_DIR": str(data_dir)}


def _run(env=None, extra=None):
    return subprocess.run(CLI + (extra or []), capture_output=True, text=True, env=env)


def _seed_thread(data_dir, sub, thread_id):
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
    path = d / f"{thread_id}.json"
    path.write_text(json.dumps(obj, indent=2))
    return path


def _enqueue_intent(data_dir, *, thread, market, in_msg, text, side, age_sec):
    """Append a thread_outbox intent dated `age_sec` ago (so we control whether it is stale)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    env = _env(data_dir)
    args = [sys.executable, str(ROOT / "bin" / "thread_outbox.py"),
            "enqueue", "--thread", thread, "--market", market, "--in-msg", in_msg,
            "--text", text, "--side", side, "--now", ts]
    out = subprocess.run(args, capture_output=True, text=True, env=env)
    return json.loads(out.stdout)["id"]


def _backdate_intent_ts(data_dir, age_sec):
    """Rewrite EVERY intent's ts in the outbox to `age_sec` ago. The CLI clamps --now to a 300s
    drift window, so to test a far-past intent (older than any grace window) we backdate the jsonl
    line directly — exactly what a long-running in-flight pass / crash orphan looks like on disk."""
    ob = Path(data_dir) / "thread_outbox.jsonl"
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    lines = []
    for line in ob.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        rec["ts"] = ts
        lines.append(json.dumps(rec))
    ob.write_text("\n".join(lines) + "\n")


def test_stale_intent_folded_unconfirmed_cursor_advanced_notify_acked():
    print("a stale (>grace) intent is folded unconfirmed + cursor advanced + notify enqueued + acked:")
    with tempfile.TemporaryDirectory() as d:
        path = _seed_thread(d, "threads", "fb:olaf-1")
        intent_id = _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                                    text="Recovered reply", side="sell", age_sec=120)
        _backdate_intent_ts(d, age_sec=10_000)  # well past GRACE_SEC (no live lease) -> a crash orphan
        out = _run(_env(d))
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("summary reports one reconciled", summary.get("reconciled") == 1)

        obj = json.loads(path.read_text())
        rec = [r for r in obj["transcript"] if r.get("unconfirmed")]
        check("an unconfirmed outbound row was folded in", len(rec) == 1)
        check("unconfirmed row carries the intended text", rec[0]["text"] == "Recovered reply")
        check("unconfirmed row direction is out", rec[0]["dir"] == "out")
        check("cursor advanced past the inbound", obj["cursor"]["last_handled_msg_id"] == "12:20 PM|hi")

        # The intent is acked (drained from the outbox).
        ob = Path(d) / "thread_outbox.jsonl"
        check("intent acked (outbox empty)", to.parse_records(ob.read_text()) == [])

        # A channel_outbox notify was enqueued asking the seller to verify.
        chan = Path(d) / "channel_outbox.jsonl"
        check("channel_outbox notify enqueued", chan.exists())
        notes = [json.loads(line) for line in chan.read_text().splitlines() if line.strip()]
        check("notify is a single record", len(notes) == 1)
        check("notify mentions verifying the recovered reply", "verify" in notes[0]["text"].lower())


def test_reconcile_never_resends():
    print("reconcile NEVER re-sends to the marketplace (no browser/network, just journals):")
    with tempfile.TemporaryDirectory() as d:
        _seed_thread(d, "threads", "fb:olaf-1")
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="x", side="sell", age_sec=120)
        # The module must expose NO send/type/browser entry point — it only journals.
        for forbidden in ("send", "send_reply", "browser", "type_message", "post_to_market"):
            check(f"no {forbidden}() entry point", not hasattr(jr, forbidden))


def test_fresh_intent_left_untouched():
    print("a fresh (<grace) intent is left untouched (the pass may still be running):")
    with tempfile.TemporaryDirectory() as d:
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="too fresh", side="sell", age_sec=2)
        out = _run(_env(d))
        summary = json.loads(out.stdout)
        check("nothing reconciled", summary.get("reconciled") == 0)
        obj = json.loads(path.read_text())
        check("no unconfirmed row folded", not any(r.get("unconfirmed") for r in obj["transcript"]))
        check("cursor unchanged", obj["cursor"]["last_handled_msg_id"] == "old|x")
        ob = Path(d) / "thread_outbox.jsonl"
        check("fresh intent still pending", len(to.parse_records(ob.read_text())) == 1)


def test_empty_outbox_is_noop():
    print("an empty outbox is a clean no-op:")
    with tempfile.TemporaryDirectory() as d:
        out = _run(_env(d))
        check("exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("reconciled 0", summary.get("reconciled") == 0)


def test_never_raises_on_bad_record():
    print("reconcile fail-opens: a garbage outbox line never raises:")
    with tempfile.TemporaryDirectory() as d:
        ob = Path(d) / "thread_outbox.jsonl"
        ob.parent.mkdir(parents=True, exist_ok=True)
        ob.write_text("{not valid json at all\n")
        out = _run(_env(d))
        check("exits 0 (never raised)", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("reconciled 0", summary.get("reconciled") == 0)


def test_buy_side_intent_targets_buyer_threads():
    print("a stale buy-side intent is folded into data/buyer_threads/:")
    with tempfile.TemporaryDirectory() as d:
        path = _seed_thread(d, "buyer_threads", "carousell:9")
        _enqueue_intent(d, thread="carousell:9", market="carousell", in_msg="2pm|hi",
                        text="Recovered buy reply", side="buy", age_sec=120)
        _backdate_intent_ts(d, age_sec=10_000)  # past GRACE_SEC, no live lease -> a crash orphan
        out = _run(_env(d))
        check("exits 0", out.returncode == 0)
        obj = json.loads(path.read_text())
        check("folded into buyer_threads",
              any(r.get("unconfirmed") and r["text"] == "Recovered buy reply"
                  for r in obj["transcript"]))


# ── BUG A1: concurrent supervisor — never steal a live in-flight intent ─────────────────────

def test_grace_sec_exceeds_max_pacing_window():
    print("GRACE_SEC sits comfortably above the max intent->commit window (>=600):"
          " a normal in-flight pacing wait is never mistaken for a crash orphan:")
    # The ceiling on the intent->commit window is the unattended pacing delay: pacing_gate's
    # DEFAULT_DELAY max is 240s (config reply_delay_sec is far smaller) plus send/commit time.
    # GRACE_SEC must be well above that so a still-waiting pass is never folded.
    check("GRACE_SEC >= 600 (>> 240s pacing ceiling)", jr.GRACE_SEC >= 600)


def test_inflight_intent_during_pacing_wait_not_stolen():
    print("an in-flight intent that has aged PAST grace while its worker is still alive (live lease)"
          " is NOT stolen — the exact Olaf race the concurrent supervisor introduces:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        # A normal pass recorded its intent and is still working the fb market (live lease) — e.g.
        # a long pacing wait, or a slow pass that has outlived GRACE_SEC. The lease guard, not the
        # age floor, must protect it so the still-pending reply is not folded before its real send.
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="still waiting out pacing", side="sell", age_sec=120)
        # Aged past GRACE_SEC (600) but within MAX_INTENT_AGE_SEC (1800) — a worker can legitimately
        # still be alive at this age, so the LIVE-lease guard (not the age floor) must protect it.
        _backdate_intent_ts(d, age_sec=700)
        lease.acquire(Path(d), "market:fb", "worker-live", "buyer", lease.AGENT_MARKET_TTL_SEC)
        out = _run(env)  # default grace; the LIVE lease must guard even past grace
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("nothing reconciled (live lease guards the in-flight intent)",
              summary.get("reconciled") == 0)
        check("one skipped (the live-lease intent)", summary.get("skipped") == 1)
        obj = json.loads(path.read_text())
        check("no unconfirmed row folded", not any(r.get("unconfirmed") for r in obj["transcript"]))
        check("cursor NOT advanced", obj["cursor"]["last_handled_msg_id"] == "old|x")
        ob = Path(d) / "thread_outbox.jsonl"
        check("intent left pending (not stolen)", len(to.parse_records(ob.read_text())) == 1)


def test_live_lease_intent_not_folded_even_when_old():
    print("an old intent whose market holds a LIVE lease is NOT folded (a pass is working it):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        # An intent old enough to clear ANY grace window, but its market (fb) has a LIVE lease, and
        # within MAX_INTENT_AGE_SEC (1800) so the live worker can still legitimately be in-flight.
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="still in flight", side="sell", age_sec=120)
        _backdate_intent_ts(d, age_sec=700)  # past any grace window, but the worker is still alive
        lease.acquire(Path(d), "market:fb", "worker-live", "buyer", lease.AGENT_MARKET_TTL_SEC)
        out = _run(env, ["--grace-sec", "1"])  # tiny grace so age alone would otherwise fold it
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("nothing reconciled (live lease guards it)", summary.get("reconciled") == 0)
        check("one skipped (the live-lease intent)", summary.get("skipped") == 1)
        obj = json.loads(path.read_text())
        check("no unconfirmed row folded", not any(r.get("unconfirmed") for r in obj["transcript"]))
        check("cursor NOT advanced", obj["cursor"]["last_handled_msg_id"] == "old|x")
        ob = Path(d) / "thread_outbox.jsonl"
        check("intent left pending (not stolen)", len(to.parse_records(ob.read_text())) == 1)


def test_no_live_lease_old_intent_is_folded():
    print("an old intent whose market has NO live lease IS folded (a true crash orphan):")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="orphaned reply", side="sell", age_sec=120)
        _backdate_intent_ts(d, age_sec=10_000)  # well past GRACE — a real crash orphan
        # No lease held for market:fb -> eventually folded even though the lease check is the
        # primary guard (defense in depth: a crash orphan stays pending forever, GRACE catches it).
        out = _run(env)
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("one reconciled (no live lease)", summary.get("reconciled") == 1)
        obj = json.loads(path.read_text())
        check("unconfirmed row folded", any(r.get("unconfirmed") for r in obj["transcript"]))
        check("cursor advanced", obj["cursor"]["last_handled_msg_id"] == "12:20 PM|hi")


def test_stale_lease_does_not_protect_intent():
    print("a STALE (dead-holder) lease does NOT protect an orphan — it is still folded:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="orphan behind a dead lease", side="sell", age_sec=120)
        _backdate_intent_ts(d, age_sec=10_000)
        # A lease whose holder died long ago (heartbeat far older than its TTL) is stale = not live.
        old = (datetime.now(timezone.utc) - timedelta(seconds=10_000)).isoformat()
        lease.acquire(Path(d), "market:fb", "dead-worker", "buyer", lease.DEFAULT_TTL_SEC)
        # Backdate the heartbeat so the lease is stale.
        lpath = lease._lease_path(Path(d), "market:fb")
        rec = json.loads(lpath.read_text())
        rec["heartbeat_at"] = old
        rec["acquired_at"] = old
        lpath.write_text(json.dumps(rec))
        out = _run(env)
        summary = json.loads(out.stdout)
        check("reconciled (stale lease offers no protection)", summary.get("reconciled") == 1)
        obj = json.loads(path.read_text())
        check("unconfirmed row folded despite the stale lease",
              any(r.get("unconfirmed") for r in obj["transcript"]))


# ── BUG A2: commit/reconcile must not double-journal an already-committed intent ────────────

def test_reconcile_skips_already_committed_intent_no_unconfirmed_dup():
    print("reconcile of an intent whose CONFIRMED outbound already exists folds NO unconfirmed dup"
          " and fires NO false notify — it just drains the racing pending record:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        # Simulate the TOCTOU window: commit has already folded the CONFIRMED outbound row
        # (msg_id "out|<intent_id>") into the thread file, but its ack has not yet drained the
        # pending intent — so reconcile sees the intent still pending.
        intent_id = _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                                    text="Yes, July 5 works!", side="sell", age_sec=120)
        _backdate_intent_ts(d, age_sec=10_000)  # past GRACE, no live lease -> reconcile WILL pick it up
        dpath = Path(d) / "threads"
        dpath.mkdir(parents=True, exist_ok=True)
        (dpath / "fb:olaf-1.json").write_text(json.dumps({
            "thread_id": "fb:olaf-1",
            "cursor": {"last_handled_msg_id": "12:20 PM|hi", "last_handled_ts": "2026-06-28T01:00:00+08:00"},
            "status": "active",
            "transcript": [
                {"msg_id": "12:20 PM|hi", "dir": "in", "text": "hi", "ts": "2026-06-28T01:00:00+08:00"},
                {"msg_id": f"out|{intent_id}", "dir": "out", "text": "Yes, July 5 works!",
                 "ts": "2026-06-28T01:00:01+08:00"},
            ],
            "updated_at": "2026-06-28T01:00:01+08:00",
        }, indent=2))

        out = _run(env)
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("not counted as reconciled (no fold happened)", summary.get("reconciled") == 0)
        check("counted as skipped (already committed)", summary.get("skipped") == 1)

        obj = json.loads((dpath / "fb:olaf-1.json").read_text())
        out_rows = [r for r in obj["transcript"] if r.get("dir") == "out"]
        check("exactly one outbound row (the confirmed one)", len(out_rows) == 1)
        check("the surviving outbound is the CONFIRMED row",
              out_rows[0]["msg_id"] == f"out|{intent_id}")
        check("no unconfirmed dup folded", not any(r.get("unconfirmed") for r in obj["transcript"]))

        # The racing pending intent was drained (acked) so it cannot be re-folded next pass.
        ob = Path(d) / "thread_outbox.jsonl"
        check("racing intent drained", to.parse_records(ob.read_text()) == [])

        # NO false "verify it landed" notify — the reply already committed cleanly.
        chan = Path(d) / "channel_outbox.jsonl"
        notes = ([json.loads(line) for line in chan.read_text().splitlines() if line.strip()]
                 if chan.exists() else [])
        check("no false verify notify enqueued", notes == [])


# ── BUG J1: the live-lease guard's TTL must MATCH the supervisor's lease TTL ─────────────────

def test_live_lease_guard_uses_supervisor_ttl_not_default():
    print("J1: a market lease heartbeated within the SUPERVISOR's 600s TTL (but older than the 120s"
          " lease default) still reads LIVE to the guard, so its in-flight intent is NOT stolen:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="still in flight (slow pacing wait)", side="sell", age_sec=120)
        # Past GRACE_SEC (600) but within MAX_INTENT_AGE_SEC (1800) so only the lease guard can save it.
        _backdate_intent_ts(d, age_sec=700)
        # Acquire AND heartbeat the lease at the supervisor's real 600s TTL, then backdate its last
        # heartbeat to ~200s ago: LIVE under a 600s window, STALE under the 120s lease default. The
        # supervisor would still be heartbeating this worker — reconcile must agree it is live.
        lease.acquire(Path(d), "market:fb", "worker-live", "buyer", lease.AGENT_MARKET_TTL_SEC)
        lpath = lease._lease_path(Path(d), "market:fb")
        rec = json.loads(lpath.read_text())
        beat = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        rec["heartbeat_at"] = beat
        lpath.write_text(json.dumps(rec))
        out = _run(env)
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("nothing reconciled (600s-TTL lease still live at a 200s-old heartbeat)",
              summary.get("reconciled") == 0)
        check("one skipped (live-lease intent guarded)", summary.get("skipped") == 1)
        obj = json.loads(path.read_text())
        check("no unconfirmed row folded", not any(r.get("unconfirmed") for r in obj["transcript"]))
        check("cursor NOT advanced", obj["cursor"]["last_handled_msg_id"] == "old|x")
        ob = Path(d) / "thread_outbox.jsonl"
        check("intent left pending (not stolen)", len(to.parse_records(ob.read_text())) == 1)


def test_shared_lease_ttl_constant_is_canonical_600():
    print("J1: the canonical market-lease TTL lives in lease.py and is 600s (supervisor's window):")
    check("lease.AGENT_MARKET_TTL_SEC exists", hasattr(lease, "AGENT_MARKET_TTL_SEC"))
    check("canonical market-lease TTL is 600s", lease.AGENT_MARKET_TTL_SEC == 600)
    check("it is strictly larger than the generic lease default (the bug's root cause)",
          lease.AGENT_MARKET_TTL_SEC > lease.DEFAULT_TTL_SEC)


# ── BUG J3: a real crash-orphan on a perpetually-busy market is eventually folded ────────────

def test_crash_orphan_folded_past_hard_ceiling_despite_live_lease():
    print("J3: an intent older than the hard age ceiling is folded EVEN under a live lease — past that"
          " age the lease must have churned to a NEW worker, so the intent can't be the same send:")
    with tempfile.TemporaryDirectory() as d:
        env = _env(d)
        path = _seed_thread(d, "threads", "fb:olaf-1")
        _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                        text="crash orphan on a busy market", side="sell", age_sec=120)
        # Age the intent beyond the hard ceiling (a perpetually-busy market always has SOME worker,
        # so the live-lease guard would otherwise skip this real orphan forever, freezing the cursor).
        _backdate_intent_ts(d, age_sec=jr.MAX_INTENT_AGE_SEC + 60)
        # A live lease IS held the whole time (relaunched workers), heartbeated fresh.
        lease.acquire(Path(d), "market:fb", "worker-busy", "buyer", lease.AGENT_MARKET_TTL_SEC)
        out = _run(env)
        check("reconcile exits 0", out.returncode == 0)
        summary = json.loads(out.stdout)
        check("the long-pending orphan is folded despite the live lease",
              summary.get("reconciled") == 1)
        obj = json.loads(path.read_text())
        check("unconfirmed row folded", any(r.get("unconfirmed") for r in obj["transcript"]))
        check("cursor advanced (so the next pass will not re-reply)",
              obj["cursor"]["last_handled_msg_id"] == "12:20 PM|hi")


def test_hard_ceiling_exceeds_max_worker_lifetime():
    print("J3: the hard fold ceiling is comfortably above a single worker's max lifetime, so a live"
          " lease that has NOT churned (one worker still within its wall-clock cap) still guards:")
    check("MAX_INTENT_AGE_SEC exists", hasattr(jr, "MAX_INTENT_AGE_SEC"))
    # The supervisor watchdog kills a worker past MAX_WORKER_SEC=900; beyond that the lease must have
    # churned to a new holder, so an intent older than the ceiling cannot be the same in-flight send.
    check("hard ceiling exceeds the supervisor's 900s worker watchdog cap with margin",
          jr.MAX_INTENT_AGE_SEC > 900)


# ── BUG J4: two concurrent reconciles must not double-count or double-notify one orphan ───────

def test_second_reconcile_of_healed_intent_skips_no_double_notify():
    print("J4: a second reconcile of an intent that was already folded+acked returns skipped and does"
          " NOT fire a duplicate verify-notify (the ack already drained it):")
    with tempfile.TemporaryDirectory() as d:
        path = _seed_thread(d, "threads", "fb:olaf-1")
        intent_id = _enqueue_intent(d, thread="fb:olaf-1", market="fb", in_msg="12:20 PM|hi",
                                    text="Recovered reply", side="sell", age_sec=120)
        _backdate_intent_ts(d, age_sec=10_000)  # past GRACE, no live lease -> a real orphan
        # First reconcile heals it: folds the unconfirmed row, notifies, acks.
        first = _run(_env(d))
        check("first reconcile folds the orphan", json.loads(first.stdout).get("reconciled") == 1)

        # Now simulate a SECOND, concurrent reconcile that captured the SAME intent record before the
        # first one's ack landed — call _reconcile_one directly with the stale (already-acked) intent.
        intent = {"id": intent_id, "thread_id": "fb:olaf-1", "in_msg_id": "12:20 PM|hi",
                  "text": "Recovered reply", "side": "sell", "market": "fb"}
        env_overlay = {"BAZAAR_DATA_DIR": str(d)}
        old_env = {k: os.environ.get(k) for k in env_overlay}
        os.environ.update(env_overlay)
        try:
            outcome = jr._reconcile_one(intent)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        check("second reconcile of the already-healed intent returns skipped", outcome == "skipped")

        # Exactly ONE outbound row (no duplicate fold) and exactly ONE notify (no duplicate nudge).
        obj = json.loads(path.read_text())
        out_rows = [r for r in obj["transcript"] if r.get("dir") == "out"]
        check("still exactly one outbound row (no duplicate fold)", len(out_rows) == 1)
        chan = Path(d) / "channel_outbox.jsonl"
        notes = ([json.loads(line) for line in chan.read_text().splitlines() if line.strip()]
                 if chan.exists() else [])
        check("exactly one verify notify (no duplicate nudge)", len(notes) == 1)


if __name__ == "__main__":
    print("journal_reconcile tests\n")
    test_stale_intent_folded_unconfirmed_cursor_advanced_notify_acked()
    test_reconcile_never_resends()
    test_fresh_intent_left_untouched()
    test_empty_outbox_is_noop()
    test_never_raises_on_bad_record()
    test_buy_side_intent_targets_buyer_threads()
    test_grace_sec_exceeds_max_pacing_window()
    test_inflight_intent_during_pacing_wait_not_stolen()
    test_live_lease_intent_not_folded_even_when_old()
    test_no_live_lease_old_intent_is_folded()
    test_stale_lease_does_not_protect_intent()
    test_reconcile_skips_already_committed_intent_no_unconfirmed_dup()
    test_live_lease_guard_uses_supervisor_ttl_not_default()
    test_shared_lease_ttl_constant_is_canonical_600()
    test_crash_orphan_folded_past_hard_ceiling_despite_live_lease()
    test_hard_ceiling_exceeds_max_worker_lifetime()
    test_second_reconcile_of_healed_intent_skips_no_double_notify()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
