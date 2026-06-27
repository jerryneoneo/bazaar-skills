#!/usr/bin/env python3
"""notify_db.py — read-only reader for the macOS Notification Center DB (~0 tokens, no LLM).

Lists recent delivered notifications as dicts {rec_id, app, origin, title, body, ts}. Two consumers:
  • trigger_resolver.py — is the notification path viable for a platform (did its origin notify)?
  • the notification-driven trigger (notify_watch) — turn a new marketplace notification into an
    instant wake + a cheap content hint, so the agent can act without a snapshot.

For Chrome web-push notifications the SOURCE DOMAIN is in the notification subtitle (e.g.
"www.facebook.com"), which we expose as `origin` — that is what the resolver matches a platform on.

Requires Full Disk Access on the reading process. FAILS OPEN to [] / False if the DB is unreadable
(FDA not granted, not macOS, schema drift) so callers fall back to the polling path. Never raises.
macOS-only.
"""

from __future__ import annotations

import datetime
import os
import plistlib
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"

# Seconds between the Unix epoch (1970-01-01) and the Cocoa reference date (2001-01-01), both UTC.
# record.delivered_date is a Cocoa timestamp; add this to get a Unix timestamp.
_COCOA_UNIX_OFFSET = 978_307_200.0


def _cocoa_to_iso(ts) -> str:
    """Cocoa timestamp -> local ISO-8601 string (matches a local datetime.now() comparison). '' on
    bad input. fromtimestamp converts to local time, so it lines up with trigger_resolver's now."""
    try:
        return datetime.datetime.fromtimestamp(
            float(ts) + _COCOA_UNIX_OFFSET).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _decode_record(blob) -> tuple[str, str, str]:
    """Decode a record.data bplist into (title, origin/subtitle, body). '' for any missing field;
    never raises (a record we cannot decode is simply skipped by the caller)."""
    try:
        pl = plistlib.loads(blob)
    except Exception:  # noqa: BLE001 — any decode failure degrades to a blank record
        return "", "", ""
    req = pl.get("req", pl) if isinstance(pl, dict) else {}
    if not isinstance(req, dict):
        return "", "", ""
    return (str(req.get("titl") or ""), str(req.get("subt") or ""), str(req.get("body") or ""))


def _connect():
    """Read-only connection to the notification DB, or None if it cannot be opened (no FDA / absent)."""
    if not DB_PATH.exists():
        return None
    try:
        # mode=ro (NOT immutable=1): the DB is in WAL mode, and immutable=1 would read a static
        # snapshot that ignores the -wal file, missing the most recent notifications — exactly the
        # ones a real-time trigger needs. mode=ro still sees committed WAL pages.
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None


def available() -> bool:
    """True if the notification DB can actually be read (Full Disk Access granted). Never raises."""
    con = _connect()
    if con is None:
        return False
    try:
        con.execute("SELECT 1 FROM record LIMIT 1")
        return True
    except sqlite3.Error:
        return False
    finally:
        con.close()


def read_recent(limit: int = 200) -> list[dict]:
    """Most-recent delivered notifications, newest first, as dicts the resolver/watcher consume.
    Returns [] on ANY error (no FDA, not macOS, schema drift) so the caller falls back to polling."""
    con = _connect()
    if con is None:
        return []
    out: list[dict] = []
    try:
        apps = {a: i for a, i in con.execute("SELECT app_id, identifier FROM app")}
        rows = con.execute(
            "SELECT rec_id, app_id, delivered_date, data FROM record ORDER BY rec_id DESC LIMIT ?",
            (int(limit),))
        for rec_id, app_id, delivered, blob in rows:
            title, origin, body = _decode_record(blob)
            out.append({
                "rec_id": rec_id,
                "app": apps.get(app_id, ""),
                "origin": origin,
                "title": title,
                "body": body,
                "ts": _cocoa_to_iso(delivered),
            })
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def main(argv: list[str]) -> int:
    import json
    n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30
    if not available():
        print(json.dumps({"available": False, "hint": "grant Full Disk Access to read notifications",
                          "records": []}))
        return 0
    print(json.dumps({"available": True, "records": read_recent(n)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
