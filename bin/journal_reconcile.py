#!/usr/bin/env python3
"""journal_reconcile.py — heal crash orphans left by an interrupted send (Fix A).

A pass can crash AFTER a reply lands on the marketplace but BEFORE bin/journal_send.py commit folds
it into the thread file (the Olaf "Reached max turns" split-brain). That leaves a PENDING intent in
data/thread_outbox.jsonl with no matching outbound in the thread file.

This drains those orphans. Folding an intent that is actually STILL IN FLIGHT would re-create the Olaf
bug (cursor advanced + the row marked unconfirmed BEFORE the real send, so a later crash drops the
reply). Two guards keep that from happening even under the concurrent supervisor (FB ∥ Carousell
workers), where a normal pass holds a pending intent for its FULL pacing delay before sending:

  • LIVE-LEASE GUARD (primary): skip any intent whose market currently holds a live (non-stale)
    lease — a worker is actively driving that market, so the intent is in-flight, not orphaned.
  • GRACE_SEC (defense in depth): the age floor below which an intent is never folded, set well
    ABOVE the maximum intent->commit window so a normal pacing wait is never mistaken for a crash.
    A true crash orphan stays pending forever, so GRACE still eventually catches it even if the
    lease check is unavailable.

For each intent that clears BOTH guards:

  1. Under atomic_io.locked(<threadfile>), fold the intended outbound into the thread file as an
     UNCONFIRMED row {"dir":"out","unconfirmed":true,"text":...,"ts":...,"note":"recovered after
     crash; verify it landed"}.
  2. Advance the cursor past in_msg_id so the NEXT pass will not blindly auto-resend, and return this
     thread_id in the run's `needs_verify` list so the buyer pass actively re-checks the live chat and
     re-sends ONLY if the recovered reply is genuinely missing (Fix 3 — closes the silent-drop hole
     when the interruption landed BEFORE the send).
  3. Enqueue a bin/channel_outbox.py notify telling the seller the reply may not have completed.
  4. Ack the intent (drain it from the outbox).

It NEVER re-sends to the marketplace — it only journals + asks the human to verify. Fail-open
everywhere: a reconcile error must never break the caller (the daemon / the BUYER prompt's first
step), so a bad record is skipped and the run still exits 0 with a JSON summary.

CLI:  python3 journal_reconcile.py [--grace-sec N]   (default GRACE_SEC=600)
Output (stdout, JSON):  {"reconciled": <n>, "skipped": <n>, "errors": <n>, "needs_verify": [<thread_id>...]}
(`needs_verify` lists the threads the next buyer pass must re-check in-chat and resend if missing.)
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402
import journal_send  # noqa: E402  reuse the path resolver + the skeleton + the dedup builder
import lease  # noqa: E402  the per-market lease — a live one means a worker is mid-pass on that market
import thread_outbox  # noqa: E402

# GRACE_SEC: the age floor below which an intent is never folded. Derivation of the >=600 ceiling:
# the longest possible intent->commit window is the unattended pacing delay the reply pipeline waits
# out AFTER recording the intent but BEFORE the send — max(reply_delay_sec) (config, currently 60s)
# vs pacing_gate.DEFAULT_DELAY max of 240s — whichever applies, plus send + commit time. 600s sits
# comfortably above that, so a normal in-flight pacing wait is NEVER mistaken for a crash orphan,
# while a true crash orphan (pending forever) is still folded once it ages past the floor.
GRACE_SEC = 600
# The folded row's note + the channel notify deliberately avoid the word "crash": the usual cause is a
# routine turn-cap continuation or a watchdog restart, NOT a crash, and the interruption can land
# BEFORE the browser send (so the reply may never have gone out) — "interrupted / may not have
# completed" is honest for all of those. The silent-drop guard (Fix 3) is carried by the `needs_verify`
# thread_ids this run returns, which the buyer pass acts on immediately (see reconcile()'s docstring).
RECOVERED_NOTE = "recovered after an interrupted pass; may not have completed — verify it sent in-chat"

# MAX_INTENT_AGE_SEC: the hard age ceiling past which an intent is folded even if its market still
# holds a LIVE lease. The live-lease guard alone would skip a real crash orphan FOREVER on a market
# that always has SOME worker (each relaunch holds the lease for its lifetime), freezing the cursor
# so a later pass on the same thread re-replies. This ceiling must exceed the longest a SINGLE worker
# can legitimately hold a market lease: the supervisor watchdog kills a worker past MAX_WORKER_SEC
# (=900s, bin/supervisor.py) and a stuck holder's lease goes stale within AGENT_MARKET_TTL_SEC (600s)
# and is reclaimed by a NEW holder. So past this ceiling the lease must have churned to a different
# worker, meaning the pending intent cannot still be that same in-flight send — safe to fold. 1800s
# (=2x the 900s worker cap, with margin) sits comfortably above both.
MAX_INTENT_AGE_SEC = 1800


def data_dir() -> Path:
    return thread_outbox.data_dir()


def _market_has_live_lease(market: str | None) -> bool:
    """True if `market` currently holds a live (non-stale) lease — a worker is actively driving it,
    so any pending intent for that market is in-flight (mid-pacing-wait), NOT a crash orphan.

    The liveness window MUST equal the supervisor's lease TTL (lease.AGENT_MARKET_TTL_SEC=600), the
    same value the supervisor acquires + heartbeats every `market:<id>` lease with. Reading liveness
    with lease's generic 120s default instead would mark a worker whose heartbeat is 120-600s old as
    NOT live here while it is still live to the supervisor — reconcile would then fold that worker's
    still-in-flight intent before its real send (the Olaf in-flight-steal). Pass the explicit TTL so
    the two windows can never silently drift.

    Fail-open: if the lease can't be read, return False so GRACE_SEC remains the backstop."""
    if not market:
        return False
    try:
        status = lease.status(data_dir(), f"market:{market}", ttl=lease.AGENT_MARKET_TTL_SEC)
        return bool(status.get("held"))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _intent_age_sec(intent: dict, now: datetime) -> float:
    """Seconds since the intent was recorded. A missing/unparseable ts is treated as infinitely old
    (it cannot be timed, so a crash orphan must not hide behind a bad timestamp) — mirrors
    thread_outbox.select_pending's fail-closed age handling."""
    ts = thread_outbox.parse_iso(intent.get("ts"))
    if ts is None:
        return float("inf")
    return (now - ts).total_seconds()


def _already_committed(thread: dict, intent_id: str) -> bool:
    """True if a CONFIRMED outbound for this intent is already in the transcript.

    journal_send.commit writes the confirmed outbound with msg_id "out|<intent_id>". If a reconcile
    races commit (sees the intent still pending because ack hasn't landed yet) it must recognize that
    confirmed row and SKIP folding — otherwise it appends a bogus UNCONFIRMED duplicate for an intent
    that already committed cleanly. Belt-and-suspenders alongside acking inside the commit lock."""
    out_id = f"out|{intent_id}"
    return any(
        isinstance(r, dict) and r.get("dir") == "out" and r.get("msg_id") == out_id
        for r in (thread.get("transcript") or [])
    )


def _fold_unconfirmed(thread: dict, *, intent_id: str, in_msg_id: str, out_text: str,
                      now_iso: str) -> dict:
    """Return a NEW thread dict with an UNCONFIRMED outbound row folded in and the cursor advanced
    past the inbound (so the next pass will not auto-resend). IMMUTABLE (deep-copies the input so no
    nested object is shared) + IDEMPOTENT: the recovered row is keyed by `intent_id` (not text, so two
    distinct orphans with identical reply text are both folded), and appended only if a row for that
    intent is not already present."""
    new_thread = copy.deepcopy(thread)
    transcript = list(new_thread.get("transcript") or [])
    row = {"dir": "out", "unconfirmed": True, "text": out_text, "ts": now_iso,
           "note": RECOVERED_NOTE, "intent_id": intent_id}
    already = any(
        isinstance(r, dict) and r.get("unconfirmed") and r.get("intent_id") == intent_id
        for r in transcript
    )
    if not already:
        transcript.append(row)
    new_thread["transcript"] = transcript
    if in_msg_id:
        new_thread["cursor"] = {"last_handled_msg_id": in_msg_id, "last_handled_ts": now_iso}
    # The cursor IS advanced (idempotency: a reply that actually landed must not be auto-resent). The
    # silent-drop guard (Fix 3) for the case where the reply NEVER sent is NOT a persistent thread
    # status — it is the `needs_verify` thread_id this run returns in its summary. The buyer pass runs
    # this reconcile as its FIRST STEP, then immediately re-reads the live chat for each needs_verify
    # thread and resends only if the recovered reply is genuinely missing. That signal is naturally
    # one-shot (the intent is acked/drained here, so a later reconcile never re-emits it), so there is
    # no lingering status to clear; the channel notify below is the human-visible backstop.
    new_thread["updated_at"] = now_iso
    return new_thread


def _notify_verify(thread_id: str) -> None:
    """Enqueue a control-channel notify asking the seller to verify the recovered reply landed.
    Fail-open: a notify failure must not abort the reconcile of this (or any other) orphan."""
    text = (f"A reply on {thread_id} was interrupted before it finished sending, so it may not have "
            f"gone through. I'll re-check that chat on the next pass and resend if it's missing — "
            f"feel free to verify it landed too.")
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "channel_outbox.py"),
             "enqueue", "--kind", "notify", "--text", text, "--ref", thread_id,
             "--source", "journal_reconcile"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return  # best-effort; the unconfirmed row in the thread file is the durable record


def _reconcile_one(intent: dict) -> str:
    """Heal one stale intent. Returns one of:
      "reconciled" — folded an unconfirmed row, notified, and acked (a genuine crash orphan).
      "skipped"    — nothing to fold: either a CONFIRMED commit for this intent already exists, OR the
                     intent was already drained (a concurrent reconcile/commit acked it first). In both
                     cases the racing pending record is left clean and NO false notify is fired.
      "error"      — anything went wrong (fail-open: never raises).
    The still-pending re-check + the already-committed check + the fold + the ack all run UNDER the
    thread lock, so a commit OR a second reconcile holding the same lock cannot interleave between them
    (closes both the commit/reconcile double-journal AND the reconcile/reconcile double-count+notify).
    A notify is sent ONLY when this call's ack actually removed the record (ack -> {"acked": True}) —
    if another process already drained it, we return "skipped" and fire no duplicate nudge."""
    try:
        thread_id = intent.get("thread_id")
        intent_id = intent.get("id")
        if not thread_id or not intent_id:
            return "error"
        side = intent.get("side", "sell")
        path = journal_send.thread_file(thread_id, side)
        in_msg_id = intent.get("in_msg_id", "")
        out_text = intent.get("text", "")
        stamp = datetime.now(timezone.utc).isoformat()

        with atomic_io.locked(path):
            # Re-check the intent is STILL pending under the lock (mirrors journal_send.run_commit's
            # under-lock _find_intent re-check): a concurrent reconcile or a commit may have folded +
            # acked it in the window since we peeked. If it is gone, there is nothing to fold and the
            # other actor already notified — drain nothing, fire nothing.
            if journal_send._find_intent(thread_id, intent_id) is None:
                return "skipped"
            thread = journal_send._read_thread(path, thread_id)
            if _already_committed(thread, intent_id):
                # commit already folded the confirmed outbound; this pending record is a race
                # leftover (its ack is in flight). Drain it without an unconfirmed dup or a notify.
                thread_outbox.ack(intent_id)
                return "skipped"
            new_thread = _fold_unconfirmed(thread, intent_id=intent_id, in_msg_id=in_msg_id,
                                           out_text=out_text, now_iso=stamp)
            atomic_io.write_json(path, new_thread)
            acked = thread_outbox.ack(intent_id).get("acked", False)
        if not acked:
            # Another process drained this intent between our re-check and our ack — it already folded
            # and notified. Do NOT fire a duplicate verify-notify. (The fold above is idempotent: the
            # unconfirmed row is keyed by intent_id, so it added no duplicate.)
            return "skipped"
        _notify_verify(thread_id)
        return "reconciled"
    except (OSError, ValueError, KeyError, TypeError):
        return "error"


def reconcile(grace_sec: float = GRACE_SEC) -> dict:
    """Drain every stale orphan. NEVER raises — returns a JSON-able summary even on a bad outbox.

    An intent is only folded if it clears BOTH guards: it is older than `grace_sec` AND its market
    holds no live lease. An intent whose market has a live lease is SKIPPED (a worker is mid-pass on
    that market, so the intent is in-flight, not orphaned) — this is the primary defense against the
    concurrent supervisor stealing a still-pacing intent before its real send.

    EXCEPTION (the perpetually-busy-market backstop): once an intent ages past MAX_INTENT_AGE_SEC the
    live-lease guard is overridden and the intent is folded anyway. Past that ceiling the lease must
    have churned to a NEW worker (no single worker outlives the supervisor's MAX_WORKER_SEC watchdog),
    so the still-pending intent cannot be that worker's in-flight send — and skipping it forever would
    freeze the cursor and let a later pass re-reply."""
    try:
        stale = thread_outbox.peek(older_than_sec=grace_sec)["pending"]
    except (OSError, ValueError):
        # unreadable outbox → fail-open no-op
        return {"reconciled": 0, "skipped": 0, "errors": 0, "needs_verify": []}
    now = datetime.now(timezone.utc)
    reconciled = skipped = errors = 0
    needs_verify: list[str] = []  # thread_ids the next buyer pass must re-check in-chat (Fix 3)
    for intent in stale:
        too_old = _intent_age_sec(intent, now) > MAX_INTENT_AGE_SEC
        if not too_old and _market_has_live_lease(intent.get("market")):
            skipped += 1  # a live worker owns this market — the intent is in-flight, not an orphan
            continue
        outcome = _reconcile_one(intent)
        if outcome == "reconciled":
            reconciled += 1
            tid = intent.get("thread_id")
            if tid and tid not in needs_verify:
                needs_verify.append(tid)
        elif outcome == "skipped":   # already committed — drained without a fold/notify
            skipped += 1
        else:
            errors += 1
    return {"reconciled": reconciled, "skipped": skipped, "errors": errors,
            "needs_verify": needs_verify}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="journal_reconcile.py", add_help=True)
    parser.add_argument("--grace-sec", type=float, default=GRACE_SEC, dest="grace_sec")
    try:
        ns = parser.parse_args(argv[1:])
    except SystemExit:
        # Even a bad flag must not break the caller: report a no-op summary, exit 0.
        print(json.dumps({"reconciled": 0, "skipped": 0, "errors": 0}))
        return 0
    print(json.dumps(reconcile(ns.grace_sec)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
