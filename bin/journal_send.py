#!/usr/bin/env python3
"""journal_send.py — deterministic, crash-safe journaling bracketed around a marketplace send (Fix A).

The Olaf split-brain: a marketplace pass sends a reply in the browser, then journals the thread file
at the END of the pass by having the LLM hand-edit the JSON. When the pass crashes AFTER the send but
BEFORE the journal (e.g. "Reached max turns (40)"), the reply is live on the marketplace but the
thread file keeps a stale cursor and never records the outbound — a split-brain ledger.

Fix: make journaling a deterministic PYTHON write bracketed around the send.

  intent  — RIGHT BEFORE the browser send: validate, enqueue the INTENDED outbound to
            bin/thread_outbox.py (a durable pending record), print the intent {id}.
  commit  — RIGHT AFTER send() returns: under atomic_io.locked(<threadfile>), read the thread file,
            append the inbound row (deduped by in_msg_id), append the outbound row
            (msg_id = "out|<iso-ts>", text from the intent record), advance the cursor to the inbound,
            set status if given, refresh updated_at, write via atomic_io.write_json, then ack the
            intent. IDEMPOTENT (re-running the same commit never duplicates rows or double-advances),
            IMMUTABLE (never mutates the read dict — builds a new one), FAIL-OPEN to a skeleton thread
            when the file is missing.

A crash between intent and commit leaves the pending intent; bin/journal_reconcile.py heals it
(folds it as unconfirmed, advances the cursor so the next pass won't auto-resend, asks the seller to
verify). The reply is NEVER re-sent.

CLI:
  python3 journal_send.py intent --thread <id> --market <m> --in-msg <inbound_msg_id>
                                 --text "<reply>" [--side sell|buy]
  python3 journal_send.py commit --thread <id> --intent <id> [--status <s>]
                                 [--side sell|buy] [--threads-dir <path>]

--side sell -> data/threads/<id>.json · --side buy -> data/buyer_threads/<id>.json
(--threads-dir overrides the directory). Exit: 0 ok · 2 bad args · 3 runtime error.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  crash-safe (tmp + os.replace) writes + the per-file lock
import thread_outbox  # noqa: E402  the intent log we enqueue to / ack against

VALID_SIDES = ("sell", "buy")
DEFAULT_SIDE = "sell"
SIDE_DIRS = {"sell": "threads", "buy": "buyer_threads"}


# ---------------------------------------------------------------------------
# path / data resolution
# ---------------------------------------------------------------------------
def data_dir() -> Path:
    """Shared with thread_outbox so tests isolate the whole tree via SELLY_DATA_DIR."""
    return thread_outbox.data_dir()


def thread_file(thread_id: str, side: str, threads_dir: str | None = None) -> Path:
    """Resolve the thread file path. --threads-dir wins; else side -> threads/buyer_threads."""
    if threads_dir:
        return Path(threads_dir) / f"{thread_id}.json"
    return data_dir() / SIDE_DIRS[side] / f"{thread_id}.json"


# ---------------------------------------------------------------------------
# pure builders (no IO) — directly unit-tested
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def skeleton_thread(thread_id: str) -> dict:
    """A minimal, well-formed thread structure (fail-open target when the file is missing)."""
    return {
        "thread_id": thread_id,
        "cursor": {"last_handled_msg_id": None, "last_handled_ts": None},
        "status": "active",
        "transcript": [],
        "updated_at": None,
    }


def _has_msg_id(transcript: list, msg_id: str) -> bool:
    return any(isinstance(r, dict) and r.get("msg_id") == msg_id for r in transcript)


def inbound_text_from_msg_id(in_msg_id: str) -> str:
    """Inbound msg_ids are human-readable "<time>|<text>" (see data/threads/*.json). Recover the text
    half for the transcript row; fall back to the whole id when there is no separator."""
    return in_msg_id.split("|", 1)[1] if "|" in in_msg_id else in_msg_id


def fold_commit(thread: dict, *, in_msg_id: str, in_text: str, out_text: str,
                status: str | None, now_iso: str, out_msg_id: str | None = None) -> dict:
    """Return a NEW thread dict with the inbound + outbound folded in and the cursor advanced.

    IMMUTABLE: never mutates `thread` — deep-copies it so even nested objects (e.g. an `agent_note`
    or any future nested field) are never shared with the caller's input dict.
    IDEMPOTENT: the inbound row is appended only if its msg_id is not already present; the outbound
    row is appended only if an outbound with the SAME deterministic msg_id is not already present.
    The outbound msg_id MUST be derived from the intent id (stable across retries / wall-clock
    drift), not a fresh timestamp — otherwise two retries at different instants produce two ids and
    duplicate the row. The caller passes `out_msg_id="out|<intent_id>"`; the `now_iso` fallback exists
    only for direct unit calls.
    """
    new_thread = copy.deepcopy(thread)
    transcript = list(new_thread.get("transcript") or [])
    out_id = out_msg_id or f"out|{now_iso}"

    if in_msg_id and not _has_msg_id(transcript, in_msg_id):
        transcript.append({"msg_id": in_msg_id, "dir": "in", "text": in_text, "ts": now_iso})
    if not _has_msg_id(transcript, out_id):
        transcript.append({"msg_id": out_id, "dir": "out", "text": out_text, "ts": now_iso})

    new_thread["transcript"] = transcript
    if in_msg_id:
        new_thread["cursor"] = {"last_handled_msg_id": in_msg_id, "last_handled_ts": now_iso}
    if status:
        new_thread["status"] = status
    new_thread["updated_at"] = now_iso
    return new_thread


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def _read_thread(path: Path, thread_id: str) -> dict:
    """Read the thread file, fail-open to a skeleton when it is missing or unreadable."""
    if not path.exists():
        return skeleton_thread(thread_id)
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return skeleton_thread(thread_id)
    if not isinstance(obj, dict):
        return skeleton_thread(thread_id)
    return obj


def run_intent(thread_id: str, market: str, in_msg_id: str, text: str, side: str) -> dict:
    """Enqueue the intended outbound BEFORE the browser send. Deduped by (thread_id, in_msg_id): a
    re-ask / re-drive of the same inbound message reuses the existing pending intent (so the returned
    id is stable across retries). Returns {"id": <intent_id>, "deduped": <bool>}."""
    result = thread_outbox.enqueue(thread_id, market, text, in_msg_id,
                                   datetime.now(timezone.utc), side=side)
    return {"id": result["id"], "thread_id": thread_id, "deduped": result.get("deduped", False)}


def run_mark_sent(intent_id: str) -> dict:
    """Record that the browser send FIRED — flip the intent pending -> sent_unverified (Track A2).
    Called between send() and commit so a recovery path can tell "send fired, commit lost" from
    "send never fired". A no-op if the intent is already sent/committed. Returns {"marked": <bool>}."""
    return thread_outbox.mark_sent(intent_id)


def _find_intent(thread_id: str, intent_id: str) -> dict | None:
    # Look across OPEN statuses (pending AND sent_unverified) so commit can still find an intent whose
    # send already fired (status flipped to sent_unverified by run_mark_sent before this commit).
    pending = thread_outbox.peek(thread_id=thread_id, statuses=thread_outbox.OPEN_STATUSES)["pending"]
    return next((r for r in pending if r.get("id") == intent_id), None)


def _commit_side(intent: dict, requested_side: str | None) -> str:
    """Reconcile the side to journal to. The INTENT record is the source of truth (the side was
    captured at intent-time, before the send). A CLI --side that DISAGREES is a hard error rather
    than silently mis-filing — e.g. an LLM that drops `--side buy` on a buy commit must never fold a
    buyer reply into the seller tree (the cursor advances on the wrong thread → an Olaf-class re-reply).
    When no --side is supplied, the intent's side is used unconditionally."""
    intent_side = intent.get("side") if intent.get("side") in VALID_SIDES else DEFAULT_SIDE
    if requested_side and requested_side in VALID_SIDES and requested_side != intent_side:
        raise ValueError(
            f"--side {requested_side!r} contradicts the intent's recorded side {intent_side!r}; "
            f"the intent is the source of truth — drop --side or pass the matching one")
    return intent_side


def run_commit(thread_id: str, intent_id: str, requested_side: str | None, status: str | None,
               threads_dir: str | None) -> dict:
    """Fold the intent's outbound into the thread file and ack the intent — both UNDER one lock.

    Idempotent: if the intent is already acked (a re-run after a successful commit, or after
    journal_reconcile already healed it), there is nothing to fold — a clean no-op, not an error.
    The intent is re-checked UNDER the thread lock to close the TOCTOU window with a concurrent
    journal_reconcile pass, the outbound msg_id is derived from the intent id (stable across retries)
    so a racing fold can never produce a duplicate outbound row, and the ack runs INSIDE that same
    lock so the fold + ack are atomic — a reconcile cannot slip in between them and double-journal.

    The side is taken from the INTENT record (the source of truth), NOT the CLI --side. An explicit
    --threads-dir still wins (a test/override seam); otherwise the path is resolved from the intent's
    side, and a contradictory CLI --side is a hard error (see _commit_side)."""
    # Cheap pre-check outside the lock (avoids taking the lock for an already-acked intent). It also
    # gives us the intent's recorded side, the source of truth for which thread file to journal to.
    intent = _find_intent(thread_id, intent_id)
    if intent is None:
        return {"committed": False, "reason": "intent_not_pending", "thread_id": thread_id}
    side = _commit_side(intent, requested_side)
    path = thread_file(thread_id, side, threads_dir)

    stamp = now_iso()
    out_id = f"out|{intent_id}"  # deterministic across retries — the dedup key for the outbound row
    with atomic_io.locked(path):
        # Re-check under the lock: a reconcile pass may have folded + acked this intent in the window
        # between the pre-check and acquiring the lock. (text + in_msg_id live ONLY on the intent.)
        intent = _find_intent(thread_id, intent_id)
        if intent is None:
            return {"committed": False, "reason": "intent_not_pending", "thread_id": thread_id}
        in_msg_id = intent.get("in_msg_id", "")
        out_text = intent.get("text", "")
        thread = _read_thread(path, thread_id)
        new_thread = fold_commit(thread, in_msg_id=in_msg_id,
                                 in_text=inbound_text_from_msg_id(in_msg_id),
                                 out_text=out_text, status=status, now_iso=stamp,
                                 out_msg_id=out_id)
        atomic_io.write_json(path, new_thread)
        # Ack INSIDE the thread lock so the fold + ack are atomic: a reconcile that grabs this same
        # lock can never observe the intent still pending after the fold and journal a duplicate
        # unconfirmed row. (ack takes the separate thread_outbox lock, so there is no deadlock.)
        thread_outbox.ack(intent_id)
    return {"committed": True, "thread_id": thread_id, "intent": intent_id}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_side(ns: argparse.Namespace) -> str:
    """A --threads-dir override stands in for a side; otherwise --side must be sell|buy."""
    if ns.threads_dir:
        return ns.side if ns.side in VALID_SIDES else DEFAULT_SIDE
    if ns.side not in VALID_SIDES:
        raise ValueError("a target side is required: pass --side sell|buy (or --threads-dir)")
    return ns.side


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="journal_send.py", add_help=False)
    parser.add_argument("command", choices=["intent", "commit", "mark-sent"])
    parser.add_argument("--thread", default="", dest="thread")
    parser.add_argument("--market", default="")
    parser.add_argument("--in-msg", default="", dest="in_msg")
    parser.add_argument("--text", default="")
    parser.add_argument("--intent", default="", dest="intent_id")
    parser.add_argument("--status", default="")
    parser.add_argument("--side", default="")
    parser.add_argument("--threads-dir", default="", dest="threads_dir")
    return parser.parse_args(argv[1:])


def _validate(ns: argparse.Namespace) -> str | None:
    """Validate at the boundary; raise ValueError (-> exit 2).

    Returns the resolved side for `intent` (which must know where to file before any record exists).
    For `commit` the side is OPTIONAL — the intent record is the source of truth — so this returns
    the raw requested side (sell|buy) or None when omitted; run_commit reconciles it against the
    intent and errors on a contradiction. `mark-sent` needs only --intent (no thread/side)."""
    if ns.command == "mark-sent":
        if not ns.intent_id.strip():
            raise ValueError("mark-sent requires --intent <intent_id>")
        return None
    if not ns.thread.strip():
        raise ValueError(f"{ns.command} requires --thread <id>")
    if ns.command == "intent":
        if not ns.market.strip():
            raise ValueError("intent requires --market <m>")
        if not ns.in_msg.strip():
            raise ValueError("intent requires --in-msg <inbound_msg_id>")
        if not ns.text.strip():
            raise ValueError("intent requires a non-empty --text")
        return _resolve_side(ns)
    # commit: --side is optional (derived from the intent). A given --side must still be valid.
    if not ns.intent_id.strip():
        raise ValueError("commit requires --intent <intent_id>")
    if ns.side and ns.side not in VALID_SIDES:
        raise ValueError(f"--side must be one of {VALID_SIDES}, got {ns.side!r}")
    return ns.side or None


def main(argv: list[str]) -> int:
    try:
        ns = _parse_args(argv)
        side = _validate(ns)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        if ns.command == "intent":
            result = run_intent(ns.thread.strip(), ns.market.strip(), ns.in_msg.strip(),
                                ns.text, side)
        elif ns.command == "mark-sent":
            result = run_mark_sent(ns.intent_id.strip())
        else:
            result = run_commit(ns.thread.strip(), ns.intent_id.strip(), side,
                                ns.status.strip() or None, ns.threads_dir.strip() or None)
    except (FileNotFoundError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
