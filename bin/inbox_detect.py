#!/usr/bin/env python3
"""inbox_detect.py — deterministic core for the inbox-sweep takeover.

The agent only ever acts on conversations it already tracks. This engine answers the
three deterministic questions the inbox SWEEP needs so the loop can offer to take over
the chats a user started on their OWN (a few sellers messaged about an iPhone; a buyer
who messaged a listing the user never imported):

  • classify — which SIDE is a thread? The FIRST message's direction decides:
        first msg dir "out" (sent by the user) → buyer_initiated  (a purchase task)
        first msg dir "in"  (sent to the user) → seller_initiated (a listing to manage)
  • diff     — which inbox rows are UNTRACKED (not already managed and not declined)?
  • declined — a thread-keyed seen-set so a thread the user said "leave it" to is NEVER
        re-offered (mirror of distribution's "never nag twice").

Plus a `due`/`mark` cadence that REUSES scan_state's logic over the UNION of the seller's
and buyer's enabled markets (the inbox is one shared surface, swept in the same cadence
slot as the my-listings SCAN, cursor: data/scan_state.json).

It NEVER reads or emits a floor, a max budget, or an address — no secrets in, no secrets
out. It reads only buyer-safe state (config, seller/buyer configs, threads, wants).

Usage:
    inbox_detect.py classify  --thread <file|->                # transcript JSON in -> {"direction"}
    inbox_detect.py diff       --market <id> --rows <file|->    # inbox rows in -> {"untracked":[...]}
    inbox_detect.py decline    --thread <market>:<id> [--side buy|sell] [--now <iso>]
    inbox_detect.py declined   --thread <market>:<id>           # -> {"declined": bool}
    inbox_detect.py due        [--now <iso>]                    # which inbox market is overdue (union)
    inbox_detect.py mark       --market <id> [--now <iso>]      # stamp it scanned (shared cursor)

Exit codes: 0 ok · 2 bad input · 3 config/data missing or invalid.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_state  # reuse the cadence core (due_market / mark_scanned / parse_iso / enabled_markets)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"
SELLER_CONFIG_PATH = DATA_DIR / "seller_config.json"
BUYER_CONFIG_PATH = DATA_DIR / "buyer_config.json"
SCAN_STATE_PATH = DATA_DIR / "scan_state.json"
TAKEOVER_SEEN_PATH = DATA_DIR / "takeover_seen.json"

THREAD_DIRS = ("threads", "buyer_threads")


# --------------------------------------------------------------------------- pure core

def classify(transcript):
    """Direction of a thread from its ordered transcript [{msg_id, dir, text, ts}].

    The FIRST in/out message decides: "out" (the user spoke first) is buyer-initiated;
    "in" (someone messaged the user) is seller-initiated. No messages -> "empty".
    """
    msgs = [m for m in (transcript or [])
            if isinstance(m, dict) and m.get("dir") in ("in", "out")]
    if not msgs:
        return "empty"
    return "buyer_initiated" if msgs[0]["dir"] == "out" else "seller_initiated"


def untracked_rows(market, rows, tracked_ids, declined_ids):
    """Pure diff: inbox rows whose namespaced id is neither tracked nor declined.

    Each returned row is a COPY with a `tid` ("<market>:<thread_id>") added. Never
    mutates the input rows. The unread gate is the skill's job (rows carry `unread`).
    """
    tracked = set(tracked_ids or ())
    declined = set(declined_ids or ())
    out = []
    for row in rows or []:
        rid = str((row or {}).get("thread_id", "")).strip()
        if not rid:
            continue
        tid = f"{market}:{rid}"
        if tid in tracked or tid in declined:
            continue
        out.append({**row, "tid": tid})
    return out


def union_enabled(seller_marketplaces, buyer_marketplaces):
    """Ordered union of enabled market ids across both side configs (seller first)."""
    seen = []
    for marketplaces in (seller_marketplaces, buyer_marketplaces):
        for mid in scan_state.enabled_markets(marketplaces or {}):
            if mid not in seen:
                seen.append(mid)
    return seen


def declined_set(seen):
    """The set of thread ids we've already decided on (declined OR managed) — all suppress."""
    return {tid for tid in (seen or {}) if isinstance((seen or {}).get(tid), dict)}


def with_decision(seen, tid, decision, side, now, extra=None):
    """Return a NEW seen-map with `tid` recorded — never mutates the input."""
    updated = {k: dict(v) for k, v in (seen or {}).items() if isinstance(v, dict)}
    entry = {"decision": decision, "side": side, "ts": now}
    if extra:
        entry.update(extra)
    updated[tid] = entry
    return updated


# --------------------------------------------------------------------------- file helpers

def _load_json(path, label):
    if not path.exists():
        raise FileNotFoundError(f"{label} not found at {path}")
    return json.loads(path.read_text())


def _load_json_optional(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except ValueError:
        return default


def _read_payload(source):
    """Read JSON from a file path or '-' (stdin)."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text()
    return json.loads(text)


def tracked_thread_ids():
    """All currently-managed thread ids: sell threads, buyer threads, and want thread_ids."""
    ids = set()
    for sub in THREAD_DIRS:
        d = DATA_DIR / sub
        if d.exists():
            for f in d.glob("*.json"):
                ids.add(f.stem)
    wants = DATA_DIR / "wants"
    if wants.exists():
        for f in wants.glob("*.json"):
            try:
                want = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            for tid in (want.get("thread_ids") or []):
                ids.add(tid)
    return ids


def load_seen():
    data = _load_json_optional(TAKEOVER_SEEN_PATH, {})
    return data if isinstance(data, dict) else {}


def save_seen(seen):
    TAKEOVER_SEEN_PATH.write_text(json.dumps(seen, indent=2) + "\n")


# --------------------------------------------------------------------------- CLI runners

def run_classify(thread_arg):
    transcript = _read_payload(thread_arg)
    if isinstance(transcript, dict):           # accept {"transcript": [...]} or a bare list
        transcript = transcript.get("transcript", [])
    return {"direction": classify(transcript)}


def run_diff(market, rows_arg):
    if not market:
        raise ValueError("diff requires --market <id>")
    rows = _read_payload(rows_arg)
    if isinstance(rows, dict):
        rows = rows.get("rows", [])
    untracked = untracked_rows(market, rows, tracked_thread_ids(), declined_set(load_seen()))
    return {"market": market, "untracked": untracked, "count": len(untracked)}


def run_decline(tid, side, now):
    if not tid:
        raise ValueError("decline requires --thread <market>:<id>")
    updated = with_decision(load_seen(), tid, "declined", side or "unknown", now.isoformat())
    save_seen(updated)
    return {"declined": tid, "side": side or "unknown"}


def run_declined(tid):
    if not tid:
        raise ValueError("declined requires --thread <market>:<id>")
    return {"thread": tid, "declined": tid in declined_set(load_seen())}


def run_due(now):
    config = _load_json(CONFIG_PATH, "config.json")
    interval = scan_state._interval_from_config(config)
    seller = _load_json_optional(SELLER_CONFIG_PATH, {})
    buyer = _load_json_optional(BUYER_CONFIG_PATH, {})
    union = union_enabled(seller.get("marketplaces"), buyer.get("marketplaces"))
    if not union:
        raise ValueError("no enabled markets on either side (seller_config/buyer_config)")
    state = _load_json_optional(SCAN_STATE_PATH, {})
    market, info = scan_state.due_market(union, state, interval, now)
    return {"due_market": market, "interval_hours": interval, "now": now.isoformat(),
            "markets": info}


def run_mark(market, now):
    if not market:
        raise ValueError("mark requires --market <id>")
    # Shared cursor with the my-listings SCAN — stamp the same data/scan_state.json.
    return {"marked": market, "scan_state": scan_state.run_mark(market, now)}


# --------------------------------------------------------------------------- arg parsing

def _parse_args(argv):
    p = argparse.ArgumentParser(prog="inbox_detect.py", add_help=False)
    p.add_argument("command", choices=["classify", "diff", "decline", "declined", "due", "mark"])
    p.add_argument("--thread", default="")
    p.add_argument("--market", default="")
    p.add_argument("--rows", default="")
    p.add_argument("--side", default="")
    p.add_argument("--now", default="")
    return p.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse_args(argv)
        now = scan_state._resolve_now(ns.now)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        if ns.command == "classify":
            result = run_classify(ns.thread or "-")
        elif ns.command == "diff":
            result = run_diff(ns.market.strip(), ns.rows or "-")
        elif ns.command == "decline":
            result = run_decline(ns.thread.strip(), ns.side.strip(), now)
        elif ns.command == "declined":
            result = run_declined(ns.thread.strip())
        elif ns.command == "due":
            result = run_due(now)
        else:
            result = run_mark(ns.market.strip(), now)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
