#!/usr/bin/env python3
"""channel_log.py — the control-channel conversation transcript (short-term memory).

Every message that physically crosses the control channel is journaled here, the same way
telegram.py owns data/channel_state.json: a dumb, durable record with no logic. The channel
pass (bin/harness_run.py) feeds the bounded TAIL back into its prompt so a fresh `claude -p`
pass can resolve follow-ups ("do all tasks", "yes", "the first one") against what was just
said — the prior turns are otherwise lost, because each pass is a fresh subprocess.

Transcript: data/channel_transcript.jsonl, one JSON object per line:
  {"ts": <int unix-sec>, "dir": "in"|"out", "kind": <str>, "text": <str>, "tag": <str|null>}

  dir   "in" = user -> agent, "out" = agent -> user.
  kind  inbound: the poll event kind (text|command|photo|action);
        outbound: the channel verb (say|ask|notify|intent).
  tag   outbound-only optional label the agent sets to mark a turn it may refer back to
        (enumerated-tasks|asked-question|sent-result|progress|escalation|intent); else null.

Secrets never reach this file by construction (floors/budgets/addresses never enter message
text — see .claude/commands/bazaar-run.md §Secrets). _scrub() is a belt-and-braces net that
redacts any exact floor/budget value or address string that somehow slips through.

Importable (telegram.py logs through it) and runnable for inspection:
    python3 channel_log.py tail [--max-turns N] [--max-chars N]   # rendered tail -> stdout
    python3 channel_log.py append --dir out --kind say --text "..." [--tag enumerated-tasks]

Fail-open everywhere: a logging failure must NEVER break a send/poll. Exit: 0 ok · 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRANSCRIPT_PATH = DATA_DIR / "channel_transcript.jsonl"
FLOORS_DIR = DATA_DIR / "floors"
BUDGETS_DIR = DATA_DIR / "budgets"
SELLER_CONFIG = DATA_DIR / "seller_config.json"
BUYER_CONFIG = DATA_DIR / "buyer_config.json"

DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_CHARS = 1600
COMPACT_BYTES = 256 * 1024  # rewrite once the file grows past this …
KEEP_LINES = 400            # … keeping only the most recent N turns
MIN_SECRET_DIGITS = 2       # never redact a single digit (too common to be a safe match)
REDACTED = "‹redacted›"  # ‹redacted›

TAIL_HEADER = (
    "RECENT CONTROL-CHANNEL CONVERSATION (most recent last; a no-session message like "
    '"do all", "yes", "the first one", "take over all" is a FOLLOW-UP to your last [out] '
    "turn — resolve it against that turn, do NOT fall back to \"let me check\"):"
)

VALID_DIRS = ("in", "out")
_LOGGING_ERRORS = (OSError, ValueError, TypeError)


@dataclass(frozen=True)
class Turn:
    ts: int
    dir: str
    kind: str
    text: str
    tag: str | None = None

    def to_dict(self) -> dict:
        return {"ts": self.ts, "dir": self.dir, "kind": self.kind,
                "text": self.text, "tag": self.tag}

    @staticmethod
    def from_dict(obj: dict) -> "Turn | None":
        """Tolerant parse: returns None for anything missing the required shape."""
        if not isinstance(obj, dict):
            return None
        direction = obj.get("dir")
        if direction not in VALID_DIRS:
            return None
        try:
            ts = int(obj.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0
        tag = obj.get("tag")
        return Turn(ts=ts, dir=direction, kind=str(obj.get("kind", "text")),
                    text=str(obj.get("text", "")), tag=str(tag) if tag else None)


# ── secret-scrubbing net (defense in depth; the real guarantee is architectural) ──────────

_secret_cache: tuple[set[str], set[str]] | None = None


def _safe_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _collect_number(value, numbers: set[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    text = str(int(value)) if float(value).is_integer() else str(value)
    if len(text) >= MIN_SECRET_DIGITS:
        numbers.add(text)


def _secret_tokens() -> tuple[set[str], set[str]]:
    """(numeric secrets, address strings) gathered once per process. Fail-open: an unreadable
    silo just shrinks the net. Only the floor (not list_price) and budget figures are secret."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    numbers: set[str] = set()
    strings: set[str] = set()
    if FLOORS_DIR.exists():
        for path in sorted(FLOORS_DIR.glob("*.json")):
            _collect_number(_safe_json(path).get("floor"), numbers)
    if BUDGETS_DIR.exists():
        for path in sorted(BUDGETS_DIR.glob("*.json")):
            rec = _safe_json(path)
            _collect_number(rec.get("max_budget"), numbers)
            _collect_number(rec.get("target_price"), numbers)
    for cfg, key in ((SELLER_CONFIG, "origin"), (BUYER_CONFIG, "delivery_area")):
        loc = _safe_json(cfg).get(key)
        if isinstance(loc, dict):
            for field in ("line1", "postcode"):
                val = loc.get(field)
                if isinstance(val, str) and len(val) >= 4:
                    strings.add(val)
    _secret_cache = (numbers, strings)
    return _secret_cache


def _scrub(text: str) -> str:
    """Redact any exact floor/budget value or address string. Numbers match only as whole
    digit-runs (so a floor of 30 never partially redacts 300)."""
    if not text:
        return text
    numbers, strings = _secret_tokens()
    out = text
    for s in strings:
        if s in out:
            out = out.replace(s, REDACTED)
    for n in numbers:
        out = re.sub(rf"(?<!\d){re.escape(n)}(?!\d)", REDACTED, out)
    return out


# ── write path ────────────────────────────────────────────────────────────────────────────

def _now() -> int:
    return int(time.time())


def _append_line(turn: Turn) -> None:
    TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRANSCRIPT_PATH.open("a") as f:
        f.write(json.dumps(turn.to_dict(), ensure_ascii=False) + "\n")


def append_turn(direction: str, kind: str, text: str, *, tag: str | None = None,
                ts: int | None = None) -> bool:
    """Append one turn. Fail-open: returns False (never raises) so a logging hiccup can't
    break the channel send/poll it rides along with."""
    try:
        if direction not in VALID_DIRS:
            return False
        turn = Turn(
            ts=int(ts) if ts is not None else _now(),
            dir=direction,
            kind=str(kind or "text"),
            text=_scrub(str(text or "")),
            tag=str(tag) if tag else None,
        )
        _append_line(turn)
        _maybe_compact()
        return True
    except _LOGGING_ERRORS:
        return False


def append_event(event: dict) -> bool:
    """Adapt one telegram.py poll event ({kind,text,payload,ts}) into an inbound turn."""
    try:
        kind = event.get("kind", "text")
        text = (event.get("text") or "[photo]") if kind == "photo" else event.get("text", "")
        return append_turn("in", kind, text, ts=event.get("ts"))
    except (AttributeError, TypeError):
        return False


def _maybe_compact() -> None:
    """Size-triggered atomic compaction: keep only the last KEEP_LINES turns. A crash mid-write
    leaves the original intact (temp file + os.replace)."""
    try:
        if TRANSCRIPT_PATH.stat().st_size < COMPACT_BYTES:
            return
    except OSError:
        return
    turns = read_turns()
    if len(turns) <= KEEP_LINES:
        return
    tmp = TRANSCRIPT_PATH.with_name(TRANSCRIPT_PATH.name + ".tmp")
    with tmp.open("w") as f:
        for turn in turns[-KEEP_LINES:]:
            f.write(json.dumps(turn.to_dict(), ensure_ascii=False) + "\n")
    os.replace(tmp, TRANSCRIPT_PATH)


# ── read path ───────────────────────────────────────────────────────────────────────────

def read_turns() -> list[Turn]:
    """All turns oldest-first. Tolerant: blank and torn/corrupt lines are skipped, so a
    half-written final line never breaks a future pass."""
    if not TRANSCRIPT_PATH.exists():
        return []
    turns: list[Turn] = []
    try:
        raw = TRANSCRIPT_PATH.read_text(errors="replace")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        turn = Turn.from_dict(obj)
        if turn is not None:
            turns.append(turn)
    return turns


def tail(max_turns: int = DEFAULT_MAX_TURNS, max_chars: int = DEFAULT_MAX_CHARS) -> list[Turn]:
    """Last <= max_turns turns, dropping oldest until total text fits max_chars."""
    turns = read_turns()[-max_turns:] if max_turns > 0 else []
    while len(turns) > 1 and sum(len(t.text) for t in turns) > max_chars:
        turns = turns[1:]
    return turns


def render_tail(max_turns: int = DEFAULT_MAX_TURNS, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The tail as a compact prompt block, or "" when there is no transcript."""
    turns = tail(max_turns, max_chars)
    if not turns:
        return ""
    lines = [TAIL_HEADER]
    for t in turns:
        flat = " ".join(t.text.split())
        label = (f"out · {t.tag}" if t.tag else "out") if t.dir == "out" else "in"
        lines.append(f"[{label}] {flat}")
    return "\n".join(lines)


# ── CLI (inspection / testing only) ──────────────────────────────────────────────────────

def _cmd_tail(ns) -> int:
    print(render_tail(ns.max_turns, ns.max_chars))
    return 0


def _cmd_append(ns) -> int:
    if ns.dir not in VALID_DIRS:
        print(json.dumps({"error": "dir must be 'in' or 'out'"}), file=sys.stderr)
        return 2
    ok = append_turn(ns.dir, ns.kind, ns.text, tag=(ns.tag or None))
    print(json.dumps({"ok": bool(ok)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="channel_log.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tail")
    t.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, dest="max_turns")
    t.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, dest="max_chars")
    t.set_defaults(func=_cmd_tail)
    a = sub.add_parser("append")
    a.add_argument("--dir", required=True)
    a.add_argument("--kind", default="say")
    a.add_argument("--text", required=True)
    a.add_argument("--tag", default="")
    a.set_defaults(func=_cmd_append)
    return p


def main(argv: list[str]) -> int:
    try:
        ns = build_parser().parse_args(argv[1:])
    except SystemExit:
        return 2
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
