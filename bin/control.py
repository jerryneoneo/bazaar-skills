#!/usr/bin/env python3
"""control.py — the runtime pause/correct flag (the agent's stop button).

The single owner of data/control.json, the same way telegram.py owns channel_state.json and
pacing_gate.py owns pacing_state.json: a dumb, durable record with no marketplace logic. It is the
ONE source of truth for "is the agent paused", read by the daemon (bin/agent_daemon.py), the
PreToolUse hook (bin/hooks/pause_guard.py), and the channel drain (bin/channel_control.py), and
written by every interface that can pause — Telegram (/pause), a Claude Code slash command, or this
CLI from a terminal. Because it is just a file, a pause survives a daemon crash/restart, and any
process that writes it pauses the agent without IPC or signals.

State: data/control.json
  {
    "paused":  bool,
    "since":   <iso8601|null>,           # stamped only on the false->true edge
    "source":  <telegram|console|cli|claude-code|null>,
    "reason":  <str>,
    "corrections": [                      # FIFO steering notes, captured while paused, drained on resume
      {"id": "corr_<ms>_<n>", "ts": <iso>, "text": <str>, "source": <str>,
       "target": {"scope": "thread|want|item|session|global", "ref": <str>}|null,
       "applied": bool, "applied_ts": <iso|null>}
    ]
  }

Fail-safe direction is the INVERSE of pacing (which fails open): a missing/garbage file reads as
NOT paused, so a corrupt file can never strand the agent paused forever. Writes are atomic
(temp + os.replace) so a crash mid-write never produces that garbage. Corrections are never
dropped silently — mark_applied compacts only entries already applied.

Importable (daemon/hook/drain call the API) and runnable:
    python3 control.py status                       # print the state as JSON
    python3 control.py is-paused                     # exit 0 if paused, 1 if not (shell gate)
    python3 control.py pause   [--source S] [--reason R]
    python3 control.py resume  [--source S]
    python3 control.py correct --text T [--source S] [--scope thread --ref carousell:123]

Exit: 0 ok · 1 (is-paused only) not paused · 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

VALID_SOURCES = ("telegram", "console", "cli", "claude-code")
VALID_SCOPES = ("thread", "want", "item", "session", "global")
KEEP_APPLIED = 50  # bound the queue: keep all pending + the most recent N applied corrections
_IO_ERRORS = (OSError, ValueError, TypeError)


def _default_state() -> dict:
    return {"paused": False, "since": None, "source": None, "reason": "", "corrections": []}


def data_dir() -> Path:
    """The data directory — relocatable via BAZAAR_DATA_DIR (used by tests for isolation).
    Read at call time so a test that sets the env before importing still hits the scratch dir."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


def control_path() -> Path:
    return data_dir() / "control.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── read path (tolerant; a garbage file reads as NOT paused) ───────────────────────────────

def load_state() -> dict:
    """The current control state, with defaults filled in. Tolerant: an absent or unparseable
    file returns the default (paused=False) rather than raising — pause must never get STUCK on."""
    path = control_path()
    if not path.exists():
        return _default_state()
    try:
        parsed = json.loads(path.read_text())
    except _IO_ERRORS:
        return _default_state()
    if not isinstance(parsed, dict):
        return _default_state()
    state = {**_default_state(), **parsed}
    if not isinstance(state.get("corrections"), list):
        state["corrections"] = []
    return state


def state() -> dict:
    return load_state()


def is_paused() -> bool:
    return bool(load_state().get("paused"))


def pending_corrections() -> list[dict]:
    """Corrections not yet applied, oldest first."""
    return [c for c in load_state().get("corrections", [])
            if isinstance(c, dict) and not c.get("applied")]


# ── write path (atomic; never mutates the loaded dict in place) ────────────────────────────

def _write_state(new_state: dict) -> None:
    """Atomic write: a crash mid-write leaves the previous file intact (temp + os.replace)."""
    path = control_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(new_state, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def pause(source: str = "cli", reason: str = "") -> dict:
    """Set paused. Idempotent: `since` is stamped only on the false->true edge, so re-pausing an
    already-paused agent keeps the original pause time."""
    current = load_state()
    new_state = {
        **current,
        "paused": True,
        "since": current.get("since") if current.get("paused") else _now_iso(),
        "source": source,
        "reason": reason or "",
    }
    _write_state(new_state)
    return new_state


def resume(source: str = "cli") -> dict:
    """Clear paused. Leaves the corrections queue intact so the resume pass can drain it."""
    current = load_state()
    new_state = {**current, "paused": False, "since": None, "source": source}
    _write_state(new_state)
    return new_state


def _build_target(scope: str | None, ref: str | None) -> dict | None:
    if not scope:
        return None
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}")
    return {"scope": scope, "ref": ref or ""}


def add_correction(text: str, source: str = "cli", target: dict | None = None) -> dict:
    """Queue one steering note. Works whether paused or running so a correction is never lost.
    Returns the new correction record."""
    if not (text or "").strip():
        raise ValueError("correction text is required")
    current = load_state()
    corrections = list(current.get("corrections", []))
    ts_ms = int(time.time() * 1000)
    record = {
        "id": f"corr_{ts_ms}_{len(corrections)}",
        "ts": _now_iso(),
        "text": text,
        "source": source,
        "target": target,
        "applied": False,
        "applied_ts": None,
    }
    _write_state({**current, "corrections": corrections + [record]})
    return record


def mark_applied(ids: list[str]) -> None:
    """Mark the given corrections applied (exactly-once guard for the resume pass) and compact the
    queue: keep every still-pending note plus only the most recent KEEP_APPLIED applied ones."""
    wanted = set(ids or [])
    current = load_state()
    now = _now_iso()
    updated = []
    for c in current.get("corrections", []):
        if not isinstance(c, dict):
            continue
        if c.get("id") in wanted and not c.get("applied"):
            updated.append({**c, "applied": True, "applied_ts": now})
        else:
            updated.append(dict(c))
    pending = [c for c in updated if not c.get("applied")]
    applied = [c for c in updated if c.get("applied")]
    _write_state({**current, "corrections": pending + applied[-KEEP_APPLIED:]})


# ── CLI ────────────────────────────────────────────────────────────────────────────────────

def _cmd_status(ns: argparse.Namespace) -> int:
    print(json.dumps(load_state(), indent=2, ensure_ascii=False))
    return 0


def _cmd_is_paused(ns: argparse.Namespace) -> int:
    return 0 if is_paused() else 1


def _cmd_pause(ns: argparse.Namespace) -> int:
    st = pause(source=ns.source, reason=ns.reason)
    print(json.dumps({"ok": True, "paused": True, "since": st["since"], "source": st["source"]}))
    return 0


def _cmd_resume(ns: argparse.Namespace) -> int:
    resume(source=ns.source)
    print(json.dumps({"ok": True, "paused": False,
                      "pending_corrections": len(pending_corrections())}))
    return 0


def _cmd_correct(ns: argparse.Namespace) -> int:
    target = _build_target(ns.scope, ns.ref)
    record = add_correction(ns.text, source=ns.source, target=target)
    print(json.dumps({"ok": True, "id": record["id"], "target": record["target"]}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="control.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=_cmd_status)
    sub.add_parser("is-paused").set_defaults(func=_cmd_is_paused)

    pa = sub.add_parser("pause")
    pa.add_argument("--source", default="cli")
    pa.add_argument("--reason", default="")
    pa.set_defaults(func=_cmd_pause)

    re = sub.add_parser("resume")
    re.add_argument("--source", default="cli")
    re.set_defaults(func=_cmd_resume)

    co = sub.add_parser("correct")
    co.add_argument("--text", required=True)
    co.add_argument("--source", default="cli")
    co.add_argument("--scope", default="")
    co.add_argument("--ref", default="")
    co.set_defaults(func=_cmd_correct)
    return p


def main(argv: list[str]) -> int:
    try:
        ns = build_parser().parse_args(argv[1:])
    except SystemExit:
        return 2
    try:
        return ns.func(ns)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
