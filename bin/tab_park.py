#!/usr/bin/env python3
"""tab_park.py — keep notification-path tabs BACKGROUNDED in the warm Chrome (no LLM, ~0 tokens).

A Meta web app (Facebook / Instagram) only fires a READABLE OS push notification when its tab is
HIDDEN; a focused tab delivers in-app instead (verified on macOS 26). The buyer pass brings a
marketplace tab to the front to act, which would then suppress that market's notifications. Between
passes we therefore re-park a NON-notification tab to the front, so the notification-path tabs go
hidden again and keep pushing (and the resolver can light up their notification path).

Safe to call freely: the warm Chrome is a DEDICATED instance (its own --user-data-dir at
$SELLER_DIR/.browser-profile, see bin/chrome_debug.sh), so this never touches the user's own browser.
Poll-path markets (e.g. Carousell) are unaffected: their CDP reads work regardless of visibility, so
parking ON the Carousell tab is fine. Fail-open no-op on any CDP error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buyer_peek as bp  # noqa: E402  reuse the stdlib CDP transport (_MiniWS) + target listing

# Origins whose tabs must stay HIDDEN to keep firing OS push notifications (the notification path).
# Meta (FB/IG) is the proven push-capable family; extend as other push-capable markets are added.
NOTIF_ORIGINS = ("facebook.com", "messenger.com", "instagram.com")


def pick_parking(targets: list[dict], notif_origins=NOTIF_ORIGINS) -> dict | None:
    """PURE: pick a tab to bring to front so the notification-path tabs go hidden. Returns the chosen
    target, or None when every open tab is a notification-path tab (nothing safe to park on → caller
    no-ops and that market simply falls back to the poll path)."""
    return next((t for t in targets
                 if t.get("url") and not any(o in t["url"] for o in notif_origins)), None)


def needs_park(targets: list[dict], notif_origins=NOTIF_ORIGINS) -> bool:
    """PURE: True only when a notification-path (Meta) tab is the CURRENTLY FRONTMOST tab and so must
    be pushed to the background. CDP /json/list returns page targets most-recently-active first, so
    the first tab with a URL is the active one. Parking only when this is True keeps an idle warm
    Chrome (e.g. a lone poll-path Carousell tab) from being raised — and its window yanked to the OS
    foreground — every loop iteration. With no Meta tab in front there is nothing to hide, so no-op."""
    front = next((t["url"] for t in targets if t.get("url")), None)
    return front is not None and any(o in front for o in notif_origins)


def _cdp_call(ws_url: str, method: str, timeout: int = 5) -> None:
    """Send one CDP method (no params) and wait for its ack. Reuses buyer_peek's minimal WS client."""
    ws = bp._MiniWS(ws_url, timeout)
    try:
        ws.send_text(json.dumps({"id": 1, "method": method, "params": {}}))
        while True:
            opcode, data = ws.recv_frame()
            if opcode == 0x8:  # close
                return
            if opcode != 0x1:  # ignore non-text frames
                continue
            if json.loads(data.decode("utf-8", "replace")).get("id") == 1:
                return
    finally:
        ws.close()


def park(notif_origins=NOTIF_ORIGINS) -> bool:
    """Bring a non-notification tab to the front so notification-path tabs go hidden. Returns True if
    it parked, False on no-op (nothing to hide / no suitable tab) or any error. Never raises.

    No-ops unless a Meta tab is currently frontmost (needs_park): otherwise the warm Chrome window
    would be raised to the OS foreground every loop iteration even when there is nothing to hide."""
    try:
        targets = bp.list_page_targets()
        if not needs_park(targets, notif_origins):
            return False
        parking = pick_parking(targets, notif_origins)
        if not parking:
            return False
        _cdp_call(parking["webSocketDebuggerUrl"], "Page.bringToFront")
        return True
    except Exception:  # noqa: BLE001 — best-effort; a parking failure must never break the loop
        return False


def main(argv: list[str]) -> int:
    print(json.dumps({"parked": park()}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
