#!/usr/bin/env python3
"""relist_state.py — per-(item, market) cooldown ledger for FREE relist offers.

Carousell's in-app assistant (and, later, other marketplaces) proactively offers to relist / renew /
bump a seller's listing — a free visibility refresh. The agent takes the FREE ones (see
skills/channel/relist-offer.md), but must not relist the SAME item on every pass the assistant nudges
(account-safety + channel noise). This ledger enforces a minimum gap (`relist_cooldown_days`, default
1) between relists of one item on one market, and doubles as the dedup so a still-unread offer doesn't
re-fire work it already did.

This is intentionally SMALL: a pure cooldown decision + an atomic stamp, no LLM, no browser. The
free-vs-paid decision lives in the per-market recipe (fail-closed); this only answers "is this item
eligible to be relisted again yet?".

    config.json -> relist_cooldown_days (default 1.0; <= 0 disables the cooldown, always due)
    data/relist_state.json -> {"items": {"<market>:<item_id>": {"last_relist_at": "<iso>"}}}

Usage:
    relist_state.py due  --item <id> --market <m> [--now ISO]
        -> {"due": bool, "item_id", "market", "cooldown_days", "last_relist_at"}
    relist_state.py mark --item <id> --market <m> [--now ISO]
        -> {"marked": "<market>:<item_id>", "last_relist_at": "<iso>"}

Exit codes: 0 ok · 2 bad input · 3 config/data invalid. Never reads a secret.
Data dir relocatable via SELLY_DATA_DIR (matches listing_health.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  crash-safe writes + cross-process lock
from scan_state import parse_iso  # noqa: E402  the one tz-safe ISO parser

DEFAULT_COOLDOWN_DAYS = 1.0


def data_dir() -> Path:
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data"


def _ledger_path(base: Path) -> Path:
    return base / "relist_state.json"


def _safe_iso(value):
    """parse_iso, but unparseable input returns None instead of raising (a corrupt stamp degrades to
    'never relisted' → eligible, which is the fail-open default)."""
    try:
        return parse_iso(value)
    except (ValueError, TypeError):
        return None


def ledger_key(market: str, item_id: str) -> str:
    """Namespace the cooldown by market so the SAME item relisted on two marketplaces keeps
    independent cooldowns ("carousell:abc" vs "fb:abc")."""
    return f"{market}:{item_id}"


# ---- pure helpers (no IO) — directly unit-tested ---------------------------

def _ledger_items(ledger: dict) -> dict:
    items = ledger.get("items") if isinstance(ledger, dict) else None
    return items if isinstance(items, dict) else {}


def is_due(market: str, item_id: str, ledger: dict, cooldown_days: float, now: datetime) -> bool:
    """True when this item may be relisted again on this market. Never relisted -> True; last relist
    older than cooldown_days -> True; within the window -> False. cooldown_days <= 0 disables the
    cooldown (always True). A corrupt/missing stamp fails OPEN to True (the recipe's pacing + the
    platform's own 'already relisted' response are the backstops)."""
    if cooldown_days <= 0:
        return True
    prev = _ledger_items(ledger).get(ledger_key(market, item_id))
    if not isinstance(prev, dict):
        return True
    last = _safe_iso(prev.get("last_relist_at"))
    if last is None:
        return True
    return (now - last).total_seconds() / 86400.0 >= cooldown_days


def mark_relisted(market: str, item_id: str, ledger: dict, now: datetime) -> dict:
    """NEW ledger with this item's last-relist stamp on this market. Immutable (never mutates input)."""
    items = dict(_ledger_items(ledger))
    items[ledger_key(market, item_id)] = {"last_relist_at": now.isoformat()}
    return {**ledger, "items": items}


# ---- config parsing (tolerant; mirrors listing_health._num) ----------------

def _cooldown_days_from_config(config: dict) -> float:
    raw = config.get("relist_cooldown_days", DEFAULT_COOLDOWN_DAYS)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"relist_cooldown_days must be a number, got {raw!r}")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---- orchestrators + IO (thin) ---------------------------------------------

def run_due(item_id: str, market: str, now: datetime, base: Path | None = None) -> dict:
    base = base or data_dir()
    config = _load_json(base / "config.json")
    cooldown = _cooldown_days_from_config(config)
    ledger = _load_json(_ledger_path(base))
    prev = _ledger_items(ledger).get(ledger_key(market, item_id))
    last_at = prev.get("last_relist_at") if isinstance(prev, dict) else None
    return {"due": is_due(market, item_id, ledger, cooldown, now), "item_id": item_id,
            "market": market, "cooldown_days": cooldown, "last_relist_at": last_at}


def run_mark(item_id: str, market: str, now: datetime, base: Path | None = None) -> dict:
    base = base or data_dir()
    path = _ledger_path(base)
    with atomic_io.locked(path):
        ledger = _load_json(path)
        atomic_io.write_json(path, mark_relisted(market, item_id, ledger, now))
    return {"marked": ledger_key(market, item_id), "last_relist_at": now.isoformat()}


# ---- CLI --------------------------------------------------------------------

def _resolve_now(now_arg: str) -> datetime:
    if now_arg:
        parsed = parse_iso(now_arg)
        if parsed is None:
            raise ValueError(f"could not parse --now {now_arg!r}")
        return parsed
    return datetime.now().astimezone()


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="relist_state.py", add_help=False)
    parser.add_argument("command", choices=["due", "mark"])
    parser.add_argument("--item", default="")
    parser.add_argument("--market", default="")
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def _validate(ns) -> None:
    if not ns.item.strip():
        raise ValueError(f"{ns.command} requires --item <id>")
    if not ns.market.strip():
        raise ValueError(f"{ns.command} requires --market <m>")


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
            result = run_due(ns.item.strip(), ns.market.strip(), now)
        else:
            result = run_mark(ns.item.strip(), ns.market.strip(), now)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
