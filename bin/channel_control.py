#!/usr/bin/env python3
"""channel_control.py — deterministic control-channel drain for a PAUSED agent (no LLM).

When the agent is paused, the daemon must still drain the control channel so the user can /resume
and leave corrections — but it must NOT spend an LLM pass or take any marketplace action. This is
that drain: it consumes Telegram events via the existing bin/telegram.py, and for each one:

  /pause   → control.pause("telegram")                  ack "⏸ Paused…"
  /resume  → control.resume("telegram")                 ack "▶️ Resuming…"
  anything → control.add_correction(text, target=…)     ack "📝 Noted…"

Capturing corrections here (deterministically) means a steering note is never lost even though no
LLM pass runs while paused. The resume pass (skills/channel/corrections.md) applies them. This file
contains no marketplace logic and never drives the browser — it is the paused-state sibling of the
LLM channel pass.

The classification is a pure function (process_events) so it is unit-tested on synthetic events;
drain() is the thin orchestration that shells out to telegram.py poll/send (the same way the daemon
already talks to telegram.py).

    python3 channel_control.py drain        # consume + classify + ack one batch; print a summary

Exit: 0 ok (even with nothing pending) · 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import control  # noqa: E402  the single owner of the pause flag + corrections queue

ACK_PAUSE = "⏸ Paused. I'll stop acting and wait. Send a correction, then /resume."
ACK_RESUME = "▶️ Resuming — applying your corrections now."

# Directories scanned for unambiguous correction targets, most-specific first. A correction whose
# text contains a known ref/id is routed straight at that thread/want/item; otherwise target=None
# and the resume pass resolves it from conversational context.
_TARGET_DIRS = (
    ("thread", "threads"),
    ("thread", "buyer_threads"),
    ("want", "wants"),
    ("item", "items"),
)
MIN_REF_LEN = 4  # ignore very short ids as substrings (too likely to false-match)


def _known_refs() -> list[tuple[str, str]]:
    """(scope, ref) candidates from the state dirs, most-specific first. Fail-open: an unreadable
    dir just shrinks the candidate set."""
    refs: list[tuple[str, str]] = []
    base = control.data_dir()
    for scope, sub in _TARGET_DIRS:
        d = base / sub
        if not d.exists():
            continue
        try:
            for path in sorted(d.glob("*.json")):
                ref = path.stem
                if len(ref) >= MIN_REF_LEN:
                    refs.append((scope, ref))
        except OSError:
            continue
    return refs


def infer_target(text: str) -> dict | None:
    """Lightweight deterministic targeting: the first known ref that appears in the text wins.
    Returns None when nothing matches (the common case — the resume pass then resolves it)."""
    if not text:
        return None
    haystack = text.lower()
    for scope, ref in _known_refs():
        if ref.lower() in haystack:
            return {"scope": scope, "ref": ref}
    return None


def _first_word(text: str) -> str:
    return (text or "").strip().split(maxsplit=1)[0].lower()


def process_events(events: list[dict]) -> list[str]:
    """Apply control side-effects for one batch of normalized telegram events (in order) and return
    the acks to send back. Pure except for the control.json writes (isolated via BAZAAR_DATA_DIR in
    tests). Every inbound is accounted for: a /pause or /resume action, or a queued correction."""
    acks: list[str] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind", "text")
        text = ev.get("text", "") or ""
        if kind == "command" and _first_word(text) == "/pause":
            reason = text.strip()[len("/pause"):].strip()
            control.pause(source="telegram", reason=reason)
            acks.append(ACK_PAUSE)
            continue
        if kind == "command" and _first_word(text) == "/resume":
            control.resume(source="telegram")
            acks.append(ACK_RESUME)
            continue
        # Everything else while paused is captured as a steering note (nothing is lost).
        note = text if kind != "photo" else (text or "[photo]")
        record = control.add_correction(note, source="telegram", target=infer_target(text))
        where = f" → {record['target']['scope']} {record['target']['ref']}" if record["target"] else ""
        flat = " ".join(note.split())[:80]
        acks.append(f"📝 Noted: '{flat}'{where}. I'll apply this when you /resume.")
    return acks


# ── orchestration (shells out to telegram.py, the same way the daemon does) ────────────────

def _poll_events(env: dict | None) -> list[dict]:
    """Consume one batch via telegram.py poll (advances the offset, acks buttons, journals)."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "telegram.py"), "poll", "--timeout", "0"],
                             capture_output=True, text=True, env=env, timeout=30)
    except subprocess.SubprocessError:
        return []
    if out.returncode != 0:
        return []
    try:
        return json.loads(out.stdout).get("events", [])
    except (ValueError, AttributeError):
        return []


def _send(text: str, env: dict | None) -> None:
    try:
        subprocess.run([sys.executable, str(BIN / "telegram.py"), "send", "--text", text,
                        "--kind", "say"], capture_output=True, text=True, env=env, timeout=30)
    except subprocess.SubprocessError:
        pass  # an ack failure must never break the drain


def drain(env: dict | None = None) -> int:
    """Consume one batch, apply control side-effects, and ack the user. Idempotent: the poll
    advances the offset so a re-drain sees nothing new."""
    events = _poll_events(env)
    acks = process_events(events)
    for ack in acks:
        _send(ack, env)
    print(json.dumps({"drained": len(events), "acks": len(acks),
                      "paused": control.is_paused()}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="channel_control.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("drain").set_defaults(func=lambda ns: drain())
    return p


def main(argv: list[str]) -> int:
    try:
        ns = build_parser().parse_args(argv[1:])
    except SystemExit:
        return 2
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
