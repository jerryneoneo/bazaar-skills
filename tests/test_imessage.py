#!/usr/bin/env python3
"""Structural tests for imessage.py (no live Messages/chat.db dependency for the pure logic).

    python3 tests/test_imessage.py

Covers the numbered-list option rendering (no inline buttons), attributedBody/text decoding,
per-adapter cursor namespacing in channel_state, and that detect()/peek degrade cleanly when
chat.db is unreadable (TCC) — never crashing, always an actionable hint.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import imessage  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_render_options():
    print("option rendering (numbered list, no buttons):")
    plain = imessage._render("Hello", "")
    check("no options -> text unchanged", plain == "Hello")
    listed = imessage._render("Pick one", "accept=Accept,decline=Decline")
    check("numbered 1.", "1. Accept" in listed)
    check("numbered 2.", "2. Decline" in listed)
    check("instructs how to reply", "number" in listed.lower())


def test_decode_body():
    print("message body decode:")
    check("plain text column wins", imessage._decode_body("hi there", b"ignored") == "hi there")
    check("no text, no body -> empty", imessage._decode_body(None, None) == "")
    blob = b"\x01\x02NSString\x01\x95\x84\x01+\x00\x06hello\x86\x84extra"
    decoded = imessage._decode_body(None, blob)
    check("attributedBody decode returns a str", isinstance(decoded, str))


def test_cursor_namespacing():
    print("per-adapter cursor namespacing in channel_state:")
    state = {"adapter": "telegram", "update_offset": 42, "chat_id": 7}  # telegram's flat keys
    check("default rowid 0", imessage.adapter_cursor(state) == 0)
    imessage.set_adapter_cursor(state, 99)
    check("writes under 'imessage' key", state["imessage"]["rowid"] == 99)
    check("does NOT touch telegram keys", state["update_offset"] == 42 and state["chat_id"] == 7)
    check("reads back", imessage.adapter_cursor(state) == 99)


def test_detect_graceful():
    print("detect() degrades cleanly (no crash, actionable hint):")
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "imessage.py"), "detect"],
                          capture_output=True, text=True)
    check("detect exits 0", proc.returncode == 0)
    out = json.loads(proc.stdout)
    check("reports availability bool", isinstance(out.get("available"), bool)) if proc.stdout else None
    # If unavailable, there must be an actionable hint (TCC / not macOS / no chat.db).
    if proc.stdout and not out["available"]:
        check("unavailable carries a hint", bool(out.get("hint")))


def test_send_requires_handle():
    print("send requires --handle:")
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "imessage.py"), "send", "--text", "hi"],
                          capture_output=True, text=True)
    check("missing --handle exits 2 (argparse)", proc.returncode == 2)


if __name__ == "__main__":
    print("imessage.py structural tests\n")
    test_render_options()
    test_decode_body()
    test_cursor_namespacing()
    test_detect_graceful()
    test_send_requires_handle()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
