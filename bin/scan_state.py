#!/usr/bin/env python3
"""scan_state.py — the cadence gate for autonomous detection of non-Bazaar listings.

The distribution SCAN (find listings the seller made OUTSIDE Bazaar and offer to
cross-list them) is expensive — it navigates each marketplace's "your listings" page
and paginates. It must NOT run every loop pass. This owns the one deterministic answer
to "which enabled marketplace is due for a re-scan right now?" so the main loop
(`.claude/commands/bazaar-run.md`) can start at most one SCAN per pass, on a cadence.

Pure / deterministic core (`due_market`); a thin CLI reads the real files:

    seller_config.json -> marketplaces   (which markets are enabled)
    config.json        -> scan_interval_hours   (the cadence; default 24)
    data/scan_state.json -> per-market last_scanned_at   (the cursor; created on first mark)

It NEVER reads or emits a floor or an address — no secrets in, no secrets out.

Usage:
    python3 scan_state.py due  [--now <iso>]          # which market to scan (or none)
    python3 scan_state.py mark --market <id> [--now <iso>]   # stamp a market scanned now
Output (stdout, JSON). `due`:
    {"due_market": <id>|null, "interval_hours": n, "now": <iso>,
     "markets": {<id>: {"enabled": bool, "last_scanned_at": <iso>|null,
                        "age_hours": <float>|null, "overdue": bool}}}

Exit codes: 0 ok · 2 bad input · 3 config/data missing or invalid.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"
SELLER_CONFIG_PATH = DATA_DIR / "seller_config.json"
SCAN_STATE_PATH = DATA_DIR / "scan_state.json"

DEFAULT_INTERVAL_HOURS = 24


def _load_json(path, label):
    if not path.exists():
        raise FileNotFoundError(f"{label} not found at {path}")
    return json.loads(path.read_text())


def parse_iso(value):
    """Parse an ISO-8601 timestamp into an aware datetime (Python 3.9 safe).

    Accepts a trailing 'Z' and offset forms like '+08:00'. A naive timestamp is
    treated as UTC so comparisons never raise on mixed awareness.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def enabled_markets(marketplaces):
    """Ordered list of enabled market ids. Tolerates the legacy ARRAY selection
    (all enabled) and the object shape {id: {enabled: bool}}."""
    if isinstance(marketplaces, list):
        return list(marketplaces)
    if isinstance(marketplaces, dict):
        return [mid for mid, sel in marketplaces.items() if sel.get("enabled")]
    return []


def due_market(marketplaces, scan_state, interval_hours, now):
    """Pure cadence decision. Returns (due_market_id|None, markets_info).

    A market is overdue when it has never been scanned, or its last scan is at least
    `interval_hours` old. Among overdue markets the MOST overdue wins (never-scanned
    first, then the oldest last_scanned_at); ties break on selection order.
    """
    enabled = enabled_markets(marketplaces)
    state = scan_state if isinstance(scan_state, dict) else {}

    info = {}
    best_id = None
    best_rank = None  # higher = more overdue; (never_flag, age_hours)
    for order, mid in enumerate(enabled):
        last_raw = (state.get(mid) or {}).get("last_scanned_at")
        last = parse_iso(last_raw)
        if last is None:
            age_hours = None
            overdue = True
            rank = (1, 0.0, -order)  # never scanned: top priority
        else:
            age_hours = (now - last).total_seconds() / 3600.0
            overdue = age_hours >= interval_hours
            rank = (0, age_hours, -order)
        info[mid] = {
            "enabled": True,
            "last_scanned_at": last_raw,
            "age_hours": None if age_hours is None else round(age_hours, 3),
            "overdue": overdue,
        }
        if overdue and (best_rank is None or rank > best_rank):
            best_rank = rank
            best_id = mid

    return best_id, info


def _interval_from_config(config):
    raw = config.get("scan_interval_hours", DEFAULT_INTERVAL_HOURS)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"scan_interval_hours must be a number, got {raw!r}")
    if interval <= 0:
        raise ValueError(f"scan_interval_hours must be positive, got {interval}")
    return interval


def run_due(now):
    config = _load_json(CONFIG_PATH, "config.json")
    seller = _load_json(SELLER_CONFIG_PATH, "seller_config.json")
    interval = _interval_from_config(config)
    scan_state = _load_json(SCAN_STATE_PATH, "scan_state.json") if SCAN_STATE_PATH.exists() else {}

    market, info = due_market(seller.get("marketplaces", {}), scan_state, interval, now)
    return {
        "due_market": market,
        "interval_hours": interval,
        "now": now.isoformat(),
        "markets": info,
    }


def mark_scanned(scan_state, market, now):
    """Return a NEW scan_state dict with `market` stamped at `now` (never mutates input)."""
    updated = {mid: dict(entry) for mid, entry in (scan_state or {}).items()}
    updated[market] = {**updated.get(market, {}), "last_scanned_at": now.isoformat()}
    return updated


def run_mark(market, now):
    if not market:
        raise ValueError("mark requires --market <id>")
    scan_state = _load_json(SCAN_STATE_PATH, "scan_state.json") if SCAN_STATE_PATH.exists() else {}
    updated = mark_scanned(scan_state, market, now)
    SCAN_STATE_PATH.write_text(json.dumps(updated, indent=2) + "\n")
    return updated


def _resolve_now(now_arg):
    if now_arg:
        parsed = parse_iso(now_arg)
        if parsed is None:
            raise ValueError(f"could not parse --now {now_arg!r}")
        return parsed
    return datetime.now().astimezone()


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="scan_state.py", add_help=False)
    parser.add_argument("command", choices=["due", "mark"])
    parser.add_argument("--market", default="")
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse_args(argv)
        now = _resolve_now(ns.now)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        if ns.command == "due":
            result = run_due(now)
        else:
            result = {"marked": ns.market.strip(), "scan_state": run_mark(ns.market.strip(), now)}
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
