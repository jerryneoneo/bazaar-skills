#!/usr/bin/env python3
"""imessage.py — iMessage transport shim (a SellerChannel adapter, macOS only).

A dumb pipe like telegram.py: it reads inbound messages from the seller's Messages database
(~/Library/Messages/chat.db, read-only) and sends outbound via AppleScript (osascript). It never
decides anything — the flow specs in skills/channel/*.md hold all logic.

The seller designates a control "handle" (a phone number or Apple ID email) at onboarding; it is
stored as a NON-SECRET id in seller_config.json -> channel.detail.handle and passed via --handle.
Inbound = messages from that handle (is_from_me = 0); outbound = AppleScript send to that handle.

No secrets are involved (iMessage auth is the macOS login itself). Reading chat.db requires the
host process to have Full Disk Access (TCC) — `detect` distinguishes "no iMessage" from
"present but FDA not granted".

State (per-adapter cursor) lives in data/channel_state.json under the "imessage" key, alongside
telegram's flat keys — only the bound adapter's cursor advances.

Subcommands:
  detect                       -> {"available": bool, "evidence": str, "hint": str}
  send    --text T --handle H [--options k=Label,...] [--ref R]
                               -> {"ok": true}    (options render as a numbered text list)
  poll    --handle H           -> {"events":[{event_id,kind,text,payload,ts}], "new_cursor": <rowid>}
  peek    --handle H           -> {"pending": <int>, "latest_text": str}   (non-consuming)
  getfile --rowid N --dest PATH -> {"path": PATH}   (copy an inbound attachment)
  typing  --handle H           -> {"ok": true}      (no-op; iMessage has no programmatic typing)

Exit: 0 ok · 2 bad input · 3 operational error.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "channel_state.json"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
ATTACHMENTS = Path.home() / "Library" / "Messages" / "Attachments"
ADAPTER_KEY = "imessage"


class ShimError(Exception):
    """Operational error -> exit 3."""


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def adapter_cursor(state: dict) -> int:
    return int(state.get(ADAPTER_KEY, {}).get("rowid", 0))


def set_adapter_cursor(state: dict, rowid: int) -> None:
    section = dict(state.get(ADAPTER_KEY, {}))
    section["rowid"] = rowid
    state[ADAPTER_KEY] = section


def open_db() -> sqlite3.Connection:
    """Open chat.db read-only. Raises ShimError with a TCC-aware hint on permission failure."""
    if not CHAT_DB.exists():
        raise ShimError("chat.db not found — Messages not set up on this Mac")
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.Error as exc:
        msg = str(exc).lower()
        if any(s in msg for s in ("permission", "unable to open", "authoriz", "denied")):
            raise ShimError(
                "cannot read chat.db (authorization denied) — grant Full Disk Access to "
                "this app in System Settings > Privacy & Security > Full Disk Access"
            ) from None
        raise ShimError(f"chat.db open failed: {exc}") from None


_TEXT_RE = re.compile(rb"NSString[^\x00]*?\x00.?.?(?P<t>[^\x86\x84]+)")


def _decode_body(text, attributed) -> str:
    """Prefer the plain text column; fall back to a best-effort scrape of attributedBody."""
    if text:
        return text
    if not attributed:
        return ""
    match = _TEXT_RE.search(attributed)
    if match:
        try:
            return match.group("t").split(b"\x86")[0].decode("utf-8", "ignore").strip()
        except Exception:  # noqa: BLE001 - best-effort decode, never fatal
            return ""
    return ""


def _fetch_inbound(conn: sqlite3.Connection, handle: str, after_rowid: int) -> list[dict]:
    """Inbound messages (is_from_me = 0) from `handle`, newer than after_rowid, oldest first."""
    rows = conn.execute(
        """
        SELECT m.ROWID, m.text, m.attributedBody, m.date, m.cache_has_attachments
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE h.id = ? AND m.is_from_me = 0 AND m.ROWID > ?
        ORDER BY m.ROWID ASC
        """,
        (handle, after_rowid),
    ).fetchall()
    events = []
    for rowid, text, attributed, date, has_attach in rows:
        body = _decode_body(text, attributed)
        if has_attach and not body:
            kind, payload = "photo", {"rowid": rowid}
        else:
            kind = "command" if body.startswith("/") else "text"
            payload = {}
        events.append({"event_id": rowid, "kind": kind, "text": body,
                       "payload": payload, "ts": date})
    return events


def cmd_detect(ns) -> int:
    if sys.platform != "darwin":
        print(json.dumps({"available": False, "evidence": "not macOS",
                          "hint": "iMessage is macOS-only"}))
        return 0
    try:
        open_db().close()
        print(json.dumps({"available": True, "evidence": "chat.db readable", "hint": ""}))
    except ShimError as exc:
        print(json.dumps({"available": False, "evidence": "chat.db present" if CHAT_DB.exists()
                          else "no chat.db", "hint": str(exc)}))
    return 0


def _osascript_send(handle: str, text: str) -> None:
    """Send an iMessage to `handle` via Messages.app. Raises ShimError on failure."""
    script = (
        'on run {targetHandle, msg}\n'
        '  tell application "Messages"\n'
        '    set targetService to 1st account whose service type = iMessage\n'
        '    set targetBuddy to participant targetHandle of targetService\n'
        '    send msg to targetBuddy\n'
        '  end tell\n'
        'end run'
    )
    try:
        proc = subprocess.run(["osascript", "-e", script, handle, text],
                              capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise ShimError(f"osascript send failed: {exc}") from None
    if proc.returncode != 0:
        err = proc.stderr.strip().lower()
        if "not authorized" in err or "1743" in err or "assistive" in err:
            raise ShimError("not authorized to control Messages — grant Automation permission "
                            "(System Settings > Privacy & Security > Automation)")
        raise ShimError(f"osascript send failed: {proc.stderr.strip()}")


def _render(text: str, options: str) -> str:
    """No inline buttons on iMessage: append options as a numbered list the seller replies to."""
    if not options:
        return text
    lines = [text, ""]
    for i, pair in enumerate(options.split(","), 1):
        label = pair.split("=", 1)[1] if "=" in pair else pair
        lines.append(f"{i}. {label.strip()}")
    lines.append("\n(reply with the number or the text)")
    return "\n".join(lines)


def cmd_send(ns) -> int:
    if not ns.handle:
        raise ShimError("--handle required (the seller's iMessage control handle)")
    _osascript_send(ns.handle, _render(ns.text, ns.options))
    print(json.dumps({"ok": True}))
    return 0


def cmd_poll(ns) -> int:
    state = load_state()
    conn = open_db()
    try:
        events = _fetch_inbound(conn, ns.handle, adapter_cursor(state))
    finally:
        conn.close()
    if events:
        set_adapter_cursor(state, events[-1]["event_id"])
        save_state(state)
    print(json.dumps({"events": events, "new_cursor": adapter_cursor(state)}))
    return 0


def cmd_peek(ns) -> int:
    """Non-consuming: count inbound past the cursor; never advance it."""
    state = load_state()
    conn = open_db()
    try:
        events = _fetch_inbound(conn, ns.handle, adapter_cursor(state))
    finally:
        conn.close()
    latest = next((e["text"] for e in reversed(events) if e["text"]), "")
    print(json.dumps({"pending": len(events), "latest_text": latest}))  # state NOT saved
    return 0


def cmd_getfile(ns) -> int:
    """Copy the newest attachment of an inbound message (by ROWID) to dest."""
    conn = open_db()
    try:
        row = conn.execute(
            """
            SELECT a.filename FROM attachment a
            JOIN message_attachment_join j ON j.attachment_id = a.ROWID
            WHERE j.message_id = ? ORDER BY a.ROWID DESC LIMIT 1
            """,
            (ns.rowid,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise ShimError("no attachment found for that message")
    src = Path(row[0].replace("~", str(Path.home()), 1)) if row[0].startswith("~") else Path(row[0])
    if not src.exists():
        raise ShimError(f"attachment file missing: {src}")
    dest = Path(ns.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    print(json.dumps({"path": str(dest)}))
    return 0


def cmd_typing(ns) -> int:
    print(json.dumps({"ok": True}))  # iMessage has no programmatic typing indicator
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imessage.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect").set_defaults(func=cmd_detect)
    s = sub.add_parser("send")
    s.add_argument("--text", required=True)
    s.add_argument("--handle", required=True)
    s.add_argument("--options", default="")
    s.add_argument("--ref", default="")
    s.set_defaults(func=cmd_send)
    po = sub.add_parser("poll")
    po.add_argument("--handle", required=True)
    po.set_defaults(func=cmd_poll)
    pk = sub.add_parser("peek")
    pk.add_argument("--handle", required=True)
    pk.set_defaults(func=cmd_peek)
    g = sub.add_parser("getfile")
    g.add_argument("--rowid", type=int, required=True)
    g.add_argument("--dest", required=True)
    g.set_defaults(func=cmd_getfile)
    ty = sub.add_parser("typing")
    ty.add_argument("--handle", default="")
    ty.set_defaults(func=cmd_typing)
    return p


def main(argv) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(argv[1:])
    except SystemExit:
        return 2
    try:
        return ns.func(ns)
    except ShimError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
