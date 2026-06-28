#!/usr/bin/env python3
"""buy_peek.py — cheap, non-LLM probe: is there BUY-side work to do? (file-state only, ~0 tokens)

The buy-side analogue of buyer_peek.py, but it needs no browser: whether a want is actionable is a
pure function of its file state. It lets the daemon GATE the expensive `run_pass.sh buy` so it fires
only when a want actually needs a step — not every cycle, and crucially NOT while a want is parked
waiting for the user's budget/picks answer.

A want is actionable when:
  • status in {liaising, agreed}                  → poll its seller-reply threads (the buy pass does
                                                    the cheap targeted read; idempotent via cursors).
  • status in {new, searching, recommend} AND its buy_session is NOT blocked on a user answer
    (awaiting a budget / selection)               → there's search/shortlist work to do.
Note: `awaiting_search_confirm` is treated as NON-blocking — the daemon auto-searches (locked
decision), so it does not wait on that flag. Genuine user-input waits (awaiting_price_range /
awaiting_confirm) DO block, so the pass doesn't re-search every cycle.

Contract (mirrors the channel/buyer peeks):
    prints {"pending": int, "want_id": str|null, "latest_text": str} to stdout, exit 0.
FAIL-OPEN: any error → {"pending": 0, ...} so a broken probe degrades to "no work", never crashes
the daemon (the force_buy_pass_every safety net covers a rare miss).

Usage: buy_peek.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WANTS_DIR = DATA_DIR / "wants"
BUY_SESSION_PATH = DATA_DIR / "buy_session.json"

LIAISE_STATES = {"liaising", "agreed"}
SEARCH_STATES = {"new", "searching", "recommend"}
# Steps/flags where the want is genuinely waiting on the USER (don't re-fire the pass).
USER_WAIT_STEPS = {"awaiting_price_range", "awaiting_confirm", "awaiting_budget", "awaiting_selection"}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _blocked_on_user(session: dict, want_id: str) -> bool:
    """True when the active buy_session for this want is parked awaiting a user budget/picks answer.
    awaiting_search_confirm is deliberately NOT blocking (auto-search policy)."""
    if not session.get("active") or session.get("want_id") != want_id:
        return False
    if session.get("step") in USER_WAIT_STEPS:
        return True
    for key, val in session.items():
        if key.startswith("awaiting_") and key != "awaiting_search_confirm" and val:
            return True
    return False


def _search_actionable(want: dict, session: dict) -> str:
    """Return a short reason if the want needs a SEARCH step now (file-only, no browser), else ''.
    LIAISE-state wants are handled separately: a full buy pass should fire only when a seller actually
    replied (a cheap inbox scan), not every cycle just because a want is open."""
    status = str(want.get("status", "")).lower()
    want_id = want.get("id") or want.get("want_id") or ""
    if status in SEARCH_STATES and not _blocked_on_user(session, want_id):
        return "search (find + shortlist)"
    return ""


def _has_liaise_want() -> bool:
    """True if any want is liaising/agreed (so it's worth a cheap inbox scan for fresh replies)."""
    if not WANTS_DIR.is_dir():
        return False
    for path in WANTS_DIR.glob("*.json"):
        if str(_load_json(path).get("status", "")).lower() in LIAISE_STATES:
            return True
    return False


def peek() -> dict:
    """Cheap gate (~0 tokens) for the buy pass. SEARCH-state wants are file-only (unchanged). For
    LIAISE-state wants, fire ONLY when a tracked buy thread has a fresh seller reply (inbox_scan over
    CDP) — previously this returned pending=1 every cycle for any open want, the dominant cost leak.
    Fail-open: an inbox-scan error degrades to 'no liaise work' (force_buy_pass_every is the backstop)."""
    if not WANTS_DIR.is_dir():
        return {"pending": 0, "want_id": None, "latest_text": ""}
    session = _load_json(BUY_SESSION_PATH)
    for path in sorted(WANTS_DIR.glob("*.json")):  # deterministic order — search work first
        want = _load_json(path)
        if not want:
            continue
        reason = _search_actionable(want, session)
        if reason:
            want_id = want.get("id") or want.get("want_id") or path.stem
            return {"pending": 1, "want_id": want_id, "latest_text": f"[{want_id}] {reason}"}
    if _has_liaise_want():
        import inbox_scan  # lazy: skip the CDP import entirely when no want is liaising
        reply = inbox_scan.buy_pending()
        if reply.get("pending"):
            return {"pending": 1, "want_id": reply.get("want_id"),
                    "latest_text": reply.get("latest_text", "")}
    return {"pending": 0, "want_id": None, "latest_text": ""}


def main(argv: list[str]) -> int:
    try:
        print(json.dumps(peek()))
    except Exception as exc:  # last-resort fail-open: never crash the daemon
        print(json.dumps({"pending": 0, "want_id": None, "latest_text": "", "error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
