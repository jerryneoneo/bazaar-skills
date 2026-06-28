#!/usr/bin/env python3
"""login_check.py — is the seller actually logged in to a marketplace? (~0 tokens, no LLM)

Onboarding asks "Logged in?" and trusts the answer (a wrong "yes" writes auth:"confirmed" and
then listing/inbox silently fail at runtime). This probe replaces the honour system with a cheap,
read-only DOM check over the already-running warm CDP Chrome (bin/chrome_debug.sh on :9222), reusing
buyer_peek's stdlib CDP transport (no Playwright, no LLM).

Three-state by design — it must never flip a genuinely-logged-in seller to "needs_login":
    logged_in  — a strong authed marker is present (account nav / inbox / sell CTA)
    logged_out — a strong unauthed marker is present (the login form / Log-in CTA, no authed marker)
    unknown    — no tab open for the market, CDP unreachable, or the page is ambiguous

Callers act only on confident signals: onboarding confirms logged_in automatically and falls back to
asking the seller on logged_out/unknown; healthcheck WARNs only on a positive logged_out.

Per-market markers live in LOGIN_PROBES so DOM drift is a one-line fix (same pattern as
buyer_peek.MARKET_PROBES). A market with no probe returns "unknown" (never a false claim).

Usage:
    login_check.py market <id>        # probe one market   -> {market, status, detail}
    login_check.py all                # probe enabled markets -> {markets: {<id>: {...}}}

Exit (market mode): 0 logged_in · 1 logged_out · 2 bad input · 3 unknown/unreachable.
Exit (all mode): always 0 (read the per-market status from JSON).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buyer_peek  # noqa: E402  reuse list_page_targets / cdp_eval / _find_tab (stdlib CDP client)

VALID_STATES = ("logged_in", "logged_out", "unknown")

# Per-market login markers. `url_match`/`url_match_alt` find the market's tab (substring match, same
# contract as buyer_peek). `js` runs in that tab and MUST return {"state": "<logged_in|logged_out|
# unknown>"} (defensive: any error returns "unknown" so the probe fails open-safe).
LOGIN_PROBES: dict[str, dict] = {
    "fb": {
        "url_match": ["facebook.com"],
        "url_match_alt": ["messenger.com"],
        # Logged-out FB serves the classic login form (a name="pass" password field) at top level.
        # Logged-in FB has an account/profile control and the marketplace nav, and no such form.
        "js": r"""(() => {
          try {
            const loginForm = !!document.querySelector('input[name="pass"]');
            const authed = !!document.querySelector(
              '[aria-label="Your profile" i], [aria-label*="Account" i], '
              + 'a[href*="/marketplace/"], div[role="navigation"] a[href*="/me/" i]');
            if (authed) return { state: 'logged_in' };
            if (loginForm) return { state: 'logged_out' };
            return { state: 'unknown' };
          } catch (e) { return { state: 'unknown' }; }
        })()""",
    },
    "carousell": {
        "url_match": ["carousell."],
        # Logged-in Carousell shows the inbox nav link and a Sell CTA (both auth-gated). Logged-out
        # shows a prominent Log in / Sign up control and no inbox link.
        "js": r"""(() => {
          try {
            const inbox = !!document.querySelector('a[href="/inbox/"], a[href$="/inbox/"]');
            const sell = !!document.querySelector('a[href="/sell/"], a[href$="/sell/"], a[href*="/sell/new"]');
            if (inbox || sell) return { state: 'logged_in' };
            const txt = (document.body && document.body.innerText || '');
            if (/\bLog in\b|\bSign up\b/i.test(txt)) return { state: 'logged_out' };
            return { state: 'unknown' };
          } catch (e) { return { state: 'unknown' }; }
        })()""",
    },
    "ebay": {
        "url_match": ["ebay."],
        # eBay greets a signed-in user by name ("Hi <name>") and shows a My eBay menu; signed-out
        # shows a "Sign in" link in the global header.
        "js": r"""(() => {
          try {
            const authed = !!document.querySelector('a[href*="/mye/myebay" i], #gh-ug, [id*="gh-ug"]');
            const signin = !!document.querySelector('a[href*="signin.ebay" i], a[rel="nofollow"][href*="SignIn" i]');
            if (authed) return { state: 'logged_in' };
            if (signin) return { state: 'logged_out' };
            return { state: 'unknown' };
          } catch (e) { return { state: 'unknown' }; }
        })()""",
    },
}


def classify(js_result) -> str:
    """PURE: map a probe's JS return value to one of VALID_STATES. Anything unexpected -> unknown."""
    if isinstance(js_result, dict):
        state = js_result.get("state")
        if state in VALID_STATES:
            return state
    return "unknown"


def probe_market(market: str, targets: list[dict]) -> dict:
    """Return {market, status, detail} for one market. Fail-open: never raises."""
    probe = LOGIN_PROBES.get(market)
    if not probe:
        return {"market": market, "status": "unknown", "detail": "no login probe for this market"}
    tab = buyer_peek._find_tab(targets, probe)
    if tab is None:
        return {"market": market, "status": "unknown",
                "detail": "no open tab for this market (navigate to it first)"}
    try:
        result = buyer_peek.cdp_eval(tab["webSocketDebuggerUrl"], probe["js"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"market": market, "status": "unknown", "detail": "CDP probe failed"}
    status = classify(result)
    return {"market": market, "status": status, "detail": tab.get("url", "")}


def check_market(market: str) -> dict:
    return probe_market(market, buyer_peek.list_page_targets())


def check_all(markets: list[str] | None = None) -> dict:
    if markets is None:
        markets = buyer_peek.enabled_markets()
    targets = buyer_peek.list_page_targets()
    return {"markets": {m: probe_market(m, targets) for m in markets}}


_EXIT = {"logged_in": 0, "logged_out": 1, "unknown": 3}


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "market":
        result = check_market(argv[2])
        print(json.dumps(result))
        return _EXIT.get(result["status"], 3)
    if len(argv) >= 2 and argv[1] == "all":
        print(json.dumps(check_all()))
        return 0
    print("usage: login_check.py market <id> | login_check.py all", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
