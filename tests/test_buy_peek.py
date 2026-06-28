#!/usr/bin/env python3
"""Tests for buy_peek.peek()'s LIAISE gating: a liaising/agreed want now fires a buy pass ONLY when
a tracked buy thread has a fresh seller reply (inbox_scan.buy_pending), not every cycle. SEARCH-state
wants stay file-only.

    python3 tests/test_buy_peek.py

Claims:
  1. a liaising want with NO fresh reply → pending 0 (the fix: no more every-cycle buy pass).
  2. a liaising want WITH a fresh reply → pending 1, scoped to that want.
  3. a searching want → pending 1 from the file-only path (no inbox scan needed).
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import buy_peek  # noqa: E402
import inbox_scan  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _patch(tmp: Path, wants, buy_pending):
    """Point buy_peek at a tmp wants dir + empty session, and stub inbox_scan.buy_pending."""
    saved = (buy_peek.WANTS_DIR, buy_peek.BUY_SESSION_PATH, inbox_scan.buy_pending)
    for name, obj in wants.items():
        (tmp / name).write_text(json.dumps(obj))
    buy_peek.WANTS_DIR = tmp
    buy_peek.BUY_SESSION_PATH = tmp / "buy_session.json"  # absent → not blocked on user
    inbox_scan.buy_pending = lambda: dict(buy_pending)

    def restore():
        (buy_peek.WANTS_DIR, buy_peek.BUY_SESSION_PATH, inbox_scan.buy_pending) = saved
    return restore


def test_liaise_without_reply_is_idle():
    print("liaising want, no fresh reply → pending 0 (no every-cycle buy pass):")
    with tempfile.TemporaryDirectory() as td:
        restore = _patch(Path(td), {"w1.json": {"want_id": "w1", "status": "liaising"}},
                         {"pending": 0, "want_id": None, "latest_text": ""})
        try:
            out = buy_peek.peek()
            check("pending 0", out["pending"] == 0)
        finally:
            restore()


def test_liaise_with_reply_fires_scoped():
    print("liaising want with a fresh seller reply → pending 1 scoped to the want:")
    with tempfile.TemporaryDirectory() as td:
        restore = _patch(Path(td), {"w1.json": {"want_id": "w1", "status": "liaising"}},
                         {"pending": 1, "want_id": "w1", "thread_id": "carousell:1", "latest_text": "[maxlinda] $55"})
        try:
            out = buy_peek.peek()
            check("pending 1", out["pending"] == 1)
            check("scoped to want w1", out["want_id"] == "w1")
            check("carries reply hint", "maxlinda" in out["latest_text"])
        finally:
            restore()


def test_search_want_file_only():
    print("searching want → pending 1 from the file-only path (no inbox scan):")
    with tempfile.TemporaryDirectory() as td:
        # buy_pending stubbed to raise — proves the search path never touches the inbox scan
        def _boom():
            raise AssertionError("search path must not call inbox_scan.buy_pending")
        saved = (buy_peek.WANTS_DIR, buy_peek.BUY_SESSION_PATH, inbox_scan.buy_pending)
        (Path(td) / "w2.json").write_text(json.dumps({"want_id": "w2", "status": "searching"}))
        buy_peek.WANTS_DIR = Path(td)
        buy_peek.BUY_SESSION_PATH = Path(td) / "buy_session.json"
        inbox_scan.buy_pending = _boom
        try:
            out = buy_peek.peek()
            check("pending 1", out["pending"] == 1)
            check("scoped to want w2", out["want_id"] == "w2")
        finally:
            (buy_peek.WANTS_DIR, buy_peek.BUY_SESSION_PATH, inbox_scan.buy_pending) = saved


if __name__ == "__main__":
    print("buy_peek liaise-gating tests\n")
    test_liaise_without_reply_is_idle()
    test_liaise_with_reply_fires_scoped()
    test_search_want_file_only()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
