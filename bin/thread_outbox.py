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

(tests relocate the whole data dir via BAZAAR_DATA_DIR; there is no per-invocation path override, so
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


def data_dir() -> Path:
    """The data directory — relocatable via BAZAAR_DATA_DIR (tests isolate on it)."""
    env = os.environ.get("BAZAAR_DATA_DIR")
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


def select_pending(records: list[dict], thread_id: str | None,
                   older_than_sec: float | None, now: datetime) -> list[dict]:
    """Return a NEW list of pending records, filtered by thread_id and/or minimum age.

    Only records still marked pending are returned (a non-pending status that somehow survived in the
    file — e.g. a partially-written row — must not leak into commit/reconcile). A record with an
    unparseable ts is treated as infinitely old (it cannot be timed, so a crash orphan must not hide
    behind a bad timestamp). Never mutates the input."""
    out: list[dict] = []
    for r in records:
        if r.get("status") != "pending":
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
    """Append one pending intent under the lock. Returns {"enqueued": True, "id": ...}."""
    path = path or outbox_path()
    record = build_record(thread_id, market, text, in_msg_id, now.isoformat(), side=side)
    _with_lock(path, lambda: _append_line(path, record))
    return {"enqueued": True, "id": record["id"]}


def peek(thread_id: str | None = None, older_than_sec: float | None = None,
         now: datetime | None = None, path: Path | None = None) -> dict:
    """Read-only snapshot of pending intents (optionally filtered by thread / age). Removes nothing."""
    path = path or outbox_path()
    now = now or datetime.now(timezone.utc)

    def _read():
        records = parse_records(_read_text(path))
        return select_pending(records, thread_id, older_than_sec, now)

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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="thread_outbox.py", add_help=False)
    parser.add_argument("command", choices=["enqueue", "peek", "ack", "fail"])
    parser.add_argument("--thread", default="", dest="thread")
    parser.add_argument("--market", default="")
    parser.add_argument("--in-msg", default="", dest="in_msg")
    parser.add_argument("--text", default="")
    parser.add_argument("--side", default=DEFAULT_SIDE)
    parser.add_argument("--id", default="", dest="rec_id")
    parser.add_argument("--older-than-sec", type=float, default=None, dest="older_than_sec")
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
    elif ns.command in ("ack", "fail"):
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
                          older_than_sec=ns.older_than_sec, now=now)
        elif ns.command == "fail":
            result = fail(ns.rec_id.strip())
        else:
            result = ack(ns.rec_id.strip())
    except (FileNotFoundError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
