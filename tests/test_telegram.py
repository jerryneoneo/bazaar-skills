#!/usr/bin/env python3
"""Structural tests for telegram.py (no live Bot API).

    python3 tests/test_telegram.py

Covers the pure logic — inline keyboard build, update->event normalization, single-tenant
filtering — plus token-safety (missing token fails cleanly, token never printed). The live
send/poll/getfile round-trip is verified separately once a bot token exists.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import telegram  # noqa: E402

_failures = []


class _NS:
    """Minimal argparse.Namespace stand-in for calling cmd_* directly."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _isolate(tmp):
    """Redirect telegram + channel_log state to a scratch dir so tests never touch real data."""
    base = Path(tmp)
    telegram.STATE_PATH = base / "channel_state.json"
    cl = telegram.channel_log
    cl.TRANSCRIPT_PATH = base / "channel_transcript.jsonl"
    cl.FLOORS_DIR, cl.BUDGETS_DIR = base / "floors", base / "budgets"
    cl.SELLER_CONFIG, cl.BUYER_CONFIG = base / "seller_config.json", base / "buyer_config.json"
    cl._secret_cache = None
    return cl


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_keyboard():
    print("inline keyboard build:")
    kb = telegram.build_keyboard("accept=Accept,counter=Counter,decline=Decline", "esc-7")
    rows = kb["inline_keyboard"]
    check("one button per option", len(rows) == 3)
    check("callback_data carries ref:key", rows[0][0]["callback_data"] == "esc-7:accept")
    check("label preserved", rows[1][0]["text"] == "Counter")
    try:
        telegram.build_keyboard("noeq", "")
        check("bad option rejected", False)
    except ValueError:
        check("bad option rejected", True)


def test_normalize():
    print("update -> event normalization (authorized chat = 100):")
    chat = 100
    cmd = telegram._normalize({"update_id": 1, "message": {"chat": {"id": 100},
                              "text": "/list", "date": 11}}, chat)[0]
    check("slash text -> command", cmd["kind"] == "command" and cmd["text"] == "/list")
    txt = telegram._normalize({"update_id": 2, "message": {"chat": {"id": 100},
                              "text": "hello", "date": 12}}, chat)[0]
    check("plain text -> text", txt["kind"] == "text")
    photo = telegram._normalize({"update_id": 3, "message": {"chat": {"id": 100}, "date": 13,
                                "photo": [{"file_id": "s", "file_size": 100},
                                          {"file_id": "L", "file_size": 9000}]}}, chat)[0]
    check("photo -> photo kind", photo["kind"] == "photo")
    check("picks largest photo", photo["payload"]["file_id"] == "L")
    action = telegram._normalize({"update_id": 4, "callback_query": {"id": "cb1", "data": "esc-7:counter",
                                 "message": {"chat": {"id": 100}, "date": 14}}}, chat)[0]
    check("callback -> action", action["kind"] == "action")
    check("action parses ref + choice",
          action["payload"]["ref"] == "esc-7" and action["payload"]["choice"] == "counter")


def test_single_tenant():
    print("single-tenant filtering:")
    ev, _ = telegram._normalize({"update_id": 9, "message": {"chat": {"id": 999},
                                "text": "hi", "date": 1}}, 100)
    check("foreign chat dropped", ev is None)


def test_token_safety():
    print("token safety (missing token, no leak):")
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "telegram.py"),
                           "send", "--text", "hi"], capture_output=True, text=True, env=env)
    check("missing token exits 3", proc.returncode == 3)
    check("error mentions env var, not a token value", "TELEGRAM_BOT_TOKEN" in proc.stderr)
    # Even with a fake token set, a bad subcommand must not echo the token anywhere.
    env["TELEGRAM_BOT_TOKEN"] = "123:SECRETTOKENVALUE"
    proc2 = subprocess.run([sys.executable, str(ROOT / "bin" / "telegram.py"), "bogus"],
                           capture_output=True, text=True, env=env)
    check("bad subcommand exits 2", proc2.returncode == 2)
    check("token never printed", "SECRETTOKENVALUE" not in proc2.stdout + proc2.stderr)


def test_send_logs_outbound():
    print("send journals one outbound turn (after a 200):")
    with tempfile.TemporaryDirectory() as tmp:
        cl = _isolate(tmp)
        telegram.save_state({**telegram.DEFAULT_STATE, "chat_id": 100})
        orig_api, orig_token = telegram.api, telegram.get_token
        telegram.api = lambda method, params, token: {"message_id": 42}
        telegram.get_token = lambda: "123:SECRETTOKENVALUE"
        try:
            telegram.cmd_send(_NS(chat_id=None, options="", ref="",
                                  text="Two things in flight", kind="say", tag="enumerated-tasks"))
        finally:
            telegram.api, telegram.get_token = orig_api, orig_token
        turns = cl.read_turns()
        check("exactly one outbound turn logged", len(turns) == 1 and turns[0].dir == "out")
        check("kind + tag captured", turns[0].kind == "say" and turns[0].tag == "enumerated-tasks")
        check("token never written to transcript",
              "SECRETTOKENVALUE" not in cl.TRANSCRIPT_PATH.read_text())


def test_poll_logs_each_event_once():
    print("poll journals each authorized event exactly once (idempotent on re-poll):")
    with tempfile.TemporaryDirectory() as tmp:
        cl = _isolate(tmp)
        telegram.save_state({**telegram.DEFAULT_STATE, "chat_id": 100})
        updates = [
            {"update_id": 10, "message": {"chat": {"id": 100}, "text": "do all tasks", "date": 1}},
            {"update_id": 11, "message": {"chat": {"id": 100}, "text": "/status", "date": 2}},
            {"update_id": 12, "message": {"chat": {"id": 999}, "text": "spam", "date": 3}},
        ]
        orig_api, orig_token = telegram.api, telegram.get_token
        telegram.get_token = lambda: "tok"
        telegram.api = lambda method, params, token: updates if method == "getUpdates" else {}
        try:
            telegram.cmd_poll(_NS(timeout=0))
            check("authorized events logged, foreign dropped", len(cl.read_turns()) == 2)
            check("inbound text captured", cl.read_turns()[0].text == "do all tasks")
            telegram.api = lambda method, params, token: [] if method == "getUpdates" else {}
            telegram.cmd_poll(_NS(timeout=0))
            check("re-poll with no new updates logs nothing new", len(cl.read_turns()) == 2)
        finally:
            telegram.api, telegram.get_token = orig_api, orig_token


def test_peek_logs_nothing():
    print("peek (non-consuming probe) journals nothing:")
    with tempfile.TemporaryDirectory() as tmp:
        cl = _isolate(tmp)
        telegram.save_state({**telegram.DEFAULT_STATE, "chat_id": 100})
        updates = [{"update_id": 20, "message": {"chat": {"id": 100}, "text": "hi", "date": 1}}]
        orig_api, orig_token = telegram.api, telegram.get_token
        telegram.get_token = lambda: "tok"
        telegram.api = lambda method, params, token: updates if method == "getUpdates" else {}
        try:
            telegram.cmd_peek(_NS(timeout=0))
            check("peek logs nothing", cl.read_turns() == [])
        finally:
            telegram.api, telegram.get_token = orig_api, orig_token


if __name__ == "__main__":
    print("telegram.py structural tests\n")
    test_keyboard()
    test_normalize()
    test_single_tenant()
    test_token_safety()
    test_send_logs_outbound()
    test_poll_logs_each_event_once()
    test_peek_logs_nothing()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
