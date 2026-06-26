#!/usr/bin/env python3
"""Tests for inbox_detect.py — the inbox-sweep takeover core.

    python3 tests/test_inbox_detect.py

Pure logic (classify / untracked_rows / union_enabled / declined-set) is tested inline;
CLI checks exercise the real files and assert no floor/budget/address leak in the output.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import inbox_detect  # noqa: E402

NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
SEEN_F = ROOT / "data" / "takeover_seen.json"

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


# ---------------------------------------------------------------- classify

def test_classify_direction():
    print("classify keys on the FIRST in/out message:")
    out_first = [{"msg_id": "1", "dir": "out", "text": "Hi, is this available?"},
                 {"msg_id": "2", "dir": "in", "text": "Yes!"}]
    in_first = [{"msg_id": "1", "dir": "in", "text": "Interested in your phone"},
                {"msg_id": "2", "dir": "out", "text": "Sure"}]
    check("user spoke first -> buyer_initiated", inbox_detect.classify(out_first) == "buyer_initiated")
    check("seller spoke first -> seller_initiated", inbox_detect.classify(in_first) == "seller_initiated")
    check("empty transcript -> empty", inbox_detect.classify([]) == "empty")
    check("None -> empty", inbox_detect.classify(None) == "empty")


def test_classify_ignores_noise():
    print("classify ignores rows with no usable direction:")
    noisy = [{"msg_id": "0", "text": "system"}, {"dir": "system"},
             {"msg_id": "1", "dir": "out", "text": "hello"}]
    check("first real dir (out) decides", inbox_detect.classify(noisy) == "buyer_initiated")


# ---------------------------------------------------------------- untracked_rows (diff)

def test_diff_excludes_tracked_and_declined():
    print("untracked_rows drops tracked + declined, keeps the genuinely new:")
    rows = [{"thread_id": "111", "unread": True},   # new
            {"thread_id": "222", "unread": True},   # tracked
            {"thread_id": "333", "unread": True},   # declined
            {"thread_id": "", "unread": True}]      # junk
    tracked = {"carousell:222"}
    declined = {"carousell:333"}
    out = inbox_detect.untracked_rows("carousell", rows, tracked, declined)
    ids = [r["tid"] for r in out]
    check("only the new thread surfaces", ids == ["carousell:111"])
    check("tid is namespaced", out[0]["tid"] == "carousell:111")
    check("row fields preserved", out[0]["unread"] is True)


def test_diff_is_immutable():
    print("untracked_rows never mutates the input rows:")
    rows = [{"thread_id": "111"}]
    _ = inbox_detect.untracked_rows("fb", rows, set(), set())
    check("input row untouched (no tid added in place)", "tid" not in rows[0])


# ---------------------------------------------------------------- union + declined-set

def test_union_enabled():
    print("union_enabled merges both side configs, seller order first:")
    seller = {"carousell": {"enabled": True}, "ebay": {"enabled": False}}
    buyer = {"fb": {"enabled": True}, "carousell": {"enabled": True}}
    union = inbox_detect.union_enabled(seller, buyer)
    check("carousell once, ebay dropped (disabled), fb added", union == ["carousell", "fb"])
    check("array form tolerated", inbox_detect.union_enabled(["fb"], None) == ["fb"])


def test_declined_set_and_decision_immutable():
    print("declined_set reads any decided thread; with_decision is immutable:")
    seen = {"carousell:1": {"decision": "declined", "side": "buy"},
            "carousell:2": {"decision": "managed", "side": "sell"}}
    ds = inbox_detect.declined_set(seen)
    check("both declined and managed suppress", ds == {"carousell:1", "carousell:2"})
    updated = inbox_detect.with_decision(seen, "fb:9", "declined", "buy", NOW.isoformat())
    check("original seen unchanged", "fb:9" not in seen)
    check("new entry recorded", updated["fb:9"]["decision"] == "declined")
    check("ts stamped", updated["fb:9"]["ts"] == NOW.isoformat())


# ---------------------------------------------------------------- CLI

def cli(*a):
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "inbox_detect.py"), *a],
                       capture_output=True, text=True, input=a and "" or "")
    return p


def cli_stdin(stdin_text, *a):
    p = subprocess.run([sys.executable, str(ROOT / "bin" / "inbox_detect.py"), *a],
                       capture_output=True, text=True, input=stdin_text)
    return p


def test_cli_classify_stdin():
    print("CLI classify reads a transcript from stdin:")
    payload = json.dumps([{"msg_id": "1", "dir": "out", "text": "hi"}])
    p = cli_stdin(payload, "classify", "--thread", "-")
    ok = p.returncode == 0 and json.loads(p.stdout).get("direction") == "buyer_initiated"
    check("classify --thread - -> buyer_initiated", ok)


def test_cli_decline_roundtrip_no_leak():
    print("CLI decline then declined round-trips and leaks no secrets:")
    existed = SEEN_F.exists()
    backup = SEEN_F.read_text() if existed else None
    try:
        d = cli("decline", "--thread", "__test_inbox__:42", "--side", "buy", "--now", NOW.isoformat())
        q = cli("declined", "--thread", "__test_inbox__:42")
        ok = d.returncode == 0 and q.returncode == 0
        ok = ok and json.loads(q.stdout).get("declined") is True
        blob = (d.stdout + d.stderr + q.stdout + q.stderr).lower()
        ok = ok and not any(t in blob for t in ("max_budget", "floor", "000000", "sample road"))
        check("declined reflects the prior decline, no secret leak", ok)
    finally:
        if backup is not None:
            SEEN_F.write_text(backup)
        elif SEEN_F.exists():
            SEEN_F.unlink()


def test_cli_bad_input():
    print("CLI rejects a bad subcommand and an unparseable --now:")
    ok = True
    for args in (["bogus"], ["due", "--now", "not-a-date"]):
        p = cli(*args)
        if p.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed input exits nonzero", ok)


if __name__ == "__main__":
    print("inbox_detect tests\n")
    test_classify_direction()
    test_classify_ignores_noise()
    test_diff_excludes_tracked_and_declined()
    test_diff_is_immutable()
    test_union_enabled()
    test_declined_set_and_decision_immutable()
    test_cli_classify_stdin()
    test_cli_decline_roundtrip_no_leak()
    test_cli_bad_input()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
