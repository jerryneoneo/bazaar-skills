#!/usr/bin/env python3
"""ui_cache.py — "page memory": a per-market selector cache for the listing flow.

The browser layer is goal-style (see skills/browser-actions.md): every listing field is normally
re-found by looking at the page — a `browser_snapshot` (the single biggest token cost per pass) plus
a vision round-trip, per field, per listing. This cache remembers WHERE each control was last found
so a routine listing can act directly via one `browser_evaluate`, skipping the snapshot+vision step.

It is a HINT / ACCELERATION layer ONLY — never a hard dependency. The consumer protocol (in
skills/browser-actions.md) is: `get` a cached selector → act + VERIFY with one browser_evaluate → on
any miss/ambiguity/verify-fail `invalidate` it and fall back to goal-style vision, then `record` the
freshly-found selector (self-heal). A miss is always safe (fail-open to vision); a stale selector can
only ever degrade to a miss, never a confident wrong action. This file owns NO send path: publish/
send still go through bin/pacing_gate.py and the recipe's confirm/anomaly gates. Generalizes the
hardcoded-JS precedent in bin/buyer_peek.py (MARKET_PROBES), but lives in data/ so it self-heals at
runtime instead of needing a source edit.

It NEVER reads or emits a floor, budget, price, or address — it stores only DOM-locating strings +
timestamps. No secrets in, no secrets out (file mode 0o644).

Usage:
    python3 ui_cache.py get        --market <id> --flow listing [--step <id>] [--now <iso>]
    python3 ui_cache.py record     --market <id> --flow listing --step <id> \\
                                   --strategy <css|aria|role|text> --query '<expr>' \\
                                   [--action-kind <type|click|upload>] --url-pattern '<re>' [--now <iso>]
    python3 ui_cache.py invalidate --market <id> --flow listing [--step <id>] [--now <iso>]
    python3 ui_cache.py prune      [--market <id>] [--flow <id>] \\
                                   [--max-fail 3] [--max-age-days 30] [--now <iso>]

    (tests relocate the whole data dir via the BAZAAR_DATA_DIR env var.)

Output (stdout, JSON). `get --step`:
    {"market": "carousell", "flow": "listing", "step": "title_field",
     "hit": bool, "stale": bool, "selector": {...}|null, "now": <iso>}
  A consumer treats hit:false OR stale:true as "no usable cache" → goal-style vision path.

Exit codes: 0 ok · 2 bad input · 3 data dir unwritable / unexpected IO error.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

SCHEMA_VERSION = 1
STALE_FAILS = 3            # a step that has failed this many times is treated as stale (→ vision)
STALE_DAYS = 30           # a selector not re-verified in this many days is treated as stale
DEFAULT_MAX_FAIL = 3      # prune: drop steps with fail_count >= this
DEFAULT_MAX_AGE_DAYS = 30  # prune: drop steps not verified within this many days
MAX_NOW_DRIFT_SEC = 300   # --now is a narrow test seam: clamp it to wall clock (no time-travel)
VALID_STRATEGIES = ("css", "aria", "role", "text")


def data_dir():
    """The data directory — relocatable via BAZAAR_DATA_DIR (used by tests for isolation)."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


# ---------------------------------------------------------------------------
# pure helpers (no IO) — directly unit-tested
# ---------------------------------------------------------------------------
def parse_iso(value):
    """Parse an ISO-8601 timestamp into an aware datetime (Python 3.9 safe)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def safe_segment(value, label):
    """A market/flow id used as a path segment. Reject anything that could traverse the tree."""
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if "/" in text or "\\" in text or text in (".", "..") or text.startswith("."):
        raise ValueError(f"{label} must be a plain id, got {value!r}")
    return text


def cache_path(dd, market, flow):
    """Absolute path of a flow's cache file: <data>/ui_cache/<market>/<flow>.json."""
    return dd / "ui_cache" / safe_segment(market, "market") / f"{safe_segment(flow, 'flow')}.json"


def is_stale(step_obj, now, max_fail=STALE_FAILS, max_age_days=STALE_DAYS):
    """A cached step is stale (treat as a miss) when it has failed too often, carries no page_url
    guard, or has not been re-verified within the freshness window. Stale never means "act anyway"
    — the consumer falls back to vision, exactly as on a miss."""
    if not isinstance(step_obj, dict):
        return True
    try:
        if int(step_obj.get("fail_count", 0)) >= max_fail:
            return True
    except (TypeError, ValueError):
        return True
    if not (step_obj.get("page_url_pattern") or "").strip():
        return True  # no page guard recorded → never trust it blind; re-confirm via vision
    last_verified = parse_iso(step_obj.get("last_verified_at"))
    if last_verified is None:
        return True
    return (now - last_verified).total_seconds() / 86400.0 > max_age_days


def new_doc(market, flow, now_iso):
    return {"schema_version": SCHEMA_VERSION, "market": market, "flow": flow,
            "recorded_at": now_iso, "steps": {}}


def record_step(doc, market, flow, step, fields, now_iso):
    """Return a NEW doc with `step` upserted (never mutates input). Preserves the step's original
    `recorded_at`, refreshes liveness, and resets `fail_count` (this is the self-heal entry point)."""
    base = doc if isinstance(doc, dict) and doc.get("steps") is not None else new_doc(market, flow, now_iso)
    steps = {sid: dict(s) for sid, s in (base.get("steps") or {}).items()}
    prior = steps.get(step, {})
    steps[step] = {
        "strategy": fields["strategy"],
        "query": fields["query"],
        "action_kind": fields.get("action_kind", ""),
        "page_url_pattern": fields.get("page_url_pattern", ""),
        "recorded_at": prior.get("recorded_at", now_iso),
        "last_verified_at": now_iso,
        "last_ok_at": now_iso,
        "fail_count": 0,
    }
    return {**base, "market": market, "flow": flow,
            "recorded_at": base.get("recorded_at", now_iso), "steps": steps}


def drop_step(doc, step):
    """Return (new_doc, dropped_bool) with `step` removed. Never mutates input."""
    steps = {sid: dict(s) for sid, s in ((doc or {}).get("steps") or {}).items()}
    dropped = steps.pop(step, None) is not None
    return {**(doc or {}), "steps": steps}, dropped


def prune_doc(doc, now, max_fail=DEFAULT_MAX_FAIL, max_age_days=DEFAULT_MAX_AGE_DAYS):
    """Return (new_doc, removed_ids) dropping steps that failed too often or aged out."""
    removed = []
    kept = {}
    for sid, step in ((doc or {}).get("steps") or {}).items():
        if is_stale(step, now, max_fail, max_age_days):
            removed.append(sid)
        else:
            kept[sid] = dict(step)
    return {**(doc or {}), "steps": kept}, removed


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def _read_doc(path):
    """Read a flow doc, or None on any problem (fail-open: a corrupt/missing file is a cache miss)."""
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def _write_doc(path, doc):
    """Atomic write: temp file (0644, no secrets) + os.replace, so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)


def _selector_view(step_obj, now):
    """The cached selector plus a derived `stale` flag, for a consumer to act on."""
    return {**step_obj, "stale": is_stale(step_obj, now)}


def run_get(dd, market, flow, step, now):
    path = cache_path(dd, market, flow)
    doc = _read_doc(path)
    steps = (doc or {}).get("steps") or {}
    if step:
        obj = steps.get(step)
        stale = True if obj is None else is_stale(obj, now)
        return {"market": market, "flow": flow, "step": step,
                "hit": obj is not None, "stale": stale,
                "selector": (_selector_view(obj, now) if obj is not None else None),
                "now": now.isoformat()}
    return {"market": market, "flow": flow, "hit": bool(steps),
            "steps": {sid: _selector_view(obj, now) for sid, obj in steps.items()},
            "now": now.isoformat()}


def run_record(dd, market, flow, step, fields, now):
    if not step:
        raise ValueError("record requires --step <id>")
    if fields["strategy"] not in VALID_STRATEGIES:
        raise ValueError(f"--strategy must be one of {VALID_STRATEGIES}, got {fields['strategy']!r}")
    if not (fields.get("query") or "").strip():
        raise ValueError("record requires a non-empty --query")
    if not (fields.get("page_url_pattern") or "").strip():
        raise ValueError("record requires --url-pattern (the page guard; without it the step is never trusted)")
    path = cache_path(dd, market, flow)
    doc = _read_doc(path)
    updated = record_step(doc, market, flow, step, fields, now.isoformat())
    _write_doc(path, updated)
    return {"market": market, "flow": flow, "step": step, "recorded": True, "now": now.isoformat()}


def run_invalidate(dd, market, flow, step, now):
    path = cache_path(dd, market, flow)
    if not step:
        existed = path.exists()
        if existed:
            path.unlink()
        return {"market": market, "flow": flow, "dropped_flow": existed, "now": now.isoformat()}
    doc = _read_doc(path)
    updated, dropped = drop_step(doc, step)
    if dropped:
        _write_doc(path, updated)
    return {"market": market, "flow": flow, "step": step, "dropped": dropped, "now": now.isoformat()}


def _iter_flow_files(dd, market, flow):
    root = dd / "ui_cache"
    if not root.exists():
        return []
    market_dirs = [root / safe_segment(market, "market")] if market else [p for p in root.iterdir() if p.is_dir()]
    files = []
    for mdir in market_dirs:
        if not mdir.is_dir():
            continue
        if flow:
            candidate = mdir / f"{safe_segment(flow, 'flow')}.json"
            if candidate.exists():
                files.append(candidate)
        else:
            files.extend(sorted(p for p in mdir.iterdir() if p.suffix == ".json"))
    return files


def run_prune(dd, market, flow, max_fail, max_age_days, now):
    pruned = []
    for path in _iter_flow_files(dd, market, flow):
        doc = _read_doc(path)
        if doc is None:
            continue
        updated, removed = prune_doc(doc, now, max_fail, max_age_days)
        if not (updated.get("steps") or {}):
            path.unlink()  # empty flow file → drop it entirely
            pruned.append({"file": str(path.relative_to(dd)), "removed": removed, "deleted_file": True})
        elif removed:
            _write_doc(path, updated)
            pruned.append({"file": str(path.relative_to(dd)), "removed": removed, "deleted_file": False})
    return {"pruned": pruned, "max_fail": max_fail, "max_age_days": max_age_days, "now": now.isoformat()}


def _resolve_now(now_arg):
    if not now_arg:
        return datetime.now().astimezone()
    parsed = parse_iso(now_arg)
    if parsed is None:
        raise ValueError(f"could not parse --now {now_arg!r}")
    drift = abs((parsed - datetime.now(timezone.utc)).total_seconds())
    if drift > MAX_NOW_DRIFT_SEC:
        raise ValueError(f"--now deviates from wall clock by {drift:.0f}s (max {MAX_NOW_DRIFT_SEC})")
    return parsed


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="ui_cache.py", add_help=False)
    parser.add_argument("command", choices=["get", "record", "invalidate", "prune"])
    parser.add_argument("--market", default="")
    parser.add_argument("--flow", default="")
    parser.add_argument("--step", default="")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--action-kind", dest="action_kind", default="")
    parser.add_argument("--url-pattern", dest="url_pattern", default="")
    parser.add_argument("--max-fail", dest="max_fail", type=int, default=DEFAULT_MAX_FAIL)
    parser.add_argument("--max-age-days", dest="max_age_days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse_args(argv)
        now = _resolve_now(ns.now)
        market = ns.market.strip()
        flow = ns.flow.strip()
        step = ns.step.strip()
        # get/record/invalidate are scoped to one flow; prune may sweep all.
        if ns.command in ("get", "record", "invalidate"):
            safe_segment(market, "market")
            safe_segment(flow, "flow")
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        dd = data_dir()
        if ns.command == "get":
            result = run_get(dd, market, flow, step, now)
        elif ns.command == "record":
            fields = {"strategy": ns.strategy.strip(), "query": ns.query,
                      "action_kind": ns.action_kind.strip(), "page_url_pattern": ns.url_pattern.strip()}
            result = run_record(dd, market, flow, step, fields, now)
        elif ns.command == "invalidate":
            result = run_invalidate(dd, market, flow, step, now)
        else:
            result = run_prune(dd, market or "", flow or "", ns.max_fail, ns.max_age_days, now)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
