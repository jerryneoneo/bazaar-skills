#!/usr/bin/env python3
"""Tests for eval_corpus.py pure cores (no file I/O).

    python3 tests/test_eval_corpus.py

Covers pass-span pairing (incl. a killed span with no 'done'), transcript -> channel_turn
joining (considered reply excludes intent pre-acks), recent-outbound-by-market, and the
secret-discipline invariant: no floor value can appear in any record (records carry message
text + non-secret signals only).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import eval_corpus  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_parse_pass_log():
    print("pass-log span pairing:")
    text = (
        "=== 2026-06-25T09:00:00Z buyer pass (claude-code) ===\n"
        "Error: Reached max turns (14)\n"
        "=== 2026-06-25T09:05:00Z buyer pass done rc=1 ===\n"
        "=== 2026-06-25T09:08:00Z maint pass (claude-code) ===\n"
        "Done one step.\n"
        "=== 2026-06-25T09:16:00Z maint pass done rc=0 ===\n"
        "=== 2026-06-25T09:20:00Z buy pass (claude-code) ===\n"  # killed: no done line
    )
    recs = eval_corpus.parse_pass_log(text)
    check("three spans parsed", len(recs) == 3)
    check("rc captured", recs[0].rc == 1 and recs[1].rc == 0)
    check("narrative captured", "Reached max turns" in recs[0].narrative)
    check("killed span has rc=None", recs[2].rc is None and recs[2].pass_mode == "buy")


def test_parse_transcript():
    print("transcript -> channel_turn join:")
    turns = [
        {"ts": 1, "dir": "out", "kind": "say", "text": "Two things in flight", "tag": "enumerated-tasks"},
        {"ts": 2, "dir": "in", "kind": "text", "text": "do all tasks", "tag": None},
        {"ts": 3, "dir": "out", "kind": "intent", "text": "Let me check what needs doing", "tag": None},
        {"ts": 4, "dir": "out", "kind": "say", "text": "On it, running both", "tag": None},
    ]
    recs = eval_corpus.parse_transcript(turns)
    check("one channel_turn per user turn", len(recs) == 1)
    r = recs[0]
    check("user text captured", r.user_said == "do all tasks")
    check("prior enumerated turn captured", r.prior_tag == "enumerated-tasks")
    check("considered reply EXCLUDES intent pre-ack",
          "running both" in r.agent_considered and "Let me check" not in r.agent_considered)


def test_outbound_by_market():
    print("recent outbound by market + leak-scan texts:")
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    threads = [
        {"thread_id": "fb:erric-1", "transcript": [
            {"dir": "out", "text": "old reply", "ts": "2026-06-20T00:00:00+08:00"}]},   # stale
        {"thread_id": "carousell:9", "transcript": [
            {"dir": "in", "text": "still there?", "ts": "2026-06-25T10:00:00+08:00"},
            {"dir": "out", "text": "yes!", "ts": "2026-06-25T11:00:00+08:00"}]},          # recent
    ]
    counts, texts = eval_corpus.outbound_by_market(threads, now, lookback_hours=24)
    check("stale fb outbound NOT counted recent", counts.get("fb", 0) == 0)
    check("recent carousell outbound counted", counts.get("carousell") == 1)
    check("all outbound texts collected for leak scan", set(texts) == {"old reply", "yes!"})


def test_no_secret_in_records():
    print("secret-discipline invariant (no floor value in any record):")
    # A floor value (e.g. 285) lives only in data/floors and must never enter the corpus. The
    # transcript records only message text, so even a thread mentioning a number keeps it as a
    # quoted offer, never as a secret field. Assert no record field is a bare secret store.
    turns = [{"ts": 1, "dir": "in", "kind": "text", "text": "buyer offered 285", "tag": None}]
    recs = eval_corpus.parse_transcript(turns)
    blob = " ".join(str(v) for r in recs for v in r.to_dict().values())
    check("record has no 'floor' field key", "floor" not in recs[0].to_dict())
    check("message text preserved as-is (number is the buyer's offer, not a secret store)",
          "285" in blob)


def test_peek_counts():
    print("peek-count normalization:")
    check("nested {count} shape read", eval_corpus._peek_counts(
        {"fb": {"count": 20, "snippet": ""}, "carousell": {"count": 7}}) == {"fb": 20, "carousell": 7})
    check("bool ignored, int kept", eval_corpus._peek_counts({"x": True, "y": 3}) == {"y": 3})


if __name__ == "__main__":
    print("eval_corpus.py tests\n")
    test_parse_pass_log()
    test_parse_transcript()
    test_outbound_by_market()
    test_no_secret_in_records()
    test_peek_counts()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
