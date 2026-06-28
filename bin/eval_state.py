#!/usr/bin/env python3
"""eval_state.py — cadence gate for the nightly self-eval.

Mirrors scan_state.py's shape: the daemon asks "is a self-eval due?" and stamps it after running.
The nightly run always does the $0 deterministic pass; when `config.eval_judge_nightly` is set
(default on) the same run also invokes the billed LLM judge (`/bazaar-eval` always runs it). Setting
`config.eval_interval_hours` to 0 disables the nightly eval entirely.

    config.json          -> eval_interval_hours   (cadence; default 24, 0 = disabled)
    config.json          -> eval_judge_nightly    (run the billed judge nightly; default true)
    data/eval_state.json -> last_eval_at          (cursor; created on first mark)

Usage:
    python3 eval_state.py due  [--now ISO]   -> {"due": bool, "interval_hours": n, "last_eval_at": ...}
    python3 eval_state.py mark [--now ISO]   -> stamps last_eval_at = now

Exit codes: 0 ok · 2 bad input · 3 config/data missing or invalid. Never reads a secret.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # crash-safe (tmp + os.replace) JSON writes
from scan_state import parse_iso  # reuse the tz-safe ISO parser

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"
EVAL_STATE_PATH = DATA_DIR / "eval_state.json"

DEFAULT_INTERVAL_HOURS = 24


def _interval_from_config(config: dict) -> float:
    raw = config.get("eval_interval_hours", DEFAULT_INTERVAL_HOURS)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"eval_interval_hours must be a number, got {raw!r}")
    return max(interval, 0.0)  # 0 (or negative) disables the nightly gate


def is_due(last_eval_at, interval_hours: float, now: datetime) -> bool:
    """Pure cadence decision. interval_hours <= 0 → never due. Never-run or aged-out → due."""
    if interval_hours <= 0:
        return False
    last = parse_iso(last_eval_at)
    if last is None:
        return True
    return (now - last).total_seconds() / 3600.0 >= interval_hours


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def run_due(now: datetime) -> dict:
    config = _load_json(CONFIG_PATH)
    interval = _interval_from_config(config)
    last = _load_json(EVAL_STATE_PATH).get("last_eval_at")
    return {"due": is_due(last, interval, now), "interval_hours": interval, "last_eval_at": last}


def run_mark(now: datetime) -> dict:
    state = _load_json(EVAL_STATE_PATH)
    updated = {**state, "last_eval_at": now.isoformat()}
    atomic_io.write_json(EVAL_STATE_PATH, updated)
    return updated


def _resolve_now(now_arg: str) -> datetime:
    if now_arg:
        parsed = parse_iso(now_arg)
        if parsed is None:
            raise ValueError(f"could not parse --now {now_arg!r}")
        return parsed
    return datetime.now().astimezone()


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="eval_state.py", add_help=False)
    parser.add_argument("command", choices=["due", "mark"])
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def main(argv) -> int:
    try:
        ns = _parse_args(argv)
        now = _resolve_now(ns.now)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        result = run_due(now) if ns.command == "due" else {"marked": run_mark(now)}
    except (ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
