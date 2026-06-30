#!/usr/bin/env python3
"""thread_outbox.py — the per-thread INTENT log that brackets a marketplace send (Fix A).

The Olaf split-brain: a reply lands on the marketplace, then the pass crashes (e.g. "Reached max
turns (40)") BEFORE the thread file is journaled, so the thread keeps a stale cursor and never records
the outbound. The fix is to make journaling deterministic and bracketed: write the INTENDED outbound
HERE before the browser send, then ack it after the send is folded into the thread file. A crash
between the two leaves a durable pending intent that bin/journal_reconcile.py heals.

This mirrors bin/channel_outbox.py's discipline exactly (a sidecar fcntl.flock, tmp+os.replace
rewrite, tolerant parse that skips garbage lines), but keyed by THREAD instead of the control channel.

STATE: data/thread_outbox.jsonl (append-only JSONL, one record per intended outbound):
  {"id": <str>, "ts": <iso>, "thread_id": <str>, "market": <str>, "text": <str>,
   "in_msg_id": <str>, "side": "sell"|"buy", "status": "pending", "attempts": <int>}

CLI (every mutation runs under an exclusive fcntl.flock so concurrent enqueues never corrupt a line):
    python3 thread_outbox.py enqueue --thread <id> --market <m> --in-msg <inbound> --text "<reply>"
                                     [--side sell|buy] [--now <iso>]
    python3 thread_outbox.py peek [--thread <id>] [--older-than-sec N] [--now <iso>]
    python3 thread_outbox.py ack --id <id>
    python3 thread_outbox.py fail --id <id>

Output (stdout, JSON). enqueue -> {"enqueued": true, "id": <str>};
peek -> {"pending": [<records>], "count": N}; ack -> {"acked": true|false};
fail -> {"failed": true|false, "attempts": N}. Errors -> stderr. Exit: 0 ok · 2 bad input · 3 runtime.

(tests relocate the whole data dir via SELLY_DATA_DIR; there is no per-invocation path override, so
 every process competes on the same lock file.)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

VALID_SIDES = ("sell", "buy")
DEFAULT_SIDE = "sell"
MAX_NOW_DRIFT_SEC = 300  # --now is a narrow test seam: clamp it to wall clock (no time-travel)

# Intent lifecycle: pending (intent journaled, send not yet fired) -> sent_unverified (the browser
# send FIRED, commit not yet folded) -> [acked/removed] (commit confirmed it). The intermediate
# 'sent_unverified' state is what lets a recovery path tell "send fired, commit lost" (cursor-advance
# is correct) apart from "send never fired" (must re-drive) — see bin/journal_reconcile.py.
STATUS_PENDING = "pending"
STATUS_SENT = "sent_unverified"
OPEN_STATUSES = (STATUS_PENDING, STATUS_SENT)  # work still in flight (not yet confirmed/acked)


def data_dir() -> Path:
    """The data directory — relocatable via SELLY_DATA_DIR (tests isolate on it)."""
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


def outbox_path() -> Path:
    return data_dir() / "thread_outbox.jsonl"


# ---------------------------------------------------------------------------
# pure helpers (no IO) — directly unit-tested
# ---------------------------------------------------------------------------
def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime (Python 3.9 safe)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def new_id() -> str:
    """A collision-free id, unique even for same-timestamp concurrent appends."""
    return uuid.uuid4().hex


def build_record(thread_id: str, market: str, text: str, in_msg_id: str, now_iso: str,
                 side: str = DEFAULT_SIDE, rec_id: str | None = None) -> dict:
    """Return a NEW intent record dict. Pure: no IO, mutates nothing."""
    return {
        "id": rec_id or new_id(),
        "ts": now_iso,
        "thread_id": thread_id,
        "market": market,
        "text": text,
        "in_msg_id": in_msg_id,
        "side": side if side in VALID_SIDES else DEFAULT_SIDE,
        "status": "pending",
        "attempts": 0,
    }


def parse_records(text: str | None) -> list[dict]:
    """Parse JSONL text into a NEW list of records, oldest-first (file = append order).

    Tolerant: blank lines and torn/corrupt lines are skipped, so a half-written final line (a crash
    mid-append) never breaks a future peek/ack. Records missing an id are dropped (an un-ackable
    record would otherwise leak forever)."""
    records: list[dict] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            records.append(obj)
    return records


def select_by_statuses(records: list[dict], statuses: tuple[str, ...], thread_id: str | None,
                       older_than_sec: float | None, now: datetime) -> list[dict]:
    """Return a NEW list of records whose status is in `statuses`, filtered by thread_id and/or age.

    A record with a status outside `statuses` (e.g. a partially-written row, or a sent_unverified row
    when only pending was asked for) is excluded. A record with an unparseable ts is treated as
    infinitely old (it cannot be timed, so a crash orphan must not hide behind a bad timestamp). The
    age is measured from `ts` (intent-creation time) uniformly for every status. Never mutates input."""
    out: list[dict] = []
    for r in records:
        if r.get("status") not in statuses:
            continue
        if thread_id is not None and r.get("thread_id") != thread_id:
            continue
        if older_than_sec is not None:
            ts = parse_iso(r.get("ts"))
            age = (now - ts).total_seconds() if ts is not None else float("inf")
            if age < older_than_sec:
                continue
        out.append(dict(r))
    return out


def select_pending(records: list[dict], thread_id: str | None,
                   older_than_sec: float | None, now: datetime) -> list[dict]:
    """Pending-only view (status == 'pending'), filtered by thread_id and/or minimum age.
    Thin wrapper over select_by_statuses — kept for callers that only want never-sent intents."""
    return select_by_statuses(records, (STATUS_PENDING,), thread_id, older_than_sec, now)


def find_pending_by_inbound(records: list[dict], thread_id: str, in_msg_id: str) -> dict | None:
    """Return a COPY of the first PENDING record for (thread_id, in_msg_id), or None.

    The dedup key for enqueue: a re-ask / re-drive of the SAME inbound message must REUSE the existing
    pending intent rather than mint a second one (the two-stranded-copies bug). Matches status
    'pending' ONLY — a 'sent_unverified' record means the browser send already fired, so a fresh
    enqueue there is a deliberate resend (e.g. after a verify-miss) and must not collapse onto it.
    Never mutates the input."""
    for r in records:
        if (r.get("status") == STATUS_PENDING and r.get("thread_id") == thread_id
                and r.get("in_msg_id") == in_msg_id):
            return dict(r)
    return None


def set_fields(records: list[dict], rec_id: str, **fields) -> tuple[list[dict], bool]:
    """Return (new_records, found): a NEW list with `rec_id`'s fields overlaid (e.g. a status flip or
    a text refresh). Never mutates the input list or any record in it."""
    out: list[dict] = []
    found = False
    for r in records:
        if r.get("id") == rec_id:
            found = True
            out.append({**r, **fields})
        else:
            out.append(dict(r))
    return out, found


def remove_id(records: list[dict], rec_id: str) -> tuple[list[dict], bool]:
    """Return (new_records, removed): a NEW list with `rec_id` dropped, never mutating input."""
    kept = [dict(r) for r in records if r.get("id") != rec_id]
    removed = len(kept) != len(records)
    return kept, removed


def bump_attempts(records: list[dict], rec_id: str) -> tuple[list[dict], int, bool]:
    """Return (new_records, attempts, found): a NEW list with `rec_id`'s attempts incremented."""
    out: list[dict] = []
    attempts = 0
    found = False
    for r in records:
        if r.get("id") == rec_id:
            attempts = int(r.get("attempts", 0)) + 1
            found = True
            out.append({**r, "attempts": attempts})
        else:
            out.append(dict(r))
    return out, attempts, found


def serialize(records: list[dict]) -> str:
    """Render records back to JSONL text (one compact object per line, trailing newline)."""
    if not records:
        return ""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


# ---------------------------------------------------------------------------
# IO — every mutation serialized under an exclusive file lock
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")


def _append_line(path: Path, record: dict) -> None:
    """Append one record as a single JSONL line. Called only while holding the lock."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _atomic_write(path: Path, records: list[dict]) -> None:
    """Atomic rewrite: temp file (0600) + os.replace, so a crash mid-ack never leaves a half-written
    outbox — the original stays intact until the rename succeeds."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(serialize(records))
    os.replace(tmp, path)


def _with_lock(path: Path, fn):
    """Run `fn()` while holding an exclusive flock on a sidecar .lock fd."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def enqueue(thread_id: str, market: str, text: str, in_msg_id: str, now: datetime,
            side: str = DEFAULT_SIDE, path: Path | None = None) -> dict:
    """Record a pending intent BEFORE the browser send, deduped by (thread_id, in_msg_id).

    If a PENDING intent for the same inbound message already exists, REUSE it (refresh `text` + `ts`
    to the latest reply) rather than appending a duplicate — a re-ask or a re-drive of the same
    message must never strand a second copy. The read + the reuse-or-append all run under the one
    lock so a concurrent enqueue can't slip a duplicate in. Returns
    {"enqueued": True, "id": <id>, "deduped": <bool>}."""
    path = path or outbox_path()
    now_iso = now.isoformat()

    def _mutate():
        records = parse_records(_read_text(path))
        existing = find_pending_by_inbound(records, thread_id, in_msg_id)
        if existing is not None:
            updated, _ = set_fields(records, existing["id"], text=text, ts=now_iso)
            _atomic_write(path, updated)
            return {"enqueued": True, "id": existing["id"], "deduped": True}
        record = build_record(thread_id, market, text, in_msg_id, now_iso, side=side)
        _append_line(path, record)
        return {"enqueued": True, "id": record["id"], "deduped": False}

    return _with_lock(path, _mutate)


def mark_sent(rec_id: str, now: datetime | None = None, path: Path | None = None) -> dict:
    """Flip a PENDING intent to 'sent_unverified' and stamp `sent_ts` — the durable record that the
    browser send FIRED (distinct from the intent merely being journaled). Returns {"marked": bool}.
    Only a still-pending record is flipped; an unknown or already-sent id is a clean no-op."""
    path = path or outbox_path()
    now = now or datetime.now(timezone.utc)

    def _mutate():
        records = parse_records(_read_text(path))
        target = next((r for r in records if r.get("id") == rec_id
                       and r.get("status") == STATUS_PENDING), None)
        if target is None:
            return False
        updated, _ = set_fields(records, rec_id, status=STATUS_SENT, sent_ts=now.isoformat())
        _atomic_write(path, updated)
        return True

    return {"marked": bool(_with_lock(path, _mutate))}


def mark_escalated(rec_id: str, now: datetime | None = None, path: Path | None = None) -> dict:
    """Stamp an intent as surfaced-to-user (durable, exactly-once marker for the outbox sweeper — see
    bin/agent_daemon.py). The record stays pending and visible; this only records that we have already
    alarmed once, so re-drives (which bump `attempts`) can never race a second escalation. Mirrors
    mark_sent: a clean no-op on an unknown id. Returns {"escalated": bool}."""
    path = path or outbox_path()
    now = now or datetime.now(timezone.utc)

    def _mutate():
        records = parse_records(_read_text(path))
        if not any(r.get("id") == rec_id for r in records):
            return False
        updated, _ = set_fields(records, rec_id, escalated=True, escalated_ts=now.isoformat())
        _atomic_write(path, updated)
        return True

    return {"escalated": bool(_with_lock(path, _mutate))}


def peek(thread_id: str | None = None, older_than_sec: float | None = None,
         now: datetime | None = None, path: Path | None = None,
         statuses: tuple[str, ...] = (STATUS_PENDING,)) -> dict:
    """Read-only snapshot of intents in `statuses` (optionally filtered by thread / age). Removes
    nothing. Default is pending-only (the legacy behavior); pass OPEN_STATUSES to also see
    sent_unverified work in flight. The result key stays "pending" for backward compatibility."""
    path = path or outbox_path()
    now = now or datetime.now(timezone.utc)

    def _read():
        records = parse_records(_read_text(path))
        return select_by_statuses(records, statuses, thread_id, older_than_sec, now)

    pending = _with_lock(path, _read)
    return {"pending": pending, "count": len(pending)}


def ack(rec_id: str, path: Path | None = None) -> dict:
    """Atomically remove the intent with `id == rec_id`. Returns {"acked": True|False}."""
    path = path or outbox_path()

    def _mutate():
        records = parse_records(_read_text(path))
        kept, removed = remove_id(records, rec_id)
        if removed:
            _atomic_write(path, kept)
        return removed

    return {"acked": bool(_with_lock(path, _mutate))}


def fail(rec_id: str, path: Path | None = None) -> dict:
    """Increment the attempt count for `rec_id` (the record stays pending). Returns {failed, attempts}."""
    path = path or outbox_path()

    def _mutate():
        records = parse_records(_read_text(path))
        bumped, attempts, found = bump_attempts(records, rec_id)
        if found:
            _atomic_write(path, bumped)
        return attempts, found

    attempts, found = _with_lock(path, _mutate)
    return {"failed": bool(found), "attempts": attempts}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_now(now_arg: str) -> datetime:
    if not now_arg:
        return datetime.now(timezone.utc)
    parsed = parse_iso(now_arg)
    if parsed is None:
        raise ValueError(f"could not parse --now {now_arg!r}")
    drift = abs((parsed - datetime.now(timezone.utc)).total_seconds())
    if drift > MAX_NOW_DRIFT_SEC:
        raise ValueError(f"--now deviates from wall clock by {drift:.0f}s (max {MAX_NOW_DRIFT_SEC})")
    return parsed


# peek --status -> the status set to return (CLI default 'open' = all in-flight work, the most useful
# human/sweeper view; the Python peek() default stays pending-only for legacy callers).
_PEEK_STATUS_MAP = {
    "pending": (STATUS_PENDING,),
    "sent_unverified": (STATUS_SENT,),
    "open": OPEN_STATUSES,
}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="thread_outbox.py", add_help=False)
    parser.add_argument("command", choices=["enqueue", "peek", "ack", "fail", "sent"])
    parser.add_argument("--thread", default="", dest="thread")
    parser.add_argument("--market", default="")
    parser.add_argument("--in-msg", default="", dest="in_msg")
    parser.add_argument("--text", default="")
    parser.add_argument("--side", default=DEFAULT_SIDE)
    parser.add_argument("--id", default="", dest="rec_id")
    parser.add_argument("--older-than-sec", type=float, default=None, dest="older_than_sec")
    parser.add_argument("--status", default="open", choices=list(_PEEK_STATUS_MAP))
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def _validate(ns: argparse.Namespace) -> None:
    """Validate at the boundary; raise ValueError (-> exit 2) on bad input."""
    if ns.command == "enqueue":
        if not ns.thread.strip():
            raise ValueError("enqueue requires --thread <id>")
        if not ns.market.strip():
            raise ValueError("enqueue requires --market <m>")
        if not ns.in_msg.strip():
            raise ValueError("enqueue requires --in-msg <inbound_msg_id>")
        if not ns.text.strip():
            raise ValueError("enqueue requires a non-empty --text")
        if ns.side not in VALID_SIDES:
            raise ValueError(f"--side must be one of {VALID_SIDES}, got {ns.side!r}")
    elif ns.command in ("ack", "fail", "sent"):
        if not ns.rec_id.strip():
            raise ValueError(f"{ns.command} requires --id <id>")


def main(argv: list[str]) -> int:
    try:
        ns = _parse_args(argv)
        _validate(ns)
        now = _resolve_now(ns.now)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        if ns.command == "enqueue":
            result = enqueue(ns.thread.strip(), ns.market.strip(), ns.text, ns.in_msg.strip(),
                             now, side=ns.side)
        elif ns.command == "peek":
            result = peek(thread_id=ns.thread.strip() or None,
                          older_than_sec=ns.older_than_sec, now=now,
                          statuses=_PEEK_STATUS_MAP.get(ns.status, OPEN_STATUSES))
        elif ns.command == "fail":
            result = fail(ns.rec_id.strip())
        elif ns.command == "sent":
            result = mark_sent(ns.rec_id.strip(), now)
        else:
            result = ack(ns.rec_id.strip())
    except (FileNotFoundError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
