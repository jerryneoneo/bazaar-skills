#!/usr/bin/env python3
"""notify_setup.py — set up / inspect the Instant-mode wake path (no LLM). macOS-only.

Instant mode = wake the agent from OS push notifications instead of polling. It needs two grants:
  1. Full Disk Access (FDA) on the daemon's Python, so it can READ the Notification Center DB.
  2. Chrome notification permission for the push-capable markets (Meta: Facebook / Instagram), so
     those sites actually post OS notifications.

This helper is used by the WAKE_SPEED onboarding step and `/selly -> speed`:
  status      -> {fda, python, instant_ready, markets:{...}}  (read-only detection)
  open-fda    -> open the System Settings Full Disk Access pane (the user toggles it; TCC can't be
                 granted programmatically)
  grant-chrome-> best-effort: grant Chrome notification permission for the markets' origins over CDP
                 (the user opted in via onboarding; falls back to a manual guide if blocked)

Everything is fail-open and never raises fatally; without macOS / FDA it simply reports not-ready.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buyer_peek as bp  # noqa: E402  CDP transport (_MiniWS, cdp_eval, list_page_targets)
import notify_db  # noqa: E402  FDA-gated Notification Center reader

# Push-capable markets and the origin substrings whose Chrome notification permission Instant needs.
PUSH_MARKETS: dict[str, list[str]] = {
    "fb": ["facebook.com", "messenger.com"],
    "ig": ["instagram.com"],
}
FDA_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def _cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: int = 6):
    """Send one CDP method and return its response object (or None). Reuses buyer_peek's WS client."""
    ws = bp._MiniWS(ws_url, timeout)
    try:
        ws.send_text(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            opcode, data = ws.recv_frame()
            if opcode == 0x8:
                return None
            if opcode != 0x1:
                continue
            obj = json.loads(data.decode("utf-8", "replace"))
            if obj.get("id") == 1:
                return obj
    finally:
        ws.close()


def _market_tab(market: str, targets: list[dict]) -> dict | None:
    return next((t for t in targets if any(o in (t.get("url") or "") for o in PUSH_MARKETS[market])), None)


def status() -> dict:
    """Read-only: is Instant mode ready? FDA + per-market Chrome notification permission + push sub."""
    fda = notify_db.available()
    targets = bp.list_page_targets()
    markets: dict[str, dict] = {}
    for market in PUSH_MARKETS:
        tab = _market_tab(market, targets)
        if not tab:
            markets[market] = {"tab_open": False}
            continue
        st = bp.cdp_eval(tab["webSocketDebuggerUrl"],
                         "(async()=>{try{const r=await navigator.serviceWorker.ready;"
                         "const s=await r.pushManager.getSubscription();"
                         "return{perm:Notification.permission,hasPushSub:!!s};}"
                         "catch(e){return{perm:Notification.permission,hasPushSub:false};}})()") or {}
        markets[market] = {"tab_open": True, "permission": st.get("perm"),
                           "has_push_sub": bool(st.get("hasPushSub"))}
    ready = fda and any(m.get("permission") == "granted" and m.get("has_push_sub")
                        for m in markets.values())
    return {"fda": fda, "python": sys.executable, "instant_ready": ready, "markets": markets}


def open_fda() -> bool:
    """Open the System Settings Full Disk Access pane so the user can toggle it. (TCC is user-only.)"""
    try:
        subprocess.run(["open", FDA_PANE], capture_output=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def grant_chrome(markets: list[str] | None = None) -> dict:
    """Best-effort: grant Chrome notification permission for each market's origin over CDP, for tabs
    that are open. Returns {market: granted|skipped|error}. The user opted in via onboarding; if the
    harness blocks this persistent change, the caller falls back to guiding a manual grant."""
    targets = bp.list_page_targets()
    try:
        browser_ws = bp._http_get_json("/json/version", 4).get("webSocketDebuggerUrl")
    except (OSError, ValueError):
        return {"error": "cannot reach Chrome CDP"}
    out: dict[str, str] = {}
    for market in (markets or list(PUSH_MARKETS)):
        tab = _market_tab(market, targets)
        if not tab:
            out[market] = "no tab open"
            continue
        origin = "https://www." + PUSH_MARKETS[market][0]
        try:
            r = _cdp_call(browser_ws, "Browser.grantPermissions",
                          {"origin": origin, "permissions": ["notifications"]})
            out[market] = "granted" if r and "error" not in r else "error"
        except (OSError, ValueError):
            out[market] = "error"
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "open-fda":
        print(json.dumps({"opened": open_fda()}))
    elif cmd == "grant-chrome":
        print(json.dumps(grant_chrome(argv[2:] or None)))
    else:
        print(json.dumps(status()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
