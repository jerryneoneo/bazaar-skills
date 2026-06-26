#!/usr/bin/env python3
"""Structural tests for whatsapp.py (no live Cloud API).

    python3 tests/test_whatsapp.py

Covers payload building (text / interactive buttons / numbered-list fallback), single-tenant inbound
capture + cursor, and token safety (missing creds fail cleanly; token never printed).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import whatsapp  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_payload_text():
    print("payload: plain text:")
    p = whatsapp._build_payload("123", "hello", "")
    check("type text", p["type"] == "text")
    check("body carried", p["text"]["body"] == "hello")


def test_payload_buttons():
    print("payload: <=3 options -> interactive buttons:")
    p = whatsapp._build_payload("123", "Pick", "accept=Accept,decline=Decline")
    check("type interactive", p["type"] == "interactive")
    btns = p["interactive"]["action"]["buttons"]
    check("two buttons", len(btns) == 2)
    check("button id is the option key", btns[0]["reply"]["id"] == "accept")


def test_payload_numbered():
    print("payload: >3 options -> numbered text list:")
    p = whatsapp._build_payload("123", "Pick", "a=A,b=B,c=C,d=D")
    check("falls back to text", p["type"] == "text")
    check("numbered list present", "1. A" in p["text"]["body"] and "4. D" in p["text"]["body"])


def test_single_tenant_cursor():
    print("single-tenant inbound + cursor (no disk):")
    saved = whatsapp._read_inbox
    whatsapp._read_inbox = lambda: [
        {"id": "m1", "from": "111", "text": "hi", "ts": 1},
        {"id": "m2", "from": "222", "text": "spam from someone else", "ts": 2},
        {"id": "m3", "from": "111", "text": "still me", "ts": 3},
    ]
    try:
        events, state = whatsapp._new_events({})
        froms = {e["text"] for e in events}
        check("captures first sender as authorized", whatsapp.section(state)["to"] == "111")
        check("keeps authorized sender's msgs", "hi" in froms and "still me" in froms)
        check("drops other senders", "spam from someone else" not in froms)
        check("cursor advances to last handled", whatsapp.section(state)["msg_id"] == "m3")
    finally:
        whatsapp._read_inbox = saved


def test_token_safety():
    print("token safety (missing creds, no leak):")
    env = {k: v for k, v in os.environ.items() if k not in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID")}
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "whatsapp.py"),
                           "send", "--text", "hi", "--to", "123"],
                          capture_output=True, text=True, env=env)
    check("missing creds exits 3", proc.returncode == 3)
    check("error names the env vars", "WHATSAPP_TOKEN" in proc.stderr)
    env2 = {**env, "WHATSAPP_TOKEN": "SECRETWA", "WHATSAPP_PHONE_ID": "999"}
    proc2 = subprocess.run([sys.executable, str(ROOT / "bin" / "whatsapp.py"), "bogus"],
                           capture_output=True, text=True, env=env2)
    check("bad subcommand exits 2", proc2.returncode == 2)
    check("token never printed", "SECRETWA" not in proc2.stdout + proc2.stderr)


if __name__ == "__main__":
    print("whatsapp.py structural tests\n")
    test_payload_text()
    test_payload_buttons()
    test_payload_numbered()
    test_single_tenant_cursor()
    test_token_safety()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
