#!/usr/bin/env python3
"""triage.py — read-only aggregator of every local "awaiting you" signal.

`/bazaar-catchup` does a deep, mostly read-only sweep of listings, marketplaces, and the
interface, then proposes work. This module is its cheap file-state core: it reads only the
local `data/` state and reports, in one digest, the tasks not yet attended to:

  * open escalations           (sell + buy, awaiting a decision)
  * unread managed threads     (sell + buy, a message past our cursor)
  * draft / undistributed listings
  * open checkouts             (issued, payment not yet completed)
  * open wants                 (a pursuit still in flight)
  * overdue cadence            (a listing re-scan or the nightly self-eval is due)

Plumbing only: it reports state, it never decides or acts, and it NEVER reads a secret
(floor / budget / token / address) - those live in data/floors and data/budgets and are
not touched here. It consolidates the earlier find_unread.py / find_unhandled.py
prototypes (sell-side unread only) into one both-sides digest, using the cursor-walk that
correctly ignores threads we have already replied to.

Standard library only. Data dir relocatable via BAZAAR_DATA_DIR (tests + isolation),
matching the rest of bin/.

Run:
  triage.py            -> human summary
  triage.py --json     -> structured JSON digest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_state import due_market  # noqa: E402  (reuse the pure cadence core)
from eval_state import is_due as eval_is_due  # noqa: E402

# Thread statuses that are NOT "awaiting you": terminal, or already surfaced elsewhere.
# `escalated` shows up under escalations; `held` means the user said stop. Excluding them
# here keeps the digest from double-counting.
SKIP_UNREAD_STATUSES = frozenset({"lost", "handover", "closed", "escalated", "held"})

# Checkout statuses that still need attention (a link was issued, payment not yet done).
OPEN_CHECKOUT_STATUSES = frozenset({"issued", "pending"})

# Want statuses worth surfacing: actively in flight, or waiting on the user's pick.
OPEN_WANT_STATUSES = frozenset({"liaising", "agreed", "recommend"})

DEFAULT_SCAN_INTERVAL_HOURS = 24
DEFAULT_EVAL_INTERVAL_HOURS = 24

CATEGORY_KEYS = (
    "escalations",
    "buyers_waiting",
    "sellers_waiting",
    "wants_open",
    "listings",
    "checkouts",
    "cadence",
)


def data_dir() -> Path:
    """The data dir - relocatable via BAZAAR_DATA_DIR (used by tests for isolation)."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data"


# ---- fail-open loaders (read-only; a broken file is skipped, never raised) ----

def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _load_dir(path: Path) -> list[dict]:
    """Every well-formed *.json in a directory, skipping dev TEST fixtures."""
    out: list[dict] = []
    try:
        names = sorted(p.name for p in path.iterdir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or "TEST" in name:
            continue
        row = _load_json(path / name)
        if row:
            out.append(row)
    return out


# ---- pure signal extractors -------------------------------------------------

def _enabled_markets(seller_config: dict) -> list[str]:
    markets = seller_config.get("marketplaces") or {}
    if isinstance(markets, list):
        return list(markets)
    if isinstance(markets, dict):
        return [mid for mid, sel in markets.items() if isinstance(sel, dict) and sel.get("enabled")]
    return []


def last_unhandled_inbound(thread: dict) -> dict | None:
    """The most recent inbound message AFTER the handled cursor, or None if caught up.

    A cursor that points at our own outbound reply (sent after the buyer's last message)
    means we are caught up - the reason the naive "last inbound != cursor" check was wrong.
    A cursor we cannot find in the transcript is treated conservatively as caught up.
    """
    transcript = thread.get("transcript") or []
    cursor_id = (thread.get("cursor") or {}).get("last_handled_msg_id")
    seen_cursor = cursor_id is None  # no cursor -> the whole transcript is "after"
    latest: dict | None = None
    for msg in transcript:
        if not seen_cursor:
            if msg.get("msg_id") == cursor_id:
                seen_cursor = True
            continue
        if msg.get("dir") == "in":
            latest = msg
    return latest


def _unread_rows(threads: list[dict], id_key: str) -> list[dict]:
    rows: list[dict] = []
    for t in threads:
        if t.get("status", "active") in SKIP_UNREAD_STATUSES:
            continue
        last_in = last_unhandled_inbound(t)
        if last_in is None:
            continue
        rows.append({
            "thread_id": t.get("thread_id"),
            id_key: t.get(id_key),
            "status": t.get("status", "active"),
            "last_in_text": last_in.get("text", ""),
            "last_in_ts": last_in.get("ts"),
        })
    return rows


def _escalation_rows(rows: list[dict], side: str) -> list[dict]:
    out: list[dict] = []
    for e in rows:
        if e.get("status") != "open":
            continue
        out.append({
            "side": side,
            "id": e.get("id"),
            "thread_id": e.get("thread_id"),
            "item_id": e.get("item_id"),
            "kind": e.get("kind"),
            "open_question": e.get("open_question") or e.get("reason") or "",
            "ts": e.get("ts") or e.get("created_at"),
        })
    return out


def _listing_rows(items: list[dict], enabled: list[str]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        status = item.get("status")
        if status == "draft":
            out.append({"item_id": item.get("item_id"), "title": item.get("title", ""),
                        "issue": "draft", "detail": "created but never published"})
            continue
        if status == "live":
            urls = item.get("listing_urls") or {}
            missing = [m for m in enabled if m not in urls]
            if missing:
                out.append({"item_id": item.get("item_id"), "title": item.get("title", ""),
                            "issue": "undistributed",
                            "detail": "not listed on: " + ", ".join(sorted(missing))})
    return out


def _checkout_rows(checkouts: list[dict]) -> list[dict]:
    return [{"sale_id": c.get("sale_id") or c.get("id"), "item_id": c.get("item_id"),
             "thread_id": c.get("thread_id"), "status": c.get("status")}
            for c in checkouts if c.get("status") in OPEN_CHECKOUT_STATUSES]


def _want_rows(wants: list[dict]) -> list[dict]:
    return [{"want_id": w.get("want_id"), "query": w.get("query", ""), "status": w.get("status")}
            for w in wants if w.get("status") in OPEN_WANT_STATUSES]


def _cadence_rows(config: dict, seller_config: dict, scan_state: dict,
                  eval_state: dict, now: datetime) -> list[dict]:
    out: list[dict] = []
    scan_interval = _interval(config, "scan_interval_hours", DEFAULT_SCAN_INTERVAL_HOURS)
    if scan_interval > 0:
        _, info = due_market(seller_config.get("marketplaces", {}), scan_state, scan_interval, now)
        for mid, m in info.items():
            if m.get("overdue"):
                out.append({"kind": "scan_overdue", "detail": f"{mid} is due for a listing re-scan"})
    # Eval only applies once onboarded (it scores the agent's own behavior); a bare/empty
    # install has nothing to evaluate, so a never-run eval is not a pending task there.
    eval_interval = _interval(config, "eval_interval_hours", DEFAULT_EVAL_INTERVAL_HOURS)
    if seller_config and eval_is_due(eval_state.get("last_eval_at"), eval_interval, now):
        out.append({"kind": "eval_overdue", "detail": "the nightly self-eval is due"})
    return out


def _interval(config: dict, key: str, default: float) -> float:
    try:
        return max(float(config.get(key, default)), 0.0)
    except (TypeError, ValueError):
        return float(default)


# ---- orchestrator -----------------------------------------------------------

def build_digest(base: Path, now: datetime) -> dict:
    """Assemble the full file-state digest from `base` (a data dir) at time `now`.

    Pure read: opens only non-secret state files and never raises on a malformed one.
    """
    seller_config = _load_json(base / "seller_config.json")
    config = _load_json(base / "config.json")
    enabled = _enabled_markets(seller_config)

    escalations = (_escalation_rows(_load_jsonl(base / "escalations.jsonl"), "sell")
                   + _escalation_rows(_load_jsonl(base / "buyer_escalations.jsonl"), "buy"))
    buyers_waiting = _unread_rows(_load_dir(base / "threads"), "item_id")
    sellers_waiting = _unread_rows(_load_dir(base / "buyer_threads"), "want_id")
    wants_open = _want_rows(_load_dir(base / "wants"))
    listings = _listing_rows(_load_dir(base / "items"), enabled)
    checkouts = _checkout_rows(_load_dir(base / "checkouts"))
    cadence = _cadence_rows(config, seller_config, _load_json(base / "scan_state.json"),
                            _load_json(base / "eval_state.json"), now)

    digest = {
        "escalations": escalations,
        "buyers_waiting": buyers_waiting,
        "sellers_waiting": sellers_waiting,
        "wants_open": wants_open,
        "listings": listings,
        "checkouts": checkouts,
        "cadence": cadence,
    }
    counts = {key: len(digest[key]) for key in CATEGORY_KEYS}
    counts["total"] = sum(counts[key] for key in CATEGORY_KEYS)
    return {"counts": counts, **digest}


# ---- human render -----------------------------------------------------------

_LABELS = {
    "escalations": "Needs a decision",
    "buyers_waiting": "Buyers waiting",
    "sellers_waiting": "Sellers waiting",
    "wants_open": "Open wants",
    "listings": "Listings",
    "checkouts": "Open checkouts",
    "cadence": "Maintenance",
}


def render(digest: dict) -> str:
    counts = digest.get("counts", {})
    if counts.get("total", 0) == 0:
        return "All caught up, nothing waiting."
    lines = ["Tasks awaiting you:"]
    for key in CATEGORY_KEYS:
        n = counts.get(key, 0)
        if n:
            lines.append(f"  {_LABELS[key]}: {n}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="triage.py")
    parser.add_argument("--json", action="store_true", help="emit the JSON digest instead of a summary")
    ns = parser.parse_args(argv[1:])
    digest = build_digest(data_dir(), datetime.now().astimezone())
    print(json.dumps(digest, indent=2) if ns.json else render(digest))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
