#!/usr/bin/env python3
"""buyer_peek.py — cheap, non-LLM probe: is there a NEW buyer message? (~0 tokens, no LLM)

This is the buyer-side equivalent of the seller-side ``channel_peek`` in agent_daemon.py.
It lets the daemon GATE the expensive ``run_pass.sh buyer`` (a full multi-turn LLM browser
pass) so it fires only when a buyer has actually written — instead of every cycle.

Contract (mirrors the channel adapters' ``peek``):
    prints  {"pending": int, "latest_text": str, "markets": {<id>: {...}}}  to stdout, exit 0.

It reads the unread signal straight from the already-running warm CDP Chrome
(``bin/chrome_debug.sh`` on :9222) — pure stdlib, no Playwright, no LLM:
    • Facebook  → unread count is in the tab title, e.g. "(14) Facebook" (plain HTTP)
    • Carousell → unread badge in the inbox UI, read via CDP ``Runtime.evaluate``

It compares the live signal against a per-market memo in ``data/buyer_peek_state.json`` so a
reply that is still pending does NOT re-fire the pass every cycle. It NEVER advances the
authoritative per-thread cursors in ``data/threads/`` — the full buyer pass owns those and is
idempotent.

FAIL-OPEN-SAFE: on ANY error (Chrome down, DOM drift, parse failure) it prints
``{"pending": 0, ...}`` and exits 0. A broken probe therefore degrades to "nothing new"
rather than crashing the daemon; the daemon's periodic safety-net full pass covers the rare
miss. Selectors are kept in MARKET_PROBES so DOM drift is a one-line fix.

Usage:
    buyer_peek.py                      # probe enabled markets, print the peek JSON
    buyer_peek.py --no-memo            # ignore/!update memo (always report raw counts)
    buyer_peek.py eval <market> '<js>' # debug: run JS in a market's inbox tab, print result
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

SELLER_DIR = Path(__file__).resolve().parent.parent
SELLER_CONFIG_PATH = SELLER_DIR / "data" / "seller_config.json"
MEMO_PATH = SELLER_DIR / "data" / "buyer_peek_state.json"

CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
HTTP_TIMEOUT = 4      # seconds — listing CDP targets
WS_TIMEOUT = 6        # seconds — a single Runtime.evaluate round-trip
SNIPPET_MAX = 160     # cap snippet length kept in the memo / handed to the pass

# Per-marketplace unread probe. Kept here so DOM drift is a single-line fix (see FAIL-OPEN docs).
#   url_match : substrings that ALL must appear in a tab URL to identify that market's inbox tab
#   mode      : "title" → parse leading "(N)" from the tab title (no WebSocket needed)
#               "eval"  → run `js` via CDP Runtime.evaluate; js must return {count:int, snippet:str}
MARKET_PROBES: dict[str, dict] = {
    "fb": {
        # Match any FB/Messenger tab. The leading "(N) Facebook" title count is FB-GLOBAL (DMs +
        # notifications + marketplace), so it badly over-counts — a quiet marketplace inbox still
        # showed "(20)". When the tab is on the Marketplace inbox we instead count the conversation
        # ROWS that need a reply. CRITICAL: those rows are `div[role="row"]`, NOT anchors — FB shows
        # them with a status line "<buyer> is waiting for your response." / "<buyer> sent you a message
        # about your listing". An `a[href*="/t/"]` query matches hidden Messenger links and misses the
        # whole marketplace list (the bug that let FB selling enquiries pile up). Off-inbox → title.
        "url_match": ["facebook.com"],
        "url_match_alt": ["messenger.com"],
        "mode": "eval",
        "js": r"""(() => {
          try {
            let titleCount = 0;
            const tm = (document.title || '').match(/^\s*\((\d+)\+?\)/);
            if (tm) titleCount = parseInt(tm[1], 10);
            // On the Marketplace inbox, FB renders an aggregated "Marketplace … N new messages" row
            // (a [role=row]); N is the precise marketplace-unread count. The per-listing Selling rows
            // are NOT reliably CDP-selectable, and the [role=row] list is otherwise the general
            // Messenger "Chats" (Meta, etc.) — so we read the aggregate row, not individual threads.
            if (/\/marketplace\/inbox/.test(location.pathname)) {
              for (const r of Array.from(document.querySelectorAll('[role="row"]'))) {
                const t = (r.textContent || '').trim().replace(/\s+/g, ' ');
                const m = t.match(/Marketplace.*?(\d+)\s*new messages?/i);
                if (m) return { count: parseInt(m[1], 10), snippet: 'Marketplace: ' + m[1] + ' new messages' };
              }
              return { count: 0, snippet: '' };  // on the inbox, no aggregate row → no marketplace unread
            }
            // Any other FB/Messenger page → coarse global-title fallback (over-fires safe).
            return { count: titleCount, snippet: '' };
          } catch (e) { return { count: 0, snippet: '' }; }
        })()""",
    },
    "ebay": {
        # eBay's unread Messages count shows as a "(N)" badge in the page title on most eBay pages
        # and on the My eBay / Messages area. Match any eBay tab and read the leading "(N)" first
        # (cheap, no WebSocket); the eval is a defensive fallback for the on-page Messages badge.
        "url_match": ["ebay."],
        "mode": "eval",
        # Best-effort unread-Messages badge. Reads the "(N)" the eBay header shows on the Messages /
        # notifications control. Any failure returns {count:0, snippet:""} → fails open-safe.
        "js": r"""(() => {
          try {
            let count = 0;
            const t = (document.title || '').match(/^\s*\((\d+)\+?\)/);
            if (t) count = parseInt(t[1], 10);
            const nodes = Array.from(document.querySelectorAll(
              '[href*="mesgweb" i], [href*="/mye/myebay/messages" i], [aria-label*="message" i]'));
            for (const n of nodes) {
              const m = ((n.getAttribute('aria-label') || '') + ' ' + (n.textContent || '')).match(/\d+/);
              if (m) count = Math.max(count, parseInt(m[0], 10));
            }
            return { count, snippet: '' };
          } catch (e) { return { count: 0, snippet: '' }; }
        })()""",
    },
    "carousell": {
        "url_match": ["carousell.", "/inbox"],
        "mode": "eval",
        # Unread-conversation count = numeric badge on the "Inbox" nav link (present on every
        # Carousell page, so this works whether a conversation or the list is open). Snippet is
        # best-effort: the topmost conversation row's preview text. Carousell's inbox-list rows are
        # client-side `div[role="button"]` with hashed classes (NOT anchors), so we target them
        # class-agnostically via the avatar <img> each carries — the snippet changes when a new
        # message lands at the top, which trips is_new even when the badge count is unchanged.
        # Defensive: any failure returns {count:0, snippet:""} so the probe fails open-safe.
        "js": r"""(() => {
          try {
            const navs = Array.from(document.querySelectorAll('a[href="/inbox/"], a[href$="/inbox/"]'));
            let count = 0;
            for (const a of navs) {
              const m = (a.textContent || '').match(/\d+/);
              if (m) count = Math.max(count, parseInt(m[0], 10));
            }
            let snippet = '';
            const rows = Array.from(document.querySelectorAll('div[role="button"]'))
              .filter(r => r.querySelector('img') && (r.textContent || '').trim().length > 6);
            if (rows.length) snippet = (rows[0].textContent || '').trim().replace(/\s+/g, ' ').slice(0, 160);
            return { count, snippet };
          } catch (e) { return { count: 0, snippet: '' }; }
        })()""",
    },
}


# --------------------------------------------------------------------------- CDP transport

def _http_get_json(path: str, timeout: int):
    with urlopen(f"http://{CDP_HOST}:{CDP_PORT}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def list_page_targets(timeout: int = HTTP_TIMEOUT) -> list[dict]:
    """Open page tabs from CDP, or [] if Chrome/CDP is unreachable (fail-open)."""
    try:
        return [t for t in _http_get_json("/json/list", timeout) if t.get("type") == "page"]
    except (OSError, ValueError):
        return []


class _MiniWS:
    """Minimal RFC6455 client over a raw socket — just enough to call one CDP method.

    stdlib-only (no websocket-client dependency); client frames are masked as required."""

    def __init__(self, ws_url: str, timeout: int):
        parsed = urlparse(ws_url)
        self._sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode())
        status_line = self._read_until(b"\r\n\r\n").split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise OSError(f"websocket upgrade failed: {status_line!r}")

    def _read_until(self, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    def _recvn(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise OSError("socket closed mid-frame")
            buf += chunk
        return buf

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_frame(self) -> tuple[int, bytes]:
        first2 = self._recvn(2)
        opcode = first2[0] & 0x0F
        is_masked = bool(first2[1] & 0x80)
        length = first2[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recvn(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recvn(8))[0]
        mask = self._recvn(4) if is_masked else b""
        data = self._recvn(length) if length else b""
        if is_masked:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        return opcode, data

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def cdp_eval(ws_url: str, expression: str, timeout: int = WS_TIMEOUT):
    """Run a JS expression in a page over CDP and return its (by-value) result, or None."""
    ws = _MiniWS(ws_url, timeout)
    try:
        ws.send_text(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        while True:
            opcode, data = ws.recv_frame()
            if opcode == 0x8:  # close
                return None
            if opcode in (0x9, 0xA):  # ping/pong — ignore (no writes back; short-lived conn)
                continue
            if opcode != 0x1:  # only care about text frames
                continue
            obj = json.loads(data.decode("utf-8", "replace"))
            if obj.get("id") == 1:
                return obj.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()


# --------------------------------------------------------------------------- probing

def _find_tab(targets: list[dict], probe: dict) -> dict | None:
    """First tab whose URL contains all url_match substrings (or the alt set)."""
    for match_key in ("url_match", "url_match_alt"):
        needles = probe.get(match_key)
        if not needles:
            continue
        for t in targets:
            url = t.get("url", "")
            if all(n in url for n in needles):
                return t
    return None


def _title_count(title: str) -> int:
    m = re.match(r"\s*\((\d+)\+?\)", title or "")
    return int(m.group(1)) if m else 0


def probe_market(market: str, probe: dict, targets: list[dict]) -> dict:
    """Return {found: bool, count: int, snippet: str} for one market. Fail-open: never raises."""
    tab = _find_tab(targets, probe)
    if tab is None:
        return {"found": False, "count": 0, "snippet": ""}
    try:
        if probe.get("mode") == "title":
            return {"found": True, "count": _title_count(tab.get("title", "")), "snippet": ""}
        result = cdp_eval(tab["webSocketDebuggerUrl"], probe["js"])
        if isinstance(result, dict):
            count = int(result.get("count") or 0)
            snippet = str(result.get("snippet") or "")[:SNIPPET_MAX]
            return {"found": True, "count": count, "snippet": snippet}
        return {"found": False, "count": 0, "snippet": ""}
    except (OSError, ValueError, KeyError, TypeError):
        return {"found": False, "count": 0, "snippet": ""}


def enabled_markets() -> list[str]:
    try:
        cfg = json.loads(SELLER_CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return []
    markets = cfg.get("marketplaces", {})
    return [mid for mid, m in markets.items()
            if m.get("enabled") and mid in MARKET_PROBES]


def load_memo() -> dict:
    try:
        return json.loads(MEMO_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_memo(memo: dict) -> None:
    try:
        MEMO_PATH.write_text(json.dumps(memo, indent=2))
    except OSError:
        pass


def is_new(market: str, cur: dict, memo: dict) -> bool:
    """New activity since last peek: unread count rose, or there's unread with a changed preview.
    This is the gate that suppresses re-firing while a reply is still pending."""
    prev = memo.get(market, {})
    if cur["count"] > int(prev.get("count", 0)):
        return True
    if cur["count"] > 0 and cur.get("snippet") and cur["snippet"] != prev.get("snippet", ""):
        return True
    return False


def peek(update_memo: bool = True) -> dict:
    """Probe all enabled markets; return the peek contract. Fail-open-safe throughout.

    For markets whose inbox rows are per-thread enumerable (Carousell), `new` comes from the PRECISE
    classifier (inbox_scan): a market is sell-new only when a tracked sell thread has a fresh reply or
    an unknown non-system buyer enquiry arrives — buy-thread rows and promos no longer trip a sell
    pass. Non-enumerable markets (FB/eBay) and any scan failure fall back to the aggregate is_new
    below, so the revenue path can never be made LESS sensitive than today (and the daemon's forced
    sweep backstops a missed precise signal)."""
    targets = list_page_targets()
    memo = load_memo()
    markets_out: dict[str, dict] = {}
    pending = 0
    latest_text = ""
    next_memo = dict(memo)  # immutable update — build a new memo, don't mutate the loaded one

    try:
        import inbox_scan  # lazy: breaks the inbox_scan -> buyer_peek import cycle
        precise = inbox_scan.sell_markets_new()  # {market: bool} for enumerable markets only
    except Exception:
        precise = {}  # fail-open: fall back to the aggregate signal for every market

    for market in enabled_markets():
        cur = probe_market(market, MARKET_PROBES[market], targets)
        if market in precise:
            new = precise[market]  # precise per-thread signal wins for enumerable markets
        else:
            new = cur["found"] and is_new(market, cur, memo)  # aggregate fallback (FB/eBay, or scan down)
        markets_out[market] = {"count": cur["count"], "snippet": cur["snippet"],
                               "found": cur["found"], "new": new}
        if new:
            pending += 1
            if not latest_text:
                latest_text = f"[{market}] {cur['snippet']}".strip() if cur["snippet"] else f"[{market}] new message"
        if cur["found"]:  # only advance memo for markets we actually reached (keep last-good otherwise)
            next_memo[market] = {"count": cur["count"], "snippet": cur["snippet"]}

    if update_memo:
        save_memo(next_memo)
    return {"pending": pending, "latest_text": latest_text, "markets": markets_out}


# --------------------------------------------------------------------------- CLI

def _cmd_eval(market: str, expression: str) -> int:
    """Debug helper: run arbitrary JS in a market's inbox tab and print the raw result."""
    probe = MARKET_PROBES.get(market)
    if not probe:
        print(json.dumps({"error": f"unknown market: {market}"}))
        return 2
    tab = _find_tab(list_page_targets(), probe)
    if tab is None:
        print(json.dumps({"error": f"no inbox tab found for {market}"}))
        return 3
    try:
        print(json.dumps({"value": cdp_eval(tab["webSocketDebuggerUrl"], expression)}, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 3


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "eval":
        if len(argv) < 4:
            print("usage: buyer_peek.py eval <market> '<js>'", file=sys.stderr)
            return 2
        return _cmd_eval(argv[2], argv[3])
    try:
        result = peek(update_memo="--no-memo" not in argv)
    except Exception as exc:  # last-resort fail-open: never crash the daemon
        print(json.dumps({"pending": 0, "latest_text": "", "error": str(exc)}))
        return 0
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
