#!/usr/bin/env python3
"""delist_item.py — the canonical seller-initiated take-down writer (no LLM).

When the seller asks to delete/remove a listing (NOT a sale completion), the durable
item record ``data/items/<item_id>.json`` MUST be transitioned to removed. Before this
engine existed there was no defined flow, so the model improvised the local-state write
and (when no listing session was active) put the deletion marker into the transient,
single-slot ``data/listing_session.json`` instead of the durable item record — leaving
the item stuck at ``status:"live"`` and the agent reporting a dead listing as live.

This is the ONE place that performs the removal transition, so it always lands on the
right file with one canonical ``status`` value. It is called by ``skills/channel/delist.md``
AFTER each platform's browser take-down recipe has confirmed the listing is actually gone.

Transition (immutable — returns a new record, never mutates in place):
    listing_urls            -> archived into removed_urls (merged), then cleared to {}
    status                  -> "removed_by_seller"   (the canonical value; matches existing data)
    removed_at              -> ISO timestamp
    removed_reason          -> optional free-text note

Idempotent: re-running on an already-removed record preserves removed_urls (never loses the
archived links) and refreshes removed_at only if a new one is supplied.

Usage:
    delist_item.py <item_id> [--removed-at <iso>] [--reason <text>]
Output (stdout, JSON):
    {"ok": true, "item_id": id, "status": "removed_by_seller",
     "removed_urls": {<market>: <url>, ...}, "removed_at": <iso>}

Exit codes: 0 ok · 2 bad input · 3 item record missing/invalid.

The items dir resolves to ../data/items, overridable via $BAZAAR_ITEMS_DIR (for tests).
Pure / stdlib. Reads + writes exactly one item record; touches no floor or address.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # crash-safe (tmp + os.replace) JSON writes

REMOVED_STATUS = "removed_by_seller"


def items_dir() -> Path:
    """The durable item-record dir. $BAZAAR_ITEMS_DIR wins (tests); else bin/ -> ../data/items."""
    override = os.environ.get("BAZAAR_ITEMS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "items"


def apply_removal(record: dict, removed_at: str, reason: str | None = None) -> dict:
    """Return a NEW record transitioned to removed. Archives listing_urls into removed_urls,
    clears listing_urls, sets status + removed_at. Idempotent: never drops already-archived
    URLs. Immutable: the input record is not modified."""
    if not isinstance(record, dict):
        raise ValueError("item record must be a JSON object")

    prior_archived = record.get("removed_urls") or {}
    current_live = record.get("listing_urls") or {}
    if not isinstance(prior_archived, dict) or not isinstance(current_live, dict):
        raise ValueError("listing_urls / removed_urls must be objects")

    # Merge: keep everything already archived, fold in any still-live URLs being removed now.
    merged_removed = {**prior_archived, **current_live}

    updated = dict(record)
    updated["listing_urls"] = {}
    updated["removed_urls"] = merged_removed
    updated["status"] = REMOVED_STATUS
    updated["removed_at"] = removed_at
    if reason:
        updated["removed_reason"] = reason
    return updated


def load_record(item_id: str) -> tuple[Path, dict]:
    """Read the durable item record. Raises on missing/invalid — never returns junk."""
    path = items_dir() / f"{item_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no item record for item_id={item_id!r} at {path}")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or not record.get("item_id"):
        raise ValueError(f"item record for {item_id!r} is malformed (no item_id field)")
    return path, record


def remove(item_id: str, removed_at: str, reason: str | None = None) -> dict:
    """Apply the removal transition to the durable record and write it back. Returns the
    new record. The write is the only side effect."""
    path, record = load_record(item_id)
    updated = apply_removal(record, removed_at, reason)
    atomic_io.write_json(path, updated, ensure_ascii=False)
    return updated


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="delist_item.py")
    p.add_argument("item_id", help="the managed item to take down (data/items/<item_id>.json)")
    p.add_argument("--removed-at", default="", help="ISO timestamp of removal (default: now, UTC)")
    p.add_argument("--reason", default="", help="optional free-text note (e.g. 'seller request')")
    ns = p.parse_args(argv[1:])

    item_id = ns.item_id.strip()
    if not item_id:
        print(json.dumps({"ok": False, "reason": "empty item_id"}))
        return 2

    removed_at = ns.removed_at.strip() or datetime.now(timezone.utc).isoformat()
    try:
        updated = remove(item_id, removed_at, ns.reason.strip() or None)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "item_id": item_id, "reason": str(exc)}))
        return 3
    except ValueError as exc:
        print(json.dumps({"ok": False, "item_id": item_id, "reason": str(exc)}))
        return 3

    print(json.dumps({
        "ok": True,
        "item_id": item_id,
        "status": updated["status"],
        "removed_urls": updated["removed_urls"],
        "removed_at": updated["removed_at"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
