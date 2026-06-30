#!/usr/bin/env python3
"""listing_health.py — detect LIVE listings that have gone quiet so the agent can suggest fixes.

A live listing is "stale" when no buyer has shown interest (no inbound message on any of its
threads) for `stale_days` (default 7). For a listing that NEVER got an inbound, the clock starts at
a published "anchor" derived from a fallback chain (current items carry no `published_at`, and we do
NOT migrate, so the fallback chain is the real engine).

This module is the deterministic DETECT core; the expensive comps research + suggestion COMPOSITION
is done by the MAINT LLM pass, ONE item per pass, via a session baton
(`data/listing_health_session.json`) - the same idiom as distribution_session / inbox_detect_session.
A small ledger (`data/listing_health_state.json`) dedups the proactive ping so the user is warned
once per stale episode, not nagged every pass.

Mirrors eval_state.py / scan_state.py: pure functions unit-tested with `--now`, a thin CLI, atomic
writes, `scan_state.parse_iso` reused. Eligibility is `status=="live"` AND a non-empty `listing_urls`
so a draft / live-but-unpublished item stays with triage._listing_rows' existing signal (no double
count).

    config.json -> listing_health_enabled / stale_days / listing_health_interval_hours / rewarn_days
    data/items/<id>.json      (the listing) + data/threads/<id>.json (buyer interest, by item_id)
    data/listing_health_state.json   -> per-item warn ledger + last_picked_at (episode rate-limit)
    data/listing_health_session.json -> the one-item baton the MAINT pass continues

Usage:
    listing_health.py due   [--now ISO]   -> {"due_item","row","stale_count","interval_hours","enabled"}
    listing_health.py list  [--now ISO]   -> {"stale":[rows],"count":N}     (for triage / /status)
    listing_health.py start --item <id> [--now ISO]   -> write the session baton + stamp last_picked_at
    listing_health.py mark  --item <id> [--now ISO]   -> stamp the warn ledger (after suggestions sent)
    listing_health.py reset --item <id>               -> drop a ledger entry (re-engaged / manual)

Exit codes: 0 ok · 2 bad input · 3 config/data missing or invalid. Never reads a secret.
Data dir relocatable via SELLY_DATA_DIR; items dir via SELLY_ITEMS_DIR (match delist_item.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  crash-safe writes + cross-process lock
from scan_state import parse_iso  # noqa: E402  the one tz-safe ISO parser

DEFAULT_STALE_DAYS = 7.0
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_REWARN_DAYS = 14.0
DEFAULT_ENABLED = True

LIVE_STATUS = "live"


def data_dir() -> Path:
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data"


def _items_dir(base: Path) -> Path:
    override = os.environ.get("SELLY_ITEMS_DIR")
    return Path(override) if override else base / "items"


def _safe_iso(value):
    """parse_iso, but unparseable input returns None instead of raising (transcript/item ts are
    less trusted than system cursors)."""
    try:
        return parse_iso(value)
    except (ValueError, TypeError):
        return None


# ---- pure helpers (no IO) — directly unit-tested ---------------------------

def last_inbound_ts(thread: dict):
    """Most recent dir=='in' ts across the WHOLE transcript, ignoring the cursor (cursor = 'have I
    replied', irrelevant to 'did a buyer ever show interest'). Aware datetime, or None."""
    latest = None
    for msg in thread.get("transcript") or []:
        if msg.get("dir") != "in":
            continue
        ts = _safe_iso(msg.get("ts"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def item_inbound_ts(item_id: str, threads: list[dict]):
    """The most recent buyer inbound across ALL of an item's threads (multi-buyer), or None."""
    latest = None
    for t in threads:
        if t.get("item_id") != item_id:
            continue
        ts = last_inbound_ts(t)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def published_anchor(item: dict, item_path: Path | None, now: datetime) -> datetime:
    """The 'stale clock starts here' anchor for a listing that NEVER had an inbound. Fallback chain,
    first that parses: published_at -> imported_at -> distribution_offered_at -> item-file mtime ->
    now (last resort -> treated as just-published -> NOT stale, the fail-safe)."""
    for key in ("published_at", "imported_at", "distribution_offered_at"):
        ts = _safe_iso(item.get(key))
        if ts is not None:
            return ts
    if item_path is not None:
        try:
            return datetime.fromtimestamp(item_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            pass
    return now


def staleness(item: dict, item_inbound, anchor: datetime, stale_days: float, now: datetime) -> dict:
    """Pure staleness decision for ONE live, published item. The clock is the LAST INBOUND when one
    exists; the anchor is only used when a buyer never wrote. stale_days <= 0 -> never stale."""
    clock = item_inbound if item_inbound is not None else anchor
    if clock is None:
        clock = now
    silent_days = (now - clock).total_seconds() / 86400.0
    return {
        "item_id": item.get("item_id"),
        "title": item.get("title", ""),
        "stale": stale_days > 0 and silent_days >= stale_days,
        "silent_days": round(silent_days, 3),
        "basis": "since_inbound" if item_inbound is not None else "no_inbound",
        "last_inbound_ts": item_inbound.isoformat() if item_inbound is not None else None,
        "list_price": item.get("list_price"),
        "currency": item.get("currency"),
        "photo_count": len(item.get("photos") or []),
    }


def is_eligible(item: dict) -> bool:
    """Stale detection runs only on a published live listing. A draft / live-but-unpublished item is
    left to triage._listing_rows (draft + undistributed), so the two signals never double-count."""
    if item.get("status") != LIVE_STATUS:
        return False
    urls = item.get("listing_urls")
    return isinstance(urls, dict) and bool(urls)


def stale_listings(items: list[dict], threads: list[dict], item_paths: dict | None,
                   stale_days: float, now: datetime) -> list[dict]:
    """Every eligible item that is stale, most-overdue first (deterministic pick order)."""
    paths = item_paths or {}
    out: list[dict] = []
    for item in items:
        if not is_eligible(item):
            continue
        iid = item.get("item_id")
        inbound = item_inbound_ts(iid, threads)
        anchor = published_anchor(item, paths.get(iid), now)
        row = staleness(item, inbound, anchor, stale_days, now)
        if row["stale"]:
            out.append(row)
    out.sort(key=lambda r: r["silent_days"], reverse=True)
    return out


# ---- ledger (dedup) — pure transforms --------------------------------------

def _ledger_items(ledger: dict) -> dict:
    items = ledger.get("items") if isinstance(ledger, dict) else None
    return items if isinstance(items, dict) else {}


def needs_warn(item_id: str, stale_row: dict, ledger: dict, rewarn_days: float, now: datetime) -> bool:
    """True if this stale item should be (re-)warned now. Never warned -> True; warned within
    rewarn_days -> False (no nag); past the window -> True. Re-engagement is handled separately by
    reset_on_engagement (drops the entry so a re-cold item warns as a fresh episode)."""
    prev = _ledger_items(ledger).get(item_id)
    if prev is None:
        return True
    warned = _safe_iso(prev.get("warned_at"))
    if warned is None:
        return True
    return (now - warned).total_seconds() / 86400.0 >= rewarn_days


def reset_on_engagement(ledger: dict, item_id: str, current_inbound_ts) -> dict:
    """Drop the ledger entry when a NEW inbound arrived after the warn (re-engaged then went cold =
    a fresh episode). Returns a NEW ledger; never mutates input."""
    items = _ledger_items(ledger)
    prev = items.get(item_id)
    if prev is None or current_inbound_ts is None:
        return ledger
    cur = current_inbound_ts if isinstance(current_inbound_ts, datetime) else _safe_iso(current_inbound_ts)
    if cur is None:
        return ledger
    prev_eng = _safe_iso(prev.get("warned_engagement_ts"))
    if prev_eng is None or cur > prev_eng:
        return {**ledger, "items": {k: v for k, v in items.items() if k != item_id}}
    return ledger


def mark_warned(ledger: dict, stale_row: dict, now: datetime) -> dict:
    """NEW ledger with this item's warn stamp (time + engagement + price + photo count for the
    material-change check). Immutable."""
    items = dict(_ledger_items(ledger))
    items[stale_row["item_id"]] = {
        "warned_at": now.isoformat(),
        "warned_engagement_ts": stale_row.get("last_inbound_ts"),
        "warned_list_price": stale_row.get("list_price"),
        "warned_photo_count": stale_row.get("photo_count"),
    }
    return {**ledger, "items": items}


def pick_due(items: list[dict], threads: list[dict], item_paths: dict | None, ledger: dict,
             config: dict, now: datetime) -> dict | None:
    """The single stale item to act on this pass, or None. Rate-limited to one NEW pick per
    listing_health_interval_hours via the ledger's last_picked_at, so a backlog drips out one item
    per interval rather than one per maint tick."""
    if not _enabled_from_config(config):
        return None
    interval_hours = _interval_hours_from_config(config)
    last_picked = _safe_iso((ledger or {}).get("last_picked_at"))
    if last_picked is not None and interval_hours > 0:
        if (now - last_picked).total_seconds() / 3600.0 < interval_hours:
            return None
    rows = stale_listings(items, threads, item_paths, _stale_days_from_config(config), now)
    rewarn_days = _rewarn_days_from_config(config)
    working = ledger
    for row in rows:
        working = reset_on_engagement(working, row["item_id"], row.get("last_inbound_ts"))
    for row in rows:
        if needs_warn(row["item_id"], row, working, rewarn_days, now):
            return row
    return None


# ---- config parsing (tolerant; mirrors scan_state._interval_from_config) ----

def _num(config: dict, key: str, default: float) -> float:
    raw = config.get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, got {raw!r}")


def _enabled_from_config(config: dict) -> bool:
    return bool(config.get("listing_health_enabled", DEFAULT_ENABLED))


def _stale_days_from_config(config: dict) -> float:
    return _num(config, "stale_days", DEFAULT_STALE_DAYS)


def _interval_hours_from_config(config: dict) -> float:
    return _num(config, "listing_health_interval_hours", DEFAULT_INTERVAL_HOURS)


def _rewarn_days_from_config(config: dict) -> float:
    return _num(config, "rewarn_days", DEFAULT_REWARN_DAYS)


# ---- fail-open loaders ------------------------------------------------------

def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_items(base: Path) -> tuple[list[dict], dict]:
    d = _items_dir(base)
    items: list[dict] = []
    paths: dict = {}
    try:
        names = sorted(p.name for p in d.iterdir())
    except OSError:
        return items, paths
    for name in names:
        if not name.endswith(".json") or "TEST" in name:
            continue
        rec = _load_json(d / name)
        if rec and rec.get("item_id"):
            items.append(rec)
            paths[rec["item_id"]] = d / name
    return items, paths


def _load_threads(base: Path) -> list[dict]:
    """Sell-side threads only — buyer interest in MY listings (buy-side threads are items I want)."""
    out: list[dict] = []
    d = base / "threads"
    try:
        names = sorted(p.name for p in d.iterdir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or "TEST" in name:
            continue
        t = _load_json(d / name)
        if t:
            out.append(t)
    return out


def _ledger_path(base: Path) -> Path:
    return base / "listing_health_state.json"


def _session_path(base: Path) -> Path:
    return base / "listing_health_session.json"


# ---- orchestrators + IO (thin) ---------------------------------------------

def run_due(now: datetime, base: Path | None = None) -> dict:
    base = base or data_dir()
    items, item_paths = _load_items(base)
    threads = _load_threads(base)
    config = _load_json(base / "config.json")
    ledger = _load_json(_ledger_path(base))
    rows = stale_listings(items, threads, item_paths, _stale_days_from_config(config), now)
    pick = pick_due(items, threads, item_paths, ledger, config, now)
    return {"due_item": pick["item_id"] if pick else None, "row": pick, "stale_count": len(rows),
            "interval_hours": _interval_hours_from_config(config), "enabled": _enabled_from_config(config)}


def run_list(now: datetime, base: Path | None = None) -> dict:
    base = base or data_dir()
    items, item_paths = _load_items(base)
    threads = _load_threads(base)
    config = _load_json(base / "config.json")
    rows = stale_listings(items, threads, item_paths, _stale_days_from_config(config), now)
    return {"stale": rows, "count": len(rows)}


def run_start(item_id: str, now: datetime, base: Path | None = None) -> dict:
    """Begin a stale-listing episode: persist the engagement reset + stamp last_picked_at, then write
    the MAINT session baton. Recomputes the stale_row from live data so the baton is current."""
    base = base or data_dir()
    items, item_paths = _load_items(base)
    threads = _load_threads(base)
    config = _load_json(base / "config.json")
    item = next((it for it in items if it.get("item_id") == item_id), None)
    if item is None:
        raise ValueError(f"no item record for {item_id!r}")
    inbound = item_inbound_ts(item_id, threads)
    anchor = published_anchor(item, item_paths.get(item_id), now)
    row = staleness(item, inbound, anchor, _stale_days_from_config(config), now)
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        ledger = reset_on_engagement(ledger, item_id, row.get("last_inbound_ts"))
        atomic_io.write_json(path, {**ledger, "last_picked_at": now.isoformat()})
    session = {"active": True, "item_id": item_id, "stale_row": row, "started_at": now.isoformat()}
    atomic_io.write_json(_session_path(base), session)
    return session


def run_mark(item_id: str, now: datetime, base: Path | None = None) -> dict:
    """Stamp the warn ledger for an item (called after the MAINT pass sends suggestions). Recomputes
    the row from live data so warned_list_price / warned_photo_count reflect the current listing."""
    base = base or data_dir()
    items, item_paths = _load_items(base)
    threads = _load_threads(base)
    config = _load_json(base / "config.json")
    item = next((it for it in items if it.get("item_id") == item_id), None)
    if item is None:
        raise ValueError(f"no item record for {item_id!r}")
    inbound = item_inbound_ts(item_id, threads)
    anchor = published_anchor(item, item_paths.get(item_id), now)
    row = staleness(item, inbound, anchor, _stale_days_from_config(config), now)
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        atomic_io.write_json(path, mark_warned(ledger, row, now))
    return {"marked": item_id, "warned_at": now.isoformat()}


def run_reset(item_id: str, now: datetime, base: Path | None = None) -> dict:
    base = base or data_dir()
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        items = {k: v for k, v in _ledger_items(ledger).items() if k != item_id}
        atomic_io.write_json(path, {**ledger, "items": items})
    return {"reset": item_id}


# ---- CLI --------------------------------------------------------------------

def _resolve_now(now_arg: str) -> datetime:
    if now_arg:
        parsed = parse_iso(now_arg)
        if parsed is None:
            raise ValueError(f"could not parse --now {now_arg!r}")
        return parsed
    return datetime.now().astimezone()


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="listing_health.py", add_help=False)
    parser.add_argument("command", choices=["due", "list", "start", "mark", "reset"])
    parser.add_argument("--item", default="")
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def _validate(ns) -> None:
    if ns.command in ("start", "mark", "reset") and not ns.item.strip():
        raise ValueError(f"{ns.command} requires --item <id>")


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
        elif ns.command == "list":
            result = run_list(now)
        elif ns.command == "start":
            result = run_start(ns.item.strip(), now)
        elif ns.command == "mark":
            result = run_mark(ns.item.strip(), now)
        else:
            result = run_reset(ns.item.strip(), now)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
