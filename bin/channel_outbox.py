#!/usr/bin/env python3
"""channel_outbox.py — the single-writer control-channel outbox.

Under concurrency, multiple background workers (the seller pass, a buyer-reply pass, an
eval pass) all want to surface a user-facing notice on the ONE control channel (Telegram).
If each wrote to that channel directly their messages would interleave and race. Instead
they ENQUEUE the notice here; later the single privileged channel worker (bin/harness_run.py)
drains the outbox and sends the notices IN ORDER, one writer, no interleaving.

This is a durable hand-off queue, not a transcript: channel_log.py journals what physically
crossed the channel (after the fact); this file is the to-send buffer that feeds it. FIFO is
the append order of the JSONL file; `ack` (not `peek`) is what removes a record, so a crash
between draining and sending never loses a notice — the worker re-peeks and re-sends.

STATE: data/channel_outbox.jsonl (append-only JSONL, one record per line):
  {"id": <str>, "ts": <iso>, "kind": "notify"|"say", "text": <str>,
   "ref": <str|null>, "source": <str|null>, "status": "pending"}

  id      unique even for same-timestamp concurrent appends (uuid4 hex) — the ack key.
  kind    notify = an unprompted heads-up · say = a reply the worker should voice.
  ref     optional correlation id (thread/item) the channel worker can thread the notice to.
  source  optional worker label (e.g. "sell-run", "buy-run") for debugging order.

CLI (every mutation runs under an exclusive fcntl.flock so concurrent enqueues from many
processes can never corrupt a line or interleave a half-written record):
    python3 channel_outbox.py enqueue --kind notify --text "..." [--ref <id>]
                                       [--source <label>] [--now <iso>]
    python3 channel_outbox.py peek [--limit N] [--now <iso>]
    python3 channel_outbox.py ack --id <id>

Output (stdout, JSON). enqueue -> {"enqueued": true, "id": <str>};
peek -> {"pending": [<records, FIFO append order>], "count": N}; ack -> {"acked": true|false}.
Errors -> {"error": "..."} to stderr. Exit codes: 0 ok · 2 bad input · 3 data missing/invalid.

(tests relocate the whole data dir via the SELLY_DATA_DIR env var; there is no per-invocation
 path override, so every process competes on the same lock file.)
"""

import argparse
import fcntl
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

VALID_KINDS = ("notify", "say")
DEFAULT_PEEK_LIMIT = 0       # 0 = no limit; return every pending record
MAX_NOW_DRIFT_SEC = 300      # --now is a narrow test seam: clamp it to wall clock (no time-travel)
MAX_SEND_ATTEMPTS = 3        # after this many failed sends a notice is dead-lettered (no infinite retry)


def data_dir():
    """The data directory — relocatable via SELLY_DATA_DIR (used by tests for isolation).
    There is no per-invocation override, so all production processes share one lock file."""
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


# ---------------------------------------------------------------------------
# pure helpers (no IO) — directly unit-tested
# ---------------------------------------------------------------------------
def parse_iso(value):
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


def new_id():
    """A collision-free id, unique even for same-timestamp concurrent appends."""
    return uuid.uuid4().hex


def build_record(kind, text, now_iso, ref=None, source=None, rec_id=None):
    """Return a NEW outbox record dict. Pure: no IO, mutates nothing."""
    return {
        "id": rec_id or new_id(),
        "ts": now_iso,
        "kind": kind,
        "text": text,
        "ref": ref if ref else None,
        "source": source if source else None,
        "status": "pending",
    }


def parse_records(text):
    """Parse JSONL text into a NEW list of records, oldest-first (file = append order).

    Tolerant: blank lines and torn/corrupt lines are skipped, so a half-written final line
    (e.g. a crash mid-append) never breaks a future peek/ack. Records missing an id are
    dropped — an un-ackable record would otherwise leak forever."""
    records = []
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


def select_pending(records, limit):
    """Return a NEW list of the first `limit` records in FIFO append order (all if limit<=0)."""
    if limit and limit > 0:
        return [dict(r) for r in records[:limit]]
    return [dict(r) for r in records]


def remove_id(records, rec_id):
    """Return (new_records, removed): a NEW list with `rec_id` dropped, never mutating input."""
    kept = [dict(r) for r in records if r.get("id") != rec_id]
    removed = len(kept) != len(records)
    return kept, removed


def serialize(records):
    """Render records back to JSONL text (one compact object per line, trailing newline)."""
    if not records:
        return ""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


# ---------------------------------------------------------------------------
# IO — every mutation serialized under an exclusive file lock
# ---------------------------------------------------------------------------
def _read_text(outbox_path):
    if not outbox_path.exists():
        return ""
    return outbox_path.read_text(errors="replace")


def _append_line(outbox_path, record):
    """Append one record as a single JSONL line. Called only while holding the lock, so two
    concurrent enqueues are serialized and a line can never interleave with another's."""
    with outbox_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _atomic_write(outbox_path, records):
    """Atomic rewrite: temp file (0600 via os.open) + os.replace, so a crash mid-ack never
    leaves a half-written outbox — the original stays intact until the rename succeeds."""
    tmp = outbox_path.with_name(outbox_path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(serialize(records))
    os.replace(tmp, outbox_path)


def _with_lock(outbox_path, fn):
    """Run `fn()` while holding an exclusive flock on a sidecar .lock fd. Mutations (append /
    rewrite) happen inside, so concurrent processes serialize on the one lock file."""
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = outbox_path.with_name(outbox_path.name + ".lock")
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def run_enqueue(kind, text, now, outbox_path, ref=None, source=None):
    """Append one pending record under the lock. Returns {"enqueued": True, "id": ...}."""
    record = build_record(kind, text, now.isoformat(), ref=ref, source=source)
    _with_lock(outbox_path, lambda: _append_line(outbox_path, record))
    return {"enqueued": True, "id": record["id"]}


def run_peek(limit, outbox_path):
    """Read-only snapshot of pending records in FIFO append order. Removes nothing.

    Taken under the lock so a peek never observes a torn line mid-append."""
    def _read():
        records = parse_records(_read_text(outbox_path))
        return select_pending(records, limit)

    pending = _with_lock(outbox_path, _read)
    return {"pending": pending, "count": len(pending)}


def run_ack(rec_id, outbox_path):
    """Atomically remove the record with `id == rec_id`. Returns {"acked": True|False}."""
    def _mutate():
        records = parse_records(_read_text(outbox_path))
        kept, removed = remove_id(records, rec_id)
        if removed:
            _atomic_write(outbox_path, kept)
        return removed

    removed = _with_lock(outbox_path, _mutate)
    return {"acked": bool(removed)}


def _deadletter_path(outbox_path):
    return outbox_path.with_name(outbox_path.stem + ".deadletter.jsonl")


def run_fail(rec_id, max_attempts, outbox_path):
    """Record a failed send for `rec_id`: increment its `attempts`; once it reaches `max_attempts`
    move it to the dead-letter file and drop it from the live queue (so one poison notice can't
    head-of-line-block the queue forever). Returns {failed, dead_lettered, attempts}. The dead-letter
    append happens under the same lock, so it's serialized too."""
    def _mutate():
        records = parse_records(_read_text(outbox_path))
        result = {"failed": False, "dead_lettered": False, "attempts": 0}
        kept = []
        for r in records:
            if r.get("id") != rec_id:
                kept.append(r)
                continue
            attempts = int(r.get("attempts", 0)) + 1
            result.update(failed=True, attempts=attempts)
            if attempts >= max_attempts:
                _append_line(_deadletter_path(outbox_path), {**r, "attempts": attempts,
                                                             "status": "deadletter"})
                result["dead_lettered"] = True   # dropped from live (not re-added to kept)
            else:
                kept.append({**r, "attempts": attempts})
        _atomic_write(outbox_path, kept)
        return result

    return _with_lock(outbox_path, _mutate)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_now(now_arg):
    if not now_arg:
        return datetime.now().astimezone()
    parsed = parse_iso(now_arg)
    if parsed is None:
        raise ValueError(f"could not parse --now {now_arg!r}")
    # --now is a narrow test seam, not a control input: clamp it to wall clock so a stray or
    # hostile timestamp can't backdate the FIFO ordering of a notice.
    drift = abs((parsed - datetime.now(timezone.utc)).total_seconds())
    if drift > MAX_NOW_DRIFT_SEC:
        raise ValueError(f"--now deviates from wall clock by {drift:.0f}s (max {MAX_NOW_DRIFT_SEC})")
    return parsed


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="channel_outbox.py", add_help=False)
    parser.add_argument("command", choices=["enqueue", "peek", "ack", "fail"])
    parser.add_argument("--kind", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--id", default="", dest="rec_id")
    parser.add_argument("--limit", type=int, default=DEFAULT_PEEK_LIMIT)
    parser.add_argument("--max", type=int, default=MAX_SEND_ATTEMPTS, dest="max_attempts")
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def _validate(ns):
    """Validate at the boundary; raise ValueError (-> exit 2) on bad input."""
    if ns.command == "enqueue":
        kind = ns.kind.strip()
        if kind not in VALID_KINDS:
            raise ValueError(f"--kind must be one of {VALID_KINDS}, got {ns.kind!r}")
        if not ns.text.strip():
            raise ValueError("enqueue requires a non-empty --text")
    elif ns.command in ("ack", "fail"):
        if not ns.rec_id.strip():
            raise ValueError(f"{ns.command} requires --id <id>")


def main(argv):
    try:
        ns = _parse_args(argv)
        _validate(ns)
        now = _resolve_now(ns.now)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        outbox_path = data_dir() / "channel_outbox.jsonl"
        if ns.command == "enqueue":
            result = run_enqueue(ns.kind.strip(), ns.text, now, outbox_path,
                                 ref=ns.ref.strip() or None, source=ns.source.strip() or None)
        elif ns.command == "peek":
            result = run_peek(ns.limit, outbox_path)
        elif ns.command == "fail":
            result = run_fail(ns.rec_id.strip(), ns.max_attempts, outbox_path)
        else:
            result = run_ack(ns.rec_id.strip(), outbox_path)
    except (FileNotFoundError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
