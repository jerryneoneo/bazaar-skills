#!/usr/bin/env python3
"""Structural tests for channel_log.py (no live channel).

    python3 tests/test_channel_log.py

Covers the transcript round-trip, tail bounding (turns AND chars), the tolerant reader
(a torn final line never breaks a read), the poll-event adapter, and the secret-scrubbing
net (a planted floor/budget value or address string never survives into the transcript).

Headline invariant: a floor/budget value or pickup address that somehow reaches message
text is redacted before it is ever written — the transcript is fed back into the prompt, so
it must carry no secret.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import channel_log  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _isolate(tmp):
    """Point the module at a scratch transcript + empty silos so tests never touch real data."""
    base = Path(tmp)
    channel_log.TRANSCRIPT_PATH = base / "channel_transcript.jsonl"
    channel_log.FLOORS_DIR = base / "floors"
    channel_log.BUDGETS_DIR = base / "budgets"
    channel_log.SELLER_CONFIG = base / "seller_config.json"
    channel_log.BUYER_CONFIG = base / "buyer_config.json"
    channel_log._secret_cache = None
    (base / "floors").mkdir(exist_ok=True)
    (base / "budgets").mkdir(exist_ok=True)


def test_roundtrip():
    print("append / read round-trip:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        channel_log.append_turn("in", "text", "do all tasks", ts=100)
        channel_log.append_turn("out", "say", "Two things in flight", tag="enumerated-tasks", ts=101)
        turns = channel_log.read_turns()
        check("two turns persisted", len(turns) == 2)
        check("dir/kind/text preserved", turns[0].dir == "in" and turns[0].text == "do all tasks")
        check("tag preserved on out turn", turns[1].tag == "enumerated-tasks")
        check("in turn has null tag", turns[0].tag is None)
        rendered = channel_log.render_tail()
        check("render shows tag", "out · enumerated-tasks" in rendered)
        check("render shows header", rendered.startswith("RECENT CONTROL-CHANNEL"))


def test_append_event():
    print("poll-event adapter:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        channel_log.append_event({"kind": "command", "text": "/status", "payload": {}, "ts": 5})
        channel_log.append_event({"kind": "photo", "text": "", "payload": {"file_id": "L"}, "ts": 6})
        channel_log.append_event({"kind": "action", "text": "accept", "payload": {}, "ts": 7})
        turns = channel_log.read_turns()
        check("command logged as inbound", turns[0].dir == "in" and turns[0].kind == "command")
        check("empty-caption photo -> [photo]", turns[1].text == "[photo]")
        check("action choice captured", turns[2].text == "accept")


def test_tail_bounds():
    print("tail bounding (turns AND chars):")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        for i in range(20):
            channel_log.append_turn("in", "text", f"msg {i}", ts=i)
        check("max_turns caps count", len(channel_log.tail(max_turns=5)) == 5)
        check("newest kept (oldest dropped)", channel_log.tail(max_turns=5)[-1].text == "msg 19")
        with tempfile.TemporaryDirectory() as tmp2:
            _isolate(tmp2)
            channel_log.append_turn("in", "text", "x" * 500, ts=1)
            channel_log.append_turn("in", "text", "y" * 500, ts=2)
            channel_log.append_turn("in", "text", "z" * 500, ts=3)
            # max_chars=600 must drop oldest until it fits, but never below one turn.
            kept = channel_log.tail(max_turns=12, max_chars=600)
            check("char budget trims oldest", len(kept) == 1 and kept[0].text.startswith("z"))


def test_tolerant_reader():
    print("tolerant reader (torn final line):")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        channel_log.append_turn("in", "text", "good line", ts=1)
        with channel_log.TRANSCRIPT_PATH.open("a") as f:
            f.write('{"ts": 2, "dir": "in", "kind": "text", "text": "tor')  # no newline, truncated
        turns = channel_log.read_turns()
        check("valid turn survives a torn trailing line", len(turns) == 1 and turns[0].text == "good line")
        check("empty/blank lines ignored", channel_log.append_turn("in", "text", "after", ts=3))


def test_scrub_secrets():
    print("secret-scrubbing net:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        (Path(tmp) / "floors" / "item.json").write_text(json.dumps(
            {"item_id": "item", "list_price": 400, "floor": 285}))
        (Path(tmp) / "budgets" / "want.json").write_text(json.dumps(
            {"want_id": "want", "target_price": 120, "max_budget": 175}))
        (Path(tmp) / "seller_config.json").write_text(json.dumps(
            {"origin": {"line1": "Blk 99 Sample Road, #12-34", "postcode": "000000"}}))
        channel_log._secret_cache = None
        check("floor value redacted", channel_log._scrub("my lowest is 285 firm") ==
              f"my lowest is {channel_log.REDACTED} firm")
        check("max budget redacted", channel_log.REDACTED in channel_log._scrub("budget 175"))
        check("address line redacted", channel_log.REDACTED in
              channel_log._scrub("pickup at Blk 99 Sample Road, #12-34"))
        check("public list_price NOT redacted", channel_log._scrub("listed at 400") == "listed at 400")
        check("digit-run boundary respected (285 not in 2850)",
              channel_log._scrub("2850") == "2850")
        # And the scrub is applied on the write path, not just in-memory.
        channel_log.append_turn("out", "say", "lowest 285", ts=9)
        check("written turn carries no secret",
              "285" not in channel_log.TRANSCRIPT_PATH.read_text())


def test_fail_open():
    print("fail-open contract:")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        check("bad direction returns False, no raise", channel_log.append_turn("sideways", "x", "y") is False)
        check("empty transcript renders to empty string", channel_log.render_tail() == "")


if __name__ == "__main__":
    print("channel_log.py structural tests\n")
    test_roundtrip()
    test_append_event()
    test_tail_bounds()
    test_tolerant_reader()
    test_scrub_secrets()
    test_fail_open()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
