#!/usr/bin/env python3
"""eval_corpus.py — build the evaluation corpus by joining transcript + logs + state.

Pure cores (parse_pass_log, parse_transcript, outbound_by_market) take already-loaded data so
they are trivially testable; `build()` is the thin layer that reads the real files.

The join interval is the pass span: passes never overlap (single-flight run-lock), so
`logs/pass.log`'s `=== <ts> <mode> pass (...) ===` … `=== <ts> <mode> pass done rc=N ===`
delimiters give a clean, non-overlapping timeline. Channel turns come from
`data/channel_transcript.jsonl`; "should have acted" signals come from `data/buyer_peek_state.json`
(unread counts) cross-checked against per-thread outbound + open escalations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eval_schema import EvalRecord, fingerprint
from scan_state import parse_iso  # reuse the tz-safe ISO parser

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
PASS_LOG = LOGS_DIR / "pass.log"
TRANSCRIPT = DATA_DIR / "channel_transcript.jsonl"
PEEK_STATE = DATA_DIR / "buyer_peek_state.json"
THREADS_DIR = DATA_DIR / "threads"
BUYER_THREADS_DIR = DATA_DIR / "buyer_threads"
WANTS_DIR = DATA_DIR / "wants"
TAKEOVER_SEEN = DATA_DIR / "takeover_seen.json"
ESCALATIONS = DATA_DIR / "escalations.jsonl"

DEFAULT_SCOPE_HOURS = 24.0       # pass spans considered "recent"
DEFAULT_UNANSWERED_HOURS = 24.0  # an unread with no reply in this window is "missed"

_PASS_START = re.compile(r"^=== (?P<ts>\S+) (?P<mode>[\w-]+) pass(?: \([^)]*\))? ===$")
_PASS_DONE = re.compile(r"^=== (?P<ts>\S+) (?P<mode>[\w-]+) pass done rc=(?P<rc>-?\d+) ===$")


@dataclass
class Corpus:
    pass_records: list = field(default_factory=list)
    channel_turns: list = field(default_factory=list)
    turns: list = field(default_factory=list)          # raw transcript dicts (oldest-first)
    threads: list = field(default_factory=list)         # raw buyer-thread dicts (data/threads/*.json)
    peek: dict = field(default_factory=dict)            # {market: unread_count}
    outbound_recent: dict = field(default_factory=dict)  # {market: outbound_count in window}
    outbound_texts: list = field(default_factory=list)  # all outbound message texts (leak scan)
    escalation_markets: set = field(default_factory=set)
    takeover_seen: dict = field(default_factory=dict)    # data/takeover_seen.json (tid -> {decision,...})
    tracked_thread_ids: set = field(default_factory=set)  # managed thread ids (sell + buy + want refs)
    unanswered_hours: float = DEFAULT_UNANSWERED_HOURS


# ── pure cores ───────────────────────────────────────────────────────────────────────────

def parse_pass_log(text: str) -> list[EvalRecord]:
    """Pair start/done delimiters into pass records. A start with no matching done (killed pass)
    gets rc=None and its window_end set to the next start (or left blank at EOF)."""
    records: list[EvalRecord] = []
    open_span = None   # (ts, mode, body_lines)
    for line in text.splitlines():
        start = _PASS_START.match(line)
        done = _PASS_DONE.match(line)
        if start:
            if open_span is not None:  # previous span never closed → killed
                records.append(_pass_record(open_span, rc=None, end=start.group("ts")))
            open_span = (start.group("ts"), start.group("mode"), [])
            continue
        if done and open_span is not None and done.group("mode") == open_span[1]:
            records.append(_pass_record(open_span, rc=int(done.group("rc")), end=done.group("ts")))
            open_span = None
            continue
        if open_span is not None:
            open_span[2].append(line)
    if open_span is not None:
        records.append(_pass_record(open_span, rc=None, end=""))
    return records


def _pass_record(span, rc, end) -> EvalRecord:
    ts, mode, body = span
    return EvalRecord(
        record_id=fingerprint("pass", ts, mode),
        kind="pass", pass_mode=mode, window_start=ts, window_end=end, rc=rc,
        narrative="\n".join(body).strip(),
    )


def parse_transcript(turns: list[dict]) -> list[EvalRecord]:
    """One channel_turn record per user ('in') turn: its text, the agent's considered reply
    (following say/ask 'out' turns, excluding 'intent' pre-acks), and the preceding considered
    'out' turn (context for follow-up resolution)."""
    records: list[EvalRecord] = []
    n = len(turns)
    for i, turn in enumerate(turns):
        if turn.get("dir") != "in":
            continue
        prior_text, prior_tag = "", ""
        for j in range(i - 1, -1, -1):
            if turns[j].get("dir") == "out" and turns[j].get("kind") != "intent":
                prior_text = turns[j].get("text", "")
                prior_tag = turns[j].get("tag") or ""
                break
        considered = []
        for k in range(i + 1, n):
            if turns[k].get("dir") == "in":
                break
            if turns[k].get("dir") == "out" and turns[k].get("kind") != "intent":
                considered.append(turns[k].get("text", ""))
        records.append(EvalRecord(
            record_id=fingerprint("ct", turn.get("ts"), turn.get("text")),
            kind="channel_turn",
            user_said=turn.get("text", ""),
            agent_considered=" ⏎ ".join(t for t in considered if t),
            prior_agent=prior_text, prior_tag=prior_tag,
            window_start=str(turn.get("ts", "")),
        ))
    return records


def outbound_by_market(threads: list[dict], now: datetime, lookback_hours: float) -> tuple[dict, list]:
    """(recent outbound count per market, all outbound texts). 'Recent' = ts within lookback_hours
    of now. Market is the thread_id namespace prefix (fb / carousell / …)."""
    counts: dict[str, int] = {}
    texts: list[str] = []
    cutoff = now - timedelta(hours=lookback_hours)
    for thread in threads:
        market = str(thread.get("thread_id", "")).split(":", 1)[0]
        for msg in thread.get("transcript", []):
            if msg.get("dir") != "out":
                continue
            text = msg.get("text", "")
            if text:
                texts.append(text)
            ts = parse_iso(msg.get("ts"))
            if market and ts is not None and ts >= cutoff:
                counts[market] = counts.get(market, 0) + 1
    return counts, texts


def open_escalation_markets(escalations: list[dict]) -> set:
    """Markets with an unresolved escalation (deliberately parked → not a 'missed' miss)."""
    markets = set()
    for esc in escalations:
        if esc.get("resolved_at"):
            continue
        market = esc.get("market") or esc.get("marketplace")
        if market:
            markets.add(str(market))
    return markets


# ── file-reading layer ───────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _peek_counts(raw: dict) -> dict:
    out = {}
    for market, info in (raw or {}).items():
        count = info.get("count") if isinstance(info, dict) else info
        if isinstance(count, (int, float)) and not isinstance(count, bool):
            out[market] = int(count)
    return out


def _filter_recent(pass_records: list[EvalRecord], now: datetime, since, last) -> list[EvalRecord]:
    if last:
        return pass_records[-last:]
    cutoff = since if since else now - timedelta(hours=DEFAULT_SCOPE_HOURS)
    kept = []
    for r in pass_records:
        ts = parse_iso(r.window_start)
        if ts is None or ts >= cutoff:
            kept.append(r)
    return kept


def build(now: datetime | None = None, since: datetime | None = None, last: int | None = None,
          unanswered_hours: float = DEFAULT_UNANSWERED_HOURS) -> Corpus:
    now = now or datetime.now(timezone.utc)
    pass_text = PASS_LOG.read_text(errors="replace") if PASS_LOG.exists() else ""
    pass_records = _filter_recent(parse_pass_log(pass_text), now, since, last)

    turns = _read_jsonl(TRANSCRIPT)
    channel_turns = parse_transcript(turns)

    threads = [_read_json(p) for p in sorted(THREADS_DIR.glob("*.json"))] if THREADS_DIR.exists() else []
    outbound_recent, outbound_texts = outbound_by_market(threads, now, unanswered_hours)

    tracked_thread_ids = set()
    for d in (THREADS_DIR, BUYER_THREADS_DIR):
        if d.exists():
            tracked_thread_ids.update(p.stem for p in d.glob("*.json"))
    if WANTS_DIR.exists():
        for p in WANTS_DIR.glob("*.json"):
            tracked_thread_ids.update(_read_json(p).get("thread_ids") or [])

    return Corpus(
        pass_records=pass_records,
        channel_turns=channel_turns,
        turns=turns,
        threads=threads,
        peek=_peek_counts(_read_json(PEEK_STATE)),
        outbound_recent=outbound_recent,
        outbound_texts=outbound_texts,
        escalation_markets=open_escalation_markets(_read_jsonl(ESCALATIONS)),
        takeover_seen=_read_json(TAKEOVER_SEEN),
        tracked_thread_ids=tracked_thread_ids,
        unanswered_hours=unanswered_hours,
    )
