#!/usr/bin/env python3
"""lease.py — per-resource leases (generalizes the single global .daemon.runlock).

The Phase-3 supervisor (agent_daemon.py) can run several workers at once, each scoped to a disjoint
RESOURCE — `market:<id>` (one per marketplace login → the conservative same-account guard),
`channel` (the singleton control-channel writer), `listing-wizard`. A worker may run for a resource
only while it holds that resource's lease, so two workers never drive the same account/tab.

Liveness is by HOLDER TOKEN + HEARTBEAT TTL, not OS pid: the supervisor assigns each worker a token,
heartbeats the lease while the worker runs, and releases it on exit. If the supervisor (and thus the
heartbeats) dies, the lease goes stale within `ttl_sec` and the next acquirer reclaims it — so a hard
crash never strands a resource. pid-based liveness is deliberately avoided (pid reuse is unreliable).

Acquire is an atomic check-and-write under fcntl.flock (same discipline as pacing_gate.py): the first
holder wins; a racing acquirer sees a live, fresh holder and is refused.

Usage:
    lease.py acquire  --resource <r> --holder <token> [--mode <m>] [--ttl <s>] [--now <iso>]
    lease.py heartbeat --resource <r> --holder <token> [--now <iso>]
    lease.py release  --resource <r> --holder <token> [--force] [--now <iso>]
    lease.py status   [--resource <r>] [--now <iso>]

Output (stdout, JSON). Exit codes: 0 ok · 2 bad input · 3 data error.
"""

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_TTL_SEC = 120

# AGENT_MARKET_TTL_SEC: the canonical liveness window for a `market:<id>` lease held by a supervisor
# worker. The supervisor (bin/supervisor.py) acquires + heartbeats each market lease with this TTL,
# and journal_reconcile's live-lease guard MUST read liveness with the SAME window — otherwise a
# worker whose heartbeat is 120-600s old reads LIVE to the supervisor but stale to the guard, and
# reconcile folds the worker's still-in-flight intent (the Olaf in-flight-steal). Keep this the single
# source of truth: supervisor.py's LEASE_TTL_SEC SHOULD be switched to import this constant so the two
# can never drift (a follow-up — supervisor.py is owned by a parallel round and is not edited here).
AGENT_MARKET_TTL_SEC = 600


def data_dir():
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


def leases_dir(base=None):
    return (base or data_dir()) / "leases"


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def parse_iso(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _safe_name(resource):
    """Map a resource id to a filesystem-safe filename; the original is also stored in the record."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", resource)


def is_stale(record, now, ttl):
    """A held lease whose last heartbeat is older than ttl is stale (its holder is presumed dead)."""
    if not record or not record.get("holder"):
        return False
    beat = parse_iso(record.get("heartbeat_at"))
    if beat is None:
        return True
    return (now - beat).total_seconds() > ttl


def is_free(record, now, ttl):
    """Free = no holder, or the holder's lease has gone stale."""
    if not record or not record.get("holder"):
        return True
    return is_stale(record, now, ttl)


# ---------------------------------------------------------------------------
# IO (flock-atomic)
# ---------------------------------------------------------------------------
def _lease_path(base, resource):
    return leases_dir(base) / f"{_safe_name(resource)}.json"


def _read(path):
    if not path.exists():
        return None
    text = path.read_text().strip()
    return json.loads(text) if text else None


def _write(path, record):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, path)


def _with_lock(path, fn):
    """Run fn() while holding an exclusive flock on the resource's lock file (serializes racers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def acquire(base, resource, holder, mode="", ttl=DEFAULT_TTL_SEC, now=None):
    now = now or datetime.now(timezone.utc)
    path = _lease_path(base, resource)

    def _do():
        record = _read(path)
        if not is_free(record, now, ttl):
            return {"acquired": False, "resource": resource, "holder": record.get("holder"),
                    "mode": record.get("mode"), "heartbeat_at": record.get("heartbeat_at")}
        reclaimed = bool(record and record.get("holder"))  # took over a stale holder
        new = {"holder": holder, "mode": mode, "acquired_at": now.isoformat(),
               "heartbeat_at": now.isoformat(), "ttl_sec": ttl, "resource": resource}
        _write(path, new)
        return {"acquired": True, "resource": resource, "holder": holder, "mode": mode,
                "acquired_at": new["acquired_at"], "stale_reclaimed": reclaimed}

    return _with_lock(path, _do)


def heartbeat(base, resource, holder, now=None):
    now = now or datetime.now(timezone.utc)
    path = _lease_path(base, resource)

    def _do():
        record = _read(path)
        if not record or record.get("holder") != holder:
            return {"ok": False, "resource": resource, "holder": record.get("holder") if record else None}
        updated = {**record, "heartbeat_at": now.isoformat()}
        _write(path, updated)
        return {"ok": True, "resource": resource, "holder": holder, "heartbeat_at": updated["heartbeat_at"]}

    return _with_lock(path, _do)


def release(base, resource, holder, force=False, now=None):
    now = now or datetime.now(timezone.utc)
    path = _lease_path(base, resource)

    def _do():
        record = _read(path)
        if not record or not record.get("holder"):
            return {"released": False, "resource": resource, "reason": "not held"}
        if record.get("holder") != holder and not force:
            return {"released": False, "resource": resource, "reason": "not holder"}
        _write(path, {"holder": None, "released_at": now.isoformat(), "resource": resource})
        return {"released": True, "resource": resource}

    return _with_lock(path, _do)


def status(base, resource=None, ttl=DEFAULT_TTL_SEC, now=None):
    now = now or datetime.now(timezone.utc)
    if resource:
        record = _read(_lease_path(base, resource))
        held = bool(record and record.get("holder")) and not is_stale(record, now, ttl)
        return {"resource": resource, "held": held, "stale": is_stale(record, now, ttl),
                "holder": record.get("holder") if record else None,
                "heartbeat_at": record.get("heartbeat_at") if record else None}
    out = {}
    d = leases_dir(base)
    if d.exists():
        for path in sorted(d.glob("*.json")):
            record = _read(path)
            if not record:
                continue
            res = record.get("resource", path.stem)
            out[res] = {"held": bool(record.get("holder")) and not is_stale(record, now, ttl),
                        "stale": is_stale(record, now, ttl), "holder": record.get("holder")}
    return {"leases": out, "now": now.isoformat()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_now(now_arg):
    if not now_arg:
        return datetime.now(timezone.utc)
    parsed = parse_iso(now_arg)
    if parsed is None:
        raise ValueError(f"could not parse --now {now_arg!r}")
    return parsed


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="lease.py", add_help=False)
    p.add_argument("command", choices=["acquire", "heartbeat", "release", "status"])
    p.add_argument("--resource", default="")
    p.add_argument("--holder", default="")
    p.add_argument("--mode", default="")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL_SEC)
    p.add_argument("--force", action="store_true")
    p.add_argument("--now", default="")
    return p.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse_args(argv)
        now = _resolve_now(ns.now)
        resource = ns.resource.strip()
        if ns.command != "status" and not resource:
            raise ValueError(f"{ns.command} requires --resource <id>")
        if ns.command in ("acquire", "heartbeat", "release") and not ns.holder.strip() and not ns.force:
            raise ValueError(f"{ns.command} requires --holder <token>")
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        base = data_dir()
        holder = ns.holder.strip()
        if ns.command == "acquire":
            result = acquire(base, resource, holder, ns.mode.strip(), ns.ttl, now)
        elif ns.command == "heartbeat":
            result = heartbeat(base, resource, holder, now)
        elif ns.command == "release":
            result = release(base, resource, holder, ns.force, now)
        else:
            result = status(base, resource or None, ns.ttl, now)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
