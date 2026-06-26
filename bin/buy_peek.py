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


def _actionable(want: dict, session: dict) -> str:
    """Return a short reason if the want needs a buy step now, else ''."""
    status = str(want.get("status", "")).lower()
    want_id = want.get("id") or want.get("want_id") or ""
    if status in LIAISE_STATES:
        return "liaise (poll seller replies)"
    if status in SEARCH_STATES and not _blocked_on_user(session, want_id):
        return "search (find + shortlist)"
    return ""


def peek() -> dict:
    if not WANTS_DIR.is_dir():
        return {"pending": 0, "want_id": None, "latest_text": ""}
    session = _load_json(BUY_SESSION_PATH)
    for path in sorted(WANTS_DIR.glob("*.json")):  # deterministic order
        want = _load_json(path)
        if not want:
            continue
        reason = _actionable(want, session)
        if reason:
            want_id = want.get("id") or want.get("want_id") or path.stem
            return {"pending": 1, "want_id": want_id, "latest_text": f"[{want_id}] {reason}"}
    return {"pending": 0, "want_id": None, "latest_text": ""}


def main(argv: list[str]) -> int:
    try:
        print(json.dumps(peek()))
    except Exception as exc:  # last-resort fail-open: never crash the daemon
        print(json.dumps({"pending": 0, "want_id": None, "latest_text": "", "error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
