#!/usr/bin/env python3
"""telegram.py — Telegram Bot API transport shim (the first SellerChannel adapter).

A dumb pipe, not a brain: it sends/receives over https://api.telegram.org/bot<token>/<METHOD>
and never decides anything (mirrors floor_gate's "told what to say, not why"). The flow specs
in skills/channel/*.md hold all logic.

Token from env TELEGRAM_BOT_TOKEN — never printed, never written to disk.
State (offset cursor + authorized chat_id) in data/channel_state.json.

Subcommands:
  send    --text T [--options k=Label,k2=Label2] [--ref R] [--chat-id N]
          -> {"message_id": <int>}                      (sendMessage [+ inline keyboard])
  poll    [--timeout 25]
          -> {"events":[{event_id,kind,text,payload,ts}], "new_cursor": <int>}
          getUpdates(offset = max update_id + 1); calling with the new offset acks old
          updates (idempotency). Captures chat_id on first /start. answerCallbackQuery clears
          button spinners. Ignores updates from any chat other than the authorized one.
  getfile --file-id F --dest PATH
          -> {"path": PATH}                              (getFile + download)

Exit: 0 ok · 2 bad input · 3 token/state/API error.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import channel_log  # journals every send/poll for short-term conversational memory
except ImportError:  # transcript is best-effort; the channel must work without it
    channel_log = None

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "channel_state.json"
API_BASE = "https://api.telegram.org"
DEFAULT_STATE = {"adapter": "telegram", "chat_id": None, "update_offset": 0,
                 "pending": [], "last_seen_ts": None}


class ShimError(Exception):
    """Operational error -> exit 3. Message must never contain the token."""


def get_token():
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ShimError("TELEGRAM_BOT_TOKEN not set in environment")
    return token


def load_state():
    if not STATE_PATH.exists():
        return dict(DEFAULT_STATE)
    state = json.loads(STATE_PATH.read_text())
    return {**DEFAULT_STATE, **state}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def api(method, params, token):
    """POST JSON to a Bot API method. Raises ShimError (token-free) on failure."""
    url = f"{API_BASE}/bot{token}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Strip any echo of the URL (which contains the token) from error text.
        raise ShimError(f"Bot API {method} HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ShimError(f"Bot API {method} network error: {exc.reason}") from None
    if not payload.get("ok"):
        raise ShimError(f"Bot API {method} returned error: {payload.get('description','?')}")
    return payload["result"]


def build_keyboard(options, ref):
    """options 'k=Label,k2=Label2' -> InlineKeyboardMarkup; callback_data carries ref:key."""
    buttons = []
    for pair in options.split(","):
        if "=" not in pair:
            raise ValueError(f"bad option {pair!r} (expected key=Label)")
        key, label = pair.split("=", 1)
        cb = f"{ref}:{key}" if ref else key
        if len(cb.encode()) > 64:
            raise ValueError("callback_data exceeds 64 bytes")
        buttons.append([{"text": label.strip(), "callback_data": cb}])
    return {"inline_keyboard": buttons}


def cmd_send(ns):
    token = get_token()
    state = load_state()
    chat_id = ns.chat_id or state.get("chat_id")
    if not chat_id:
        raise ShimError("no chat_id yet — seller must /start the bot first")
    params = {"chat_id": chat_id, "text": ns.text}
    if ns.options:
        params["reply_markup"] = build_keyboard(ns.options, ns.ref)
    result = api("sendMessage", params, token)
    # Journal the outbound turn only after a successful send (never log a phantom message).
    if channel_log:
        channel_log.append_turn("out", ns.kind, ns.text, tag=(ns.tag or None))
    print(json.dumps({"message_id": result.get("message_id")}))
    return 0


def _normalize(update, authorized_chat):
    """Turn one Telegram update into our event dict, or None if it's not from the seller."""
    if "callback_query" in update:
        cq = update["callback_query"]
        chat = cq.get("message", {}).get("chat", {}).get("id")
        if authorized_chat and chat != authorized_chat:
            return None, None
        data = cq.get("data", "")
        ref, choice = (data.split(":", 1) if ":" in data else (None, data))
        return {
            "event_id": update["update_id"],
            "kind": "action",
            "text": choice,
            "payload": {"ref": ref, "choice": choice, "callback_query_id": cq.get("id")},
            "ts": cq.get("message", {}).get("date"),
        }, chat
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None, None
    chat = msg.get("chat", {}).get("id")
    if authorized_chat and chat != authorized_chat:
        return None, None
    if "photo" in msg:
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        return {"event_id": update["update_id"], "kind": "photo", "text": msg.get("caption", ""),
                "payload": {"file_id": largest["file_id"]}, "ts": msg.get("date")}, chat
    text = msg.get("text", "")
    kind = "command" if text.startswith("/") else "text"
    return {"event_id": update["update_id"], "kind": kind, "text": text,
            "payload": {}, "ts": msg.get("date")}, chat


def cmd_poll(ns):
    token = get_token()
    state = load_state()
    offset = state.get("update_offset", 0)
    updates = api("getUpdates", {"offset": offset, "timeout": ns.timeout,
                                 "allowed_updates": ["message", "callback_query"]}, token)
    events = []
    max_id = offset - 1
    for upd in updates:
        max_id = max(max_id, upd["update_id"])
        # First contact from anyone captures the authorized chat (single-tenant).
        if not state.get("chat_id"):
            msg = upd.get("message") or upd.get("callback_query", {}).get("message")
            if msg:
                state["chat_id"] = msg.get("chat", {}).get("id")
        event, _chat = _normalize(upd, state.get("chat_id"))
        if event is None:
            continue
        # Acknowledge button taps so Telegram stops showing a spinner.
        if event["kind"] == "action" and event["payload"].get("callback_query_id"):
            try:
                api("answerCallbackQuery",
                    {"callback_query_id": event["payload"]["callback_query_id"]}, token)
            except ShimError:
                pass  # non-fatal
        event["payload"].pop("callback_query_id", None)
        if event["ts"]:
            state["last_seen_ts"] = event["ts"]
        events.append(event)
        # Journal the inbound turn here (inside the loop, before save_state): the offset
        # advances atomically with this poll, so each event is logged exactly once.
        if channel_log:
            channel_log.append_event(event)
    if updates:
        state["update_offset"] = max_id + 1  # calling getUpdates with this acks the batch
    save_state(state)
    print(json.dumps({"events": events, "new_cursor": state["update_offset"]}))
    return 0


def cmd_typing(ns):
    """Show the native 'Bot is typing…' indicator (lasts ~5s; re-send to keep alive)."""
    token = get_token()
    state = load_state()
    chat_id = ns.chat_id or state.get("chat_id")
    if not chat_id:
        raise ShimError("no chat_id yet — seller must /start the bot first")
    api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, token)
    print(json.dumps({"ok": True}))
    return 0


def cmd_peek(ns):
    """Non-consuming check: are there updates waiting? Does NOT advance the offset or ack
    callbacks — the daemon uses this to decide whether to invoke the (consuming) processing
    pass, so events aren't stolen before the brain handles them."""
    token = get_token()
    state = load_state()
    updates = api("getUpdates", {"offset": state.get("update_offset", 0),
                                 "timeout": ns.timeout,
                                 "allowed_updates": ["message", "callback_query"]}, token)
    pending = 0
    latest_text = ""
    for upd in updates:
        # Count only updates from the authorized chat (or any, if not captured yet).
        event, _chat = _normalize(upd, state.get("chat_id"))
        if event is not None:
            pending += 1
            text = event.get("text") or ("[sent photos]" if event["kind"] == "photo" else "")
            if text:
                latest_text = text          # newest textful message → seeds the intent line
        elif not state.get("chat_id"):
            pending += 1
    print(json.dumps({"pending": pending, "latest_text": latest_text}))  # state NOT saved
    return 0


def cmd_getfile(ns):
    token = get_token()
    result = api("getFile", {"file_id": ns.file_id}, token)
    file_path = result.get("file_path")
    if not file_path:
        raise ShimError("getFile returned no file_path")
    dest = Path(ns.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{API_BASE}/file/bot{token}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            dest.write_bytes(resp.read())
    except (urllib.error.URLError, TimeoutError):
        raise ShimError("file download failed") from None
    print(json.dumps({"path": str(dest)}))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="telegram.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--text", required=True)
    s.add_argument("--options", default="")
    s.add_argument("--ref", default="")
    s.add_argument("--chat-id", type=int, default=None, dest="chat_id")
    # Transcript metadata (safe defaults so existing `telegram.py send` calls are unchanged):
    # --kind names the channel verb; --tag optionally marks a turn to refer back to later
    # (e.g. enumerated-tasks) so a follow-up like "do all" resolves against it.
    s.add_argument("--kind", default="say")
    s.add_argument("--tag", default="")
    s.set_defaults(func=cmd_send)
    po = sub.add_parser("poll")
    po.add_argument("--timeout", type=int, default=25)
    po.set_defaults(func=cmd_poll)
    pk = sub.add_parser("peek")
    pk.add_argument("--timeout", type=int, default=25)
    pk.set_defaults(func=cmd_peek)
    ty = sub.add_parser("typing")
    ty.add_argument("--chat-id", type=int, default=None, dest="chat_id")
    ty.set_defaults(func=cmd_typing)
    g = sub.add_parser("getfile")
    g.add_argument("--file-id", required=True, dest="file_id")
    g.add_argument("--dest", required=True)
    g.set_defaults(func=cmd_getfile)
    return p


def main(argv):
    parser = build_parser()
    try:
        ns = parser.parse_args(argv[1:])
    except SystemExit:
        return 2
    try:
        return ns.func(ns)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except ShimError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
