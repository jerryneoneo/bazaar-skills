#!/usr/bin/env python3
"""followup_state.py — detect chats that have gone quiet and schedule gentle follow-ups.

When WE sent the last message in a thread and the counterpart never replied, the agent should
nudge them up to `followup_max_nudges` times on a gentle-escalation cadence, then give up and mark
them "not interested". This module is the deterministic DETECT + SCHEDULE core; the actual COMPOSE +
SEND of a nudge is done by the existing buyer/buy LLM pass (the same `journal_send` bracket every
reply uses). Marking "not interested" needs no LLM, so it is done here ($0 deterministic).

Design (mirrors eval_state.py / scan_state.py): pure functions unit-tested with `--now`, a thin CLI,
`atomic_io` writes, `scan_state.parse_iso` reused. The nudge COUNT is always DERIVED from the
transcript tail (the run of consecutive trailing outbound rows after the last inbound) — never
trusted from a stored counter, which would desync on any crash between send and ledger write. The
ledger (`data/followup_state.json`) is only a cache of that derived count plus the terminal
`disposition`; the transcript stays the source of truth for "did they reply".

    config.json -> followup_enabled / followup_nudge_intervals_days / followup_drop_after_days /
                   followup_max_nudges
    data/threads/<id>.json        (sell side) + data/buyer_threads/<id>.json (buy side)
    data/followup_state.json      -> per-thread {disposition, derived-count cache}

Usage:
    followup_state.py due        [--now ISO]
    followup_state.py mark-nudge --thread <id> --side sell|buy [--now ISO]
    followup_state.py mark-drop  --thread <id> --side sell|buy [--now ISO]
    followup_state.py drops      [--now ISO]   # deterministic: mark not_interested + notify the user
    followup_state.py reconcile  [--now ISO]   # prune entries for answered/gone threads

Exit codes: 0 ok · 2 bad input · 3 config/data missing or invalid. Never reads a secret.
Data dir relocatable via BAZAAR_DATA_DIR (tests + isolation), matching the rest of bin/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  crash-safe (tmp + os.replace) JSON writes + cross-process lock
import channel_outbox  # noqa: E402  single-writer user-notice queue (for the "went cold" notice)
from scan_state import parse_iso  # noqa: E402  the one tz-safe ISO parser

DEFAULT_NUDGE_INTERVALS_DAYS = (1.0, 3.0)  # gap before nudge #1, gap before nudge #2 (gentle escalation)
DEFAULT_DROP_AFTER_DAYS = 3.0              # gap after the last nudge before marking not_interested
DEFAULT_MAX_NUDGES = 2                     # product decision: chase up to 2 more times
DEFAULT_ENABLED = True                     # the user asked for this; pacing caps bound the blast radius

# Statuses that make a thread INELIGIBLE for a follow-up: terminal, or already awaiting the USER.
# Mirrors triage.SKIP_UNREAD_STATUSES, plus `agreed` (a deal in motion must never get "you still
# there?") and the buy-side terminals.
SELL_TERMINAL = frozenset({"lost", "handover", "closed", "escalated", "held", "agreed"})
BUY_TERMINAL = frozenset({"closed", "escalated", "agreed", "held"})

NOTE_PREFIX = "followup#"  # outbound nudges are committed with note="followup#<n>: ..." (audit only)


def data_dir() -> Path:
    """The data dir - relocatable via BAZAAR_DATA_DIR (used by tests for isolation)."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data"


def _safe_iso(value):
    """parse_iso, but a garbage transcript ts returns None instead of raising. Transcript rows are
    less trusted than system-written cursors, so an un-parseable one must fail closed, not crash."""
    try:
        return parse_iso(value)
    except (ValueError, TypeError):
        return None


# ---- pure functions (no IO) — directly unit-tested -------------------------

def trailing_outbound(thread: dict) -> list[dict]:
    """The run of consecutive dir=='out' rows at the END of the transcript (after the last inbound).

    [] when the last row is inbound or the transcript is empty. This run IS the schedule driver:
    its length is (our reply + nudges already sent) and its last element's ts is the anchor whose age
    decides the next action. The inverse of triage.last_unhandled_inbound's cursor walk.
    """
    transcript = thread.get("transcript") or []
    run: list[dict] = []
    for msg in reversed(transcript):
        if msg.get("dir") == "out":
            run.append(msg)
        else:
            break
    run.reverse()
    return run


def awaiting_counterpart(thread: dict, terminal: frozenset) -> bool:
    """True when the counterpart owes us a reply: status not terminal AND the tail is outbound.
    A trailing INBOUND means it is the normal pass's job (they replied), not a follow-up candidate."""
    if thread.get("status", "active") in terminal:
        return False
    return bool(trailing_outbound(thread))


def schedule_for(run_len: int, intervals_days, drop_after_days: float,
                 max_nudges: int) -> tuple[str, float] | None:
    """Pure schedule lookup keyed on the trailing-outbound run length.

    With max_nudges=2, intervals=[1,3], drop=3:
       run_len <= 0              -> None            (their turn)
       run_len == 1             -> ('nudge', 1.0)  (our reply unanswered -> nudge #1 due 1d later)
       run_len == 2             -> ('nudge', 3.0)  (1 nudge sent          -> nudge #2 due 3d later)
       run_len == 3             -> ('drop',  3.0)  (2 nudges sent         -> give up 3d later)
       run_len >= 4             -> None            (past the drop horizon)
    The interval index is clamped to the list length so a config with fewer intervals than
    max_nudges never IndexErrors.
    """
    if run_len <= 0 or max_nudges <= 0 or not intervals_days:
        return None
    if run_len <= max_nudges:
        idx = min(run_len - 1, len(intervals_days) - 1)
        return ("nudge", float(intervals_days[idx]))
    if run_len == max_nudges + 1:
        return ("drop", float(drop_after_days))
    return None


def due_decision(thread: dict, side: str, terminal: frozenset, intervals_days,
                 drop_after_days: float, max_nudges: int, now: datetime) -> dict | None:
    """One follow-up decision for one thread, or None when nothing is due.

    Fail-closed: an un-parseable anchor ts yields None (never act on a thread we cannot place in
    time). The returned dict is plumbing only — no secret, no transcript body.
    """
    if not awaiting_counterpart(thread, terminal):
        return None
    run = trailing_outbound(thread)
    sched = schedule_for(len(run), intervals_days, drop_after_days, max_nudges)
    if sched is None:
        return None
    action, gap_days = sched
    anchor = _safe_iso(run[-1].get("ts"))
    if anchor is None:
        return None
    age_days = (now - anchor).total_seconds() / 86400.0
    if age_days < gap_days:
        return None
    return {
        "thread_id": thread.get("thread_id"),
        "side": side,
        "marketplace": thread.get("marketplace"),
        "handle": thread.get("buyer_handle") or thread.get("seller_handle"),
        "id_value": thread.get("item_id") or thread.get("want_id"),
        "action": action,
        "nudges_sent": max(0, len(run) - 1),
        "anchor_ts": run[-1].get("ts"),
        "age_days": round(age_days, 3),
        "due_at": (anchor + timedelta(days=gap_days)).isoformat(),
    }


def reconcile_ledger(ledger: dict, still_awaiting_ids: set) -> dict:
    """NEW ledger keeping only entries whose thread still awaits the counterpart (tail outbound,
    not terminal). Drops entries for threads that no longer exist OR were answered/closed since.
    Pure; never mutates input. This is the 'reset when they reply' rule, driven off the transcript."""
    return {tid: dict(entry) for tid, entry in (ledger or {}).items() if tid in still_awaiting_ids}


def drop_notice_text(drops: list[dict]) -> str:
    """One batched, voice-safe (no em-dashes) summary of the chats we are giving up on."""
    parts = []
    for d in drops:
        who = d.get("handle") or d.get("thread_id")
        what = d.get("id_value")
        parts.append(f"{who} ({what})" if what else f"{who}")
    n = len(drops)
    noun = "chat" if n == 1 else "chats"
    return (f"{n} {noun} went quiet after my follow-ups, so I've marked them not interested: "
            f"{', '.join(parts)}. They'll still get answered if they message back.")


# ---- config parsing (tolerant; mirrors eval_state._interval_from_config) ----

def _enabled_from_config(config: dict) -> bool:
    return bool(config.get("followup_enabled", DEFAULT_ENABLED))


def _intervals_from_config(config: dict) -> tuple:
    raw = config.get("followup_nudge_intervals_days", list(DEFAULT_NUDGE_INTERVALS_DAYS))
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"followup_nudge_intervals_days must be a non-empty list, got {raw!r}")
    try:
        return tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        raise ValueError(f"followup_nudge_intervals_days must be numbers, got {raw!r}")


def _drop_after_from_config(config: dict) -> float:
    raw = config.get("followup_drop_after_days", DEFAULT_DROP_AFTER_DAYS)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"followup_drop_after_days must be a number, got {raw!r}")


def _max_nudges_from_config(config: dict) -> int:
    raw = config.get("followup_max_nudges", DEFAULT_MAX_NUDGES)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"followup_max_nudges must be an integer, got {raw!r}")


# ---- fail-open loaders (read-only; a broken file is skipped, never raised) ----

def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_thread_dir(path: Path, side: str) -> list[tuple]:
    """Every well-formed *.json thread in a dir as (thread_id, side, dict). Skips TEST fixtures."""
    out: list[tuple] = []
    try:
        names = sorted(p.name for p in path.iterdir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or "TEST" in name:
            continue
        thread = _load_json(path / name)
        if thread:
            out.append((thread.get("thread_id") or name[:-5], side, thread))
    return out


def _all_threads(base: Path) -> list[tuple]:
    return (_load_thread_dir(base / "threads", "sell")
            + _load_thread_dir(base / "buyer_threads", "buy"))


def _load_thread(base: Path, thread_id: str, side: str) -> dict | None:
    dirname = "threads" if side == "sell" else "buyer_threads"
    direct = base / dirname / f"{thread_id}.json"
    if direct.exists():
        thread = _load_json(direct)
        if thread:
            return thread
    for tid, _, thread in _load_thread_dir(base / dirname, side):
        if tid == thread_id:
            return thread
    return None


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _open_escalation_thread_ids(base: Path) -> set:
    """thread_ids with an OPEN escalation (either side) — excluded so we never nudge a counterpart
    while we are actually waiting on the USER's decision."""
    out: set = set()
    for name in ("escalations.jsonl", "buyer_escalations.jsonl"):
        for row in _load_jsonl(base / name):
            if row.get("status") == "open" and row.get("thread_id"):
                out.add(row["thread_id"])
    return out


def _ledger_path(base: Path) -> Path:
    return base / "followup_state.json"


# ---- orchestrators + IO (thin) ----------------------------------------------

def scan_due(base: Path, now: datetime) -> dict:
    """Partition every eligible thread into due nudges and due drops. Read-only.

    followup_enabled false -> empty result. Excludes terminal/awaiting-user threads, threads with an
    open escalation, and threads already dispositioned not_interested (drops still surface until the
    user has been notified, so the notice is never lost)."""
    config = _load_json(base / "config.json")
    if not _enabled_from_config(config):
        return {"enabled": False, "now": now.isoformat(), "due_nudges": [], "due_drops": [],
                "counts": {"nudges": 0, "drops": 0, "candidates": 0}}
    intervals = _intervals_from_config(config)
    drop_after = _drop_after_from_config(config)
    max_nudges = _max_nudges_from_config(config)
    ledger = _load_json(_ledger_path(base))
    excluded = _open_escalation_thread_ids(base)

    due_nudges: list[dict] = []
    due_drops: list[dict] = []
    candidates = 0
    for thread_id, side, thread in _all_threads(base):
        terminal = SELL_TERMINAL if side == "sell" else BUY_TERMINAL
        if not awaiting_counterpart(thread, terminal):
            continue
        candidates += 1
        if thread_id in excluded:
            continue
        decision = due_decision(thread, side, terminal, intervals, drop_after, max_nudges, now)
        if decision is None:
            continue
        entry = ledger.get(thread_id) or {}
        disposition = entry.get("disposition", "active")
        if decision["action"] == "nudge":
            if disposition == "not_interested":
                continue
            due_nudges.append(decision)
        else:  # drop
            if disposition == "not_interested" and entry.get("dropped_notified"):
                continue
            due_drops.append(decision)
    return {"enabled": True, "now": now.isoformat(), "due_nudges": due_nudges, "due_drops": due_drops,
            "counts": {"nudges": len(due_nudges), "drops": len(due_drops), "candidates": candidates}}


def run_due(now: datetime, base: Path | None = None) -> dict:
    return scan_due(base or data_dir(), now)


def run_mark_nudge(thread_id: str, side: str, now: datetime, base: Path | None = None) -> dict:
    """Refresh the ledger cache AFTER a nudge has been sent + committed. The count is re-DERIVED from
    the now-updated transcript, never blindly incremented, so a double call is idempotent."""
    base = base or data_dir()
    config = _load_json(base / "config.json")
    intervals = _intervals_from_config(config)
    drop_after = _drop_after_from_config(config)
    max_nudges = _max_nudges_from_config(config)
    thread = _load_thread(base, thread_id, side) or {}
    run = trailing_outbound(thread)
    anchor_ts = run[-1].get("ts") if run else now.isoformat()
    next_due_at = None
    nxt = schedule_for(len(run), intervals, drop_after, max_nudges)
    if nxt is not None:
        anchor = _safe_iso(anchor_ts)
        if anchor is not None:
            next_due_at = (anchor + timedelta(days=nxt[1])).isoformat()
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        prev = ledger.get(thread_id) or {}
        entry = {
            "side": side,
            "marketplace": thread.get("marketplace") or prev.get("marketplace"),
            "followup_count": max(0, len(run) - 1),
            "last_followup_at": anchor_ts,
            "next_due_at": next_due_at,
            "disposition": prev.get("disposition", "active"),
            "dropped_notified": prev.get("dropped_notified", False),
            "updated_at": now.isoformat(),
        }
        atomic_io.write_json(path, {**ledger, thread_id: entry})
    return entry


def run_mark_drop(thread_id: str, side: str, now: datetime, base: Path | None = None) -> dict:
    """Set disposition=not_interested (atomic, locked). Idempotent; never touches thread status."""
    base = base or data_dir()
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        prev = ledger.get(thread_id) or {}
        entry = {
            **prev,
            "side": side,
            "disposition": "not_interested",
            "dropped_notified": prev.get("dropped_notified", False),
            "followup_count": prev.get("followup_count", 0),
            "updated_at": now.isoformat(),
        }
        atomic_io.write_json(path, {**ledger, thread_id: entry})
    return entry


def _mark_notified(thread_ids: list[str], now: datetime, base: Path) -> None:
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        updated = dict(ledger)
        for tid in thread_ids:
            if tid in updated:
                updated[tid] = {**updated[tid], "dropped_notified": True, "updated_at": now.isoformat()}
        atomic_io.write_json(path, updated)


def run_drops(now: datetime, base: Path | None = None) -> dict:
    """Deterministic ($0, no LLM): mark every due-to-drop thread not_interested and enqueue ONE
    batched 'went cold' notice to the user. The notice goes to channel_outbox (informational), not
    the escalation ledgers (those await a user decision)."""
    base = base or data_dir()
    result = scan_due(base, now)
    drops = result.get("due_drops", [])
    if not drops:
        return {"dropped": 0, "notified": 0}
    for d in drops:
        run_mark_drop(d["thread_id"], d["side"], now, base=base)
    ledger = _load_json(_ledger_path(base))
    to_notify = [d for d in drops if not (ledger.get(d["thread_id"]) or {}).get("dropped_notified")]
    if to_notify:
        channel_outbox.run_enqueue("notify", drop_notice_text(to_notify), now,
                                   base / "channel_outbox.jsonl", source="followup")
        _mark_notified([d["thread_id"] for d in to_notify], now, base)
    return {"dropped": len(drops), "notified": len(to_notify)}


def run_reconcile(now: datetime, base: Path | None = None) -> dict:
    """Prune ledger entries for threads that no longer exist or have been answered/closed."""
    base = base or data_dir()
    still: set = set()
    for thread_id, side, thread in _all_threads(base):
        terminal = SELL_TERMINAL if side == "sell" else BUY_TERMINAL
        if awaiting_counterpart(thread, terminal):
            still.add(thread_id)
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        updated = reconcile_ledger(ledger, still)
        dropped = len(ledger) - len(updated)
        if updated != ledger:
            atomic_io.write_json(path, updated)
    return {"kept": len(updated), "dropped": dropped}


# ---- CLI --------------------------------------------------------------------

def _resolve_now(now_arg: str) -> datetime:
    if now_arg:
        parsed = parse_iso(now_arg)
        if parsed is None:
            raise ValueError(f"could not parse --now {now_arg!r}")
        return parsed
    return datetime.now().astimezone()


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="followup_state.py", add_help=False)
    parser.add_argument("command", choices=["due", "mark-nudge", "mark-drop", "drops", "reconcile"])
    parser.add_argument("--thread", default="")
    parser.add_argument("--side", default="", choices=["", "sell", "buy"])
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def _validate(ns) -> None:
    if ns.command in ("mark-nudge", "mark-drop"):
        if not ns.thread.strip():
            raise ValueError(f"{ns.command} requires --thread <id>")
        if ns.side not in ("sell", "buy"):
            raise ValueError(f"{ns.command} requires --side sell|buy")


def main(argv) -> int:
    try:
        ns = _parse_args(argv)
        _validate(ns)
        now = _resolve_now(ns.now)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        if ns.command == "due":
            result = run_due(now)
        elif ns.command == "mark-nudge":
            result = run_mark_nudge(ns.thread.strip(), ns.side, now)
        elif ns.command == "mark-drop":
            result = run_mark_drop(ns.thread.strip(), ns.side, now)
        elif ns.command == "drops":
            result = run_drops(now)
        else:
            result = run_reconcile(now)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
