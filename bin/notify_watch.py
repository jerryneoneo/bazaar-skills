#!/usr/bin/env python3
"""notify_watch.py — notification-path trigger (the OS-notification counterpart of buyer_peek).

For each enabled market whose trigger_resolver path is "notification" (e.g. Facebook web push), find
OS notifications from that market's origin that are NEWER than a per-market rec_id cursor
(data/notify_watch_state.json), so each notification wakes the agent exactly once (idempotent, like
buyer_peek's memo). The notification body is also handed to the pass as a content hint, so the agent
can often reply without taking a snapshot.

A market on the POLL path is skipped here (buyer_peek handles it). Fail-open: no Full Disk Access /
no notifications / not macOS -> {"pending": 0}. ~0 tokens, no LLM. macOS-only.

Contract (mirrors buyer_peek):
    {"pending": int, "latest_text": str, "markets": {<id>: {"pending": int, "latest_text": str}}}
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_db  # noqa: E402  reads the macOS Notification Center DB (fail-open)
import trigger_resolver as tr  # noqa: E402  per-market path + PLATFORM_ORIGINS

SELLER_DIR = Path(__file__).resolve().parent.parent


def _state_path() -> Path:
    """Per-market last-seen rec_id cursor. Relocatable via BAZAAR_DATA_DIR (test isolation)."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    base = Path(env) if env else (SELLER_DIR / "data")
    return base / "notify_watch_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def select_new(enabled: list[str], notifs: list[dict], state: dict, now_iso: str) -> dict:
    """PURE: for each notification-path market, which notifications are new past its cursor.

    Returns {pending, latest_text, markets:{m:{pending, latest_text, max_rec_id}}, next_state}.
    The cursor advances to the newest rec_id seen for the origin even when there is nothing fresh
    (memo-style), so a notification fires the agent exactly once. Idempotency across a failed pass is
    covered by the per-thread cursors in data/threads plus the poll floor backstop."""
    markets: dict[str, dict] = {}
    pending = 0
    latest_text = ""
    next_state = dict(state)  # immutable update — build a new state, never mutate the loaded one

    for market in enabled:
        if tr.resolve(market, notifs, now_iso) != "notification":
            continue  # poll-path market is handled by buyer_peek, not here
        origin = tr.PLATFORM_ORIGINS.get(market, "")
        mine = [n for n in notifs if origin and origin in (n.get("origin") or "")]
        if not mine:
            continue
        cur = int(state.get(market, 0) or 0)
        max_rec = max(int(n.get("rec_id", 0) or 0) for n in mine)
        fresh = [n for n in mine if int(n.get("rec_id", 0) or 0) > cur]
        next_state[market] = max(cur, max_rec)
        if fresh:
            top = max(fresh, key=lambda n: int(n.get("rec_id", 0) or 0))
            text = f"[{market}] {top.get('title', '')}: {top.get('body', '')}".strip()
            markets[market] = {"pending": len(fresh), "latest_text": text, "max_rec_id": max_rec}
            pending += 1
            if not latest_text:
                latest_text = text
    return {"pending": pending, "latest_text": latest_text, "markets": markets,
            "next_state": next_state}


def watch(enabled: list[str] | None = None, now_iso: str | None = None, update: bool = True) -> dict:
    """Read the notification DB and report per-market notification-path pending. Fail-open."""
    now_iso = now_iso or datetime.datetime.now().isoformat()
    if enabled is None:
        enabled = _enabled_markets()
    try:
        notifs = notify_db.read_recent()
        state = _load_state()
        res = select_new(enabled, notifs, state, now_iso)
        if update:
            _save_state(res["next_state"])
        return {"pending": res["pending"], "latest_text": res["latest_text"],
                "markets": res["markets"]}
    except Exception:  # noqa: BLE001 — never crash the daemon; degrade to "nothing new"
        return {"pending": 0, "latest_text": "", "markets": {}}


def _enabled_markets() -> list[str]:
    try:
        base = Path(os.environ["BAZAAR_DATA_DIR"]) if os.environ.get("BAZAAR_DATA_DIR") \
            else (SELLER_DIR / "data")
        cfg = json.loads((base / "seller_config.json").read_text())
        mk = cfg.get("marketplaces", {})
        if isinstance(mk, dict):
            return [m for m, sel in mk.items() if sel.get("enabled")]
        if isinstance(mk, list):
            return list(mk)
    except (OSError, ValueError):
        pass
    return []


def main(argv: list[str]) -> int:
    update = "--no-update" not in argv
    print(json.dumps(watch(_enabled_markets(), update=update)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
