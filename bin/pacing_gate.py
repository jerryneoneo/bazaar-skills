#!/usr/bin/env python3
"""pacing_gate.py — the atomic account-safety pacing engine.

Replaces the old "LLM counts `out` messages in the transcript" pacing (the model could
not count actions it could not see, and two passes acting on the same account would each
count only their own — collectively busting the cap). This is the single deterministic
authority for "may I act on this marketplace right now?", safe under concurrency.

KEYED PER MARKETPLACE ACCOUNT. `max_actions_per_hour` is enforced per marketplace, not
globally: a platform flags YOUR account on YOUR account's action rate, and the bug being
fixed is sell- and buy-side work on the SAME marketplace double-counting. Both a buyer-
inbox reply and a buy-side message on `fb` increment the one `fb` ledger.

`reserve` is an atomic check-and-record under an OS file lock (fcntl.flock): it reads the
rolling 1-hour ledger, prunes, decides, and — only on "go" — records the action before
releasing the lock. Two workers racing therefore cannot both see a free slot. Recording
at reserve time (not after the send) is deliberately conservative: a crash between
reserve and send under-sends, which is the safe direction for account safety.

Usage:
    python3 pacing_gate.py reserve --marketplace <id> [--kind reply] [--mode interactive] [--now <iso>]
    python3 pacing_gate.py status  [--marketplace <id>] [--now <iso>]
    (tests relocate the whole data dir via the SELLY_DATA_DIR env var; there is no
     per-invocation path override, so every process competes on the same lock file.)

`--mode` selects the post-`go` jitter range ONLY — the cap and quiet_hours are enforced
identically in both modes (they are checked before the delay is chosen):
  unattended (default) -> `reply_delay_sec` jitter (the background daemon's disguise).
  interactive          -> `interactive_reply_delay_sec` jitter (a human is driving the
                          console and watching; a few seconds is both faster and a better
                          disguise in a live chat). The console adapters (/sell, /buy) pass
                          this; every other call site omits it and stays unattended.

Output (stdout, JSON). `reserve`:
    {"decision": "go"|"wait"|"quiet", "delay_sec": <float>, "marketplace": "fb",
     "kind": "reply", "mode": "unattended", "count": <int>, "cap": <int>,
     "window_hours": 1.0, "now": <iso>}

  go    -> wait `delay_sec` (the deliberate anti-automation jitter), then send.
  wait  -> at the hourly cap; `delay_sec` is when a slot next frees. Do NOT send.
  quiet -> inside quiet_hours; `delay_sec` is seconds until the window ends. Queue.

Exit codes: 0 ok · 2 bad input · 3 config/data missing or invalid.
"""

import argparse
import fcntl
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

WINDOW_SECONDS = 3600  # the cap is "per hour"
DEFAULT_CAP = 12
DEFAULT_DELAY = (40, 240)        # unattended daemon: the full anti-automation jitter
DEFAULT_INTERACTIVE_DELAY = (2, 6)  # attended console: short, live-chat-cadence jitter (never zero)
DEFAULT_QUIET = (23, 8)
HARD_CAP_CEILING = 60       # a misconfigured config can only ever TIGHTEN pacing, never exceed this
MAX_NOW_DRIFT_SEC = 300     # --now is a narrow test seam: clamp it to wall clock (no time-travel)
# HARD_DELAY_CEILING_SEC: the upper clamp on a post-go reply delay (both reply_delay_sec and
# interactive_reply_delay_sec). A delay is waited out AFTER the intent is recorded but BEFORE the
# send, so an unbounded delay would let a HEALTHY in-flight intent sit pending longer than
# journal_reconcile.GRACE_SEC and be folded as a false crash orphan. This ceiling MUST stay strictly
# below journal_reconcile.GRACE_SEC (=600) with margin, so the longest legitimate intent->send window
# can never reach the fold floor. Like HARD_CAP_CEILING, a tampered/fat-fingered config can only ever
# tighten pacing toward this ceiling, never exceed it.
HARD_DELAY_CEILING_SEC = 300


def data_dir():
    """The data directory — relocatable via SELLY_DATA_DIR (used by tests for isolation).
    There is no per-invocation override, so all production processes share one lock file."""
    env = os.environ.get("SELLY_DATA_DIR")
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


def prune_actions(actions, now, window_seconds):
    """Return a NEW list of actions whose timestamp is within `window_seconds` of now.

    Unparseable/missing timestamps are dropped (fail-closed: an action we cannot place
    in time does not get to occupy a slot forever)."""
    cutoff = now.timestamp() - window_seconds
    kept = []
    for action in actions or []:
        ts = parse_iso(action.get("ts"))
        if ts is not None and ts.timestamp() > cutoff:
            kept.append(dict(action))
    return kept


def count_in_window(state, marketplace, now, window_seconds):
    """How many actions for `marketplace` fall inside the rolling window."""
    actions = (state.get(marketplace) or {}).get("actions", [])
    return len(prune_actions(actions, now, window_seconds))


def compact_state(state, now, window_seconds):
    """Return a NEW state with every ledger pruned to in-window actions and empty markets dropped.
    Keeps the file bounded — per-market list <= cap, and stale marketplace keys never accumulate."""
    compacted = {}
    for mid, entry in (state or {}).items():
        kept = prune_actions((entry or {}).get("actions", []), now, window_seconds)
        if kept:
            compacted[mid] = {"actions": kept}
    return compacted


def record_action(state, marketplace, ts_iso, kind):
    """Return a NEW state with one action appended to `marketplace` (never mutates input)."""
    updated = {mid: {"actions": [dict(a) for a in (entry.get("actions") or [])]}
               for mid, entry in (state or {}).items()}
    ledger = updated.setdefault(marketplace, {"actions": []})
    ledger["actions"] = ledger.get("actions", []) + [{"ts": ts_iso, "kind": kind}]
    return updated


def in_quiet_hours(hour, start, end):
    """Is `hour` inside the quiet window [start, end)? Handles wrap past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight, e.g. [23, 8]


def _seconds_until_hour(now, target_hour):
    """Seconds from `now` until the next occurrence of `target_hour`:00 local time."""
    delta_hours = (target_hour - now.hour) % 24
    secs = delta_hours * 3600 - now.minute * 60 - now.second
    if secs <= 0:
        secs += 24 * 3600
    return float(secs)


def _seconds_until_slot_frees(actions, now, window_seconds):
    """Seconds until the OLDEST in-window action ages out (a slot then frees)."""
    in_window = prune_actions(actions, now, window_seconds)
    if not in_window:
        return 0.0
    oldest = min(parse_iso(a["ts"]).timestamp() for a in in_window)
    return max(0.0, (oldest + window_seconds) - now.timestamp())


def evaluate(state, marketplace, kind, now, cfg, mode="unattended"):
    """Pure decision. Returns (result_dict, new_state | None).

    new_state is non-None ONLY on "go" (the action recorded). The caller writes it under
    the lock. quiet_hours and the cap are checked BEFORE recording, so a blocked request
    never consumes a slot.

    `mode` ("interactive" | "unattended") selects the post-go jitter range ONLY — it is read
    after quiet_hours and the cap, so it can never relax either safety floor."""
    window = cfg["window_seconds"]
    base = {"marketplace": marketplace, "kind": kind, "mode": mode,
            "cap": cfg["cap"], "window_hours": round(window / 3600.0, 3),
            "now": now.isoformat()}

    if in_quiet_hours(now.hour, cfg["quiet_start"], cfg["quiet_end"]):
        return ({**base, "decision": "quiet", "count": count_in_window(state, marketplace, now, window),
                 "delay_sec": _seconds_until_hour(now, cfg["quiet_end"])}, None)

    actions = (state.get(marketplace) or {}).get("actions", [])
    count = len(prune_actions(actions, now, window))
    if count >= cfg["cap"]:
        return ({**base, "decision": "wait", "count": count,
                 "delay_sec": _seconds_until_slot_frees(actions, now, window)}, None)

    new_state = record_action(state, marketplace, now.isoformat(), kind)
    delay_min, delay_max = ((cfg["idelay_min"], cfg["idelay_max"]) if mode == "interactive"
                            else (cfg["delay_min"], cfg["delay_max"]))
    delay = random.uniform(delay_min, delay_max) if delay_max > 0 else 0.0
    return ({**base, "decision": "go", "count": count, "delay_sec": round(delay, 1)}, new_state)


# ---------------------------------------------------------------------------
# config + IO
# ---------------------------------------------------------------------------
def _validate_delay_pair(value, key):
    """Validate a [min, max] delay pair and clamp its max DOWN to HARD_DELAY_CEILING_SEC.

    A malformed value raises ValueError (loud, like reply_delay_sec); a well-formed max above the
    ceiling is clamped (a tampered/fat-fingered config can only tighten pacing, never blow past the
    fold floor). Returns (min, clamped_max) as floats. Never mutates the input."""
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        raise ValueError(f"{key} must be a [min, max] pair, got {value!r}")
    delay_min, delay_max = float(value[0]), float(value[1])
    if delay_min < 0 or delay_max < delay_min:
        raise ValueError(f"{key} must be 0 <= min <= max, got {value!r}")
    # Clamp DOWN so an unbounded delay can never let a healthy in-flight intent outlive the reconcile
    # fold floor (journal_reconcile.GRACE_SEC). min is clamped too so min <= max always holds.
    delay_max = min(delay_max, float(HARD_DELAY_CEILING_SEC))
    delay_min = min(delay_min, delay_max)
    return delay_min, delay_max


def load_cfg(config_path):
    """Read the pacing knobs from config.json, with validated defaults."""
    config = {}
    if config_path.exists():
        config = json.loads(config_path.read_text())

    cap_raw = config.get("max_actions_per_hour", DEFAULT_CAP)
    try:
        cap = int(cap_raw)
    except (TypeError, ValueError):
        raise ValueError(f"max_actions_per_hour must be an integer, got {cap_raw!r}")
    if cap < 1:
        raise ValueError(f"max_actions_per_hour must be >= 1, got {cap} (to stop the agent use /pause)")
    # Safety ceiling: clamp DOWN so a tampered/fat-fingered config can only tighten the
    # anti-detection budget, never blow past it.
    cap = min(cap, HARD_CAP_CEILING)

    # Both delay ranges are validated AND clamped to HARD_DELAY_CEILING_SEC so an unbounded max can
    # never let a healthy in-flight intent outlive journal_reconcile.GRACE_SEC and be folded.
    delay_min, delay_max = _validate_delay_pair(
        config.get("reply_delay_sec", list(DEFAULT_DELAY)), "reply_delay_sec")

    # Attended-console jitter — validated + clamped symmetrically with reply_delay_sec. A malformed
    # value raises (loud, like reply_delay_sec); a missing key falls back to the short default.
    idelay_min, idelay_max = _validate_delay_pair(
        config.get("interactive_reply_delay_sec", list(DEFAULT_INTERACTIVE_DELAY)),
        "interactive_reply_delay_sec")

    quiet = config.get("quiet_hours", list(DEFAULT_QUIET))
    if not (isinstance(quiet, (list, tuple)) and len(quiet) == 2):
        raise ValueError(f"quiet_hours must be a [start, end] pair, got {quiet!r}")
    quiet_start, quiet_end = int(quiet[0]), int(quiet[1])
    if not (0 <= quiet_start <= 24 and 0 <= quiet_end <= 24):
        raise ValueError(f"quiet_hours must be 0..24, got {quiet!r}")

    return {"cap": cap, "delay_min": delay_min, "delay_max": delay_max,
            "idelay_min": idelay_min, "idelay_max": idelay_max,
            "quiet_start": quiet_start, "quiet_end": quiet_end, "window_seconds": WINDOW_SECONDS}


def _read_state(state_path):
    if not state_path.exists():
        return {}
    text = state_path.read_text().strip()
    return json.loads(text) if text else {}


def _write_state(state_path, state):
    """Atomic write: temp file (0600) + os.replace, so a crash never leaves a half-written ledger."""
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, state_path)


def run_reserve(marketplace, kind, now, state_path, config_path, mode="unattended"):
    """Atomic check-and-record under an exclusive file lock."""
    cfg = load_cfg(config_path)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            state = _read_state(state_path)
            result, new_state = evaluate(state, marketplace, kind, now, cfg, mode)
            if new_state is not None:
                _write_state(state_path, compact_state(new_state, now, cfg["window_seconds"]))
            return result
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def _maybe_block(result):
    """Track B1 — when --block is set, sleep a `go` decision's delay SERVER-SIDE (one CLI call) so the
    LLM does NOT idle the delay across turns (which burns its finite turn budget and risks the pass
    ending mid-send). The delay is clamped to HARD_DELAY_CEILING_SEC (already evaluate's own clamp, so
    this is belt-and-suspenders). After sleeping, return delay_sec=0 + slept_sec so the caller sends
    immediately. A non-go decision (wait/quiet) is returned UNTOUCHED — the caller still handles it.

    The sleep runs AFTER run_reserve has released its lock (we never hold the pacing lock while
    sleeping, which would stall every other marketplace's reservation). Pause stays responsive: the
    PreToolUse pause guard still blocks the actual send, and the daemon SIGTERMs the whole pass tree
    (killing this sleep) on /pause."""
    if result.get("decision") != "go":
        return result
    delay = max(0.0, min(float(result.get("delay_sec", 0) or 0), float(HARD_DELAY_CEILING_SEC)))
    if delay > 0:
        time.sleep(delay)
    return {**result, "delay_sec": 0, "slept_sec": round(delay, 1), "blocked": True}


def run_status(marketplace, now, state_path, config_path):
    cfg = load_cfg(config_path)
    state = _read_state(state_path)
    if marketplace:
        return {"marketplace": marketplace, "cap": cfg["cap"],
                "count": count_in_window(state, marketplace, now, cfg["window_seconds"]),
                "now": now.isoformat()}
    return {"cap": cfg["cap"], "now": now.isoformat(),
            "markets": {mid: count_in_window(state, mid, now, cfg["window_seconds"])
                        for mid in state}}


def _resolve_now(now_arg):
    if not now_arg:
        return datetime.now().astimezone()
    parsed = parse_iso(now_arg)
    if parsed is None:
        raise ValueError(f"could not parse --now {now_arg!r}")
    # --now is a narrow test seam, not a control input: clamp it to wall clock so a stray or
    # hostile timestamp can't time-travel the rolling window and silently empty the hourly ledger.
    drift = abs((parsed - datetime.now(timezone.utc)).total_seconds())
    if drift > MAX_NOW_DRIFT_SEC:
        raise ValueError(f"--now deviates from wall clock by {drift:.0f}s (max {MAX_NOW_DRIFT_SEC})")
    return parsed


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="pacing_gate.py", add_help=False)
    parser.add_argument("command", choices=["reserve", "status"])
    parser.add_argument("--marketplace", default="")
    parser.add_argument("--kind", default="action")
    parser.add_argument("--mode", choices=["interactive", "unattended"], default="unattended")
    parser.add_argument("--block", action="store_true",
                        help="sleep a 'go' decision's delay server-side, then return delay_sec=0 "
                             "(so the caller doesn't idle the wait across LLM turns)")
    parser.add_argument("--now", default="")
    return parser.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse_args(argv)
        now = _resolve_now(ns.now)
        marketplace = ns.marketplace.strip()
        if ns.command == "reserve" and not marketplace:
            raise ValueError("reserve requires --marketplace <id>")
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        dd = data_dir()
        state_path = dd / "pacing_state.json"
        config_path = dd / "config.json"
        if ns.command == "reserve":
            result = run_reserve(marketplace, ns.kind.strip() or "action", now, state_path, config_path, ns.mode)
            if ns.block:
                result = _maybe_block(result)  # sleep a 'go' delay here (lock already released)
        else:
            result = run_status(marketplace, now, state_path, config_path)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
