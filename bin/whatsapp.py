#!/usr/bin/env python3
"""whatsapp.py — WhatsApp transport shim (a SellerChannel adapter).

A dumb pipe like telegram.py: it sends over the WhatsApp Business Cloud API
(https://graph.facebook.com/v21.0/<phone_id>/messages) and reads inbound from a local webhook
cache (data/whatsapp_inbox.jsonl, appended by the seller's webhook receiver). It never decides
anything — the flow specs in skills/channel/*.md hold all logic.

Secrets from env, never printed or written to disk:
  WHATSAPP_TOKEN     — Cloud API access token (system-user token)
  WHATSAPP_PHONE_ID  — the business phone-number id (also stored, non-secret, in
                       seller_config.json -> channel.detail.phone_id)

Single-tenant: the authorized counterparty number is captured on first inbound and stored under
the "whatsapp" key in data/channel_state.json (per-adapter cursor lives there too); messages from
any other number are ignored.

If a WhatsApp MCP server is configured instead, `detect` reports it and the flow binds the MCP
tools directly (this shim is the Cloud-API path).

Subcommands:
  detect                        -> {"available": bool, "evidence": str, "hint": str}
  send    --text T [--to N] [--options k=Label,...] [--ref R]   -> {"ok": true}
          (options become interactive reply buttons; >3 options degrade to a numbered list)
  poll                          -> {"events":[...], "new_cursor": <msg_id>}
  peek                          -> {"pending": <int>, "latest_text": str}   (non-consuming)
  typing                        -> {"ok": true}   (no-op on Cloud API)

Exit: 0 ok · 2 bad input · 3 token/state/API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "channel_state.json"
INBOX_PATH = DATA_DIR / "whatsapp_inbox.jsonl"
GRAPH_BASE = "https://graph.facebook.com/v21.0"
ADAPTER_KEY = "whatsapp"


class ShimError(Exception):
    """Operational error -> exit 3. Message must never contain the token."""


def get_creds() -> tuple[str, str]:
    token = os.environ.get("WHATSAPP_TOKEN", "").strip()
    phone_id = os.environ.get("WHATSAPP_PHONE_ID", "").strip()
    if not token or not phone_id:
        raise ShimError("WHATSAPP_TOKEN / WHATSAPP_PHONE_ID not set in environment")
    return token, phone_id


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def section(state: dict) -> dict:
    return dict(state.get(ADAPTER_KEY, {}))


def graph_post(phone_id: str, payload: dict, token: str) -> dict:
    url = f"{GRAPH_BASE}/{phone_id}/messages"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ShimError(f"WhatsApp API HTTP {exc.code}") from None  # never echo the URL/token
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ShimError(f"WhatsApp API network error: {exc.reason}") from None


def _build_payload(to: str, text: str, options: str) -> dict:
    if not options:
        return {"messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"body": text}}
    pairs = [(p.split("=", 1) if "=" in p else (p, p)) for p in options.split(",")]
    if len(pairs) <= 3:
        # Interactive reply buttons (Cloud API caps at 3).
        buttons = [{"type": "reply", "reply": {"id": k.strip(), "title": v.strip()[:20]}}
                   for k, v in pairs]
        return {"messaging_product": "whatsapp", "to": to, "type": "interactive",
                "interactive": {"type": "button", "body": {"text": text},
                                "action": {"buttons": buttons}}}
    # >3 options: degrade to a numbered text list (parsed back by the flow).
    listed = text + "\n\n" + "\n".join(f"{i}. {v.strip()}" for i, (_, v) in enumerate(pairs, 1))
    listed += "\n\n(reply with the number)"
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": listed}}


def cmd_detect(ns) -> int:
    token = os.environ.get("WHATSAPP_TOKEN", "").strip()
    phone_id = os.environ.get("WHATSAPP_PHONE_ID", "").strip()
    if token and phone_id:
        print(json.dumps({"available": True, "evidence": "WA Business Cloud API creds present",
                          "hint": ""}))
    else:
        print(json.dumps({"available": False, "evidence": "no WA creds in env",
                          "hint": "set WHATSAPP_TOKEN + WHATSAPP_PHONE_ID, or configure a "
                                  "WhatsApp MCP server"}))
    return 0


def cmd_send(ns) -> int:
    token, phone_id = get_creds()
    state = load_state()
    to = ns.to or section(state).get("to")
    if not to:
        raise ShimError("no recipient yet — seller must message the WhatsApp number first")
    graph_post(phone_id, _build_payload(to, ns.text, ns.options), token)
    print(json.dumps({"ok": True}))
    return 0


def _read_inbox() -> list[dict]:
    """Inbound events the webhook receiver appended (one JSON object per line)."""
    if not INBOX_PATH.exists():
        return []
    events = []
    for line in INBOX_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # skip a malformed line, never crash the poll
    return events


def _new_events(state: dict) -> tuple[list[dict], dict]:
    """Events past the cursor, capturing the authorized number on first contact (single-tenant)."""
    sec = section(state)
    cursor = sec.get("msg_id")
    authorized = sec.get("to")
    raw = _read_inbox()
    out, seen_cursor = [], cursor is None
    for ev in raw:
        if not seen_cursor:
            if ev.get("id") == cursor:
                seen_cursor = True
            continue
        sender = ev.get("from")
        if authorized is None:
            authorized = sender
            sec["to"] = authorized
        if sender != authorized:
            continue
        text = ev.get("text", "")
        kind = "command" if text.startswith("/") else ev.get("kind", "text")
        out.append({"event_id": ev.get("id"), "kind": kind, "text": text,
                    "payload": ev.get("payload", {}), "ts": ev.get("ts")})
    if out:
        sec["msg_id"] = out[-1]["event_id"]
    state[ADAPTER_KEY] = sec
    return out, state


def cmd_poll(ns) -> int:
    state = load_state()
    events, state = _new_events(state)
    if events:
        save_state(state)
    print(json.dumps({"events": events, "new_cursor": section(state).get("msg_id")}))
    return 0


def cmd_peek(ns) -> int:
    state = load_state()
    events, _ = _new_events(dict(state))  # work on a copy; never persist
    latest = next((e["text"] for e in reversed(events) if e["text"]), "")
    print(json.dumps({"pending": len(events), "latest_text": latest}))
    return 0


def cmd_typing(ns) -> int:
    print(json.dumps({"ok": True}))  # Cloud API has no typing indicator
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whatsapp.py", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect").set_defaults(func=cmd_detect)
    s = sub.add_parser("send")
    s.add_argument("--text", required=True)
    s.add_argument("--to", default="")
    s.add_argument("--options", default="")
    s.add_argument("--ref", default="")
    s.set_defaults(func=cmd_send)
    sub.add_parser("poll").set_defaults(func=cmd_poll)
    sub.add_parser("peek").set_defaults(func=cmd_peek)
    sub.add_parser("typing").set_defaults(func=cmd_typing)
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
