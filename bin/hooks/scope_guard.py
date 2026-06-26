#!/usr/bin/env python3
"""scope_guard.py — PreToolUse hook: a per-marketplace worker can't navigate to ANOTHER marketplace.

Phase-3 concurrency runs one buyer worker per marketplace, each scoped via $BAZAAR_RESOURCE and
told (by its prompt) to drive only its own tab. This is the DETERMINISTIC backstop, independent of
LLM compliance: when $BAZAAR_RESOURCE is set, a `browser_navigate` whose target host belongs to a
DIFFERENT marketplace is DENIED — so a worker can never act on another account's tab (the
conservative same-account invariant, hard-enforced at the harness level).

Allowed when scoped: the worker's OWN marketplace host (any region) and any non-marketplace host
(we don't over-block — the market:<id> lease + the atomic per-market pacing cap remain the primary
guards; this hook targets the one dangerous case: cross-marketplace navigation). Unscoped passes
(no $BAZAAR_RESOURCE — the channel/legacy single-flight loop) are never affected.

Reads the PreToolUse event JSON on stdin; emits a deny decision on stdout, or nothing (exit 0 =
allow). Fail-OPEN: any error allows the tool — a hook must never wedge the agent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

MARKETPLACES = Path(__file__).resolve().parent.parent.parent / "data" / "marketplaces.json"
NAVIGATE_TOOL = "mcp__playwright__browser_navigate"


def _hosts_by_market():
    """{market_id: {host, …}} from marketplaces.json `domains` (all regions). Tolerates the
    list-of-objects shape and the legacy {id: {...}} mapping shape."""
    raw = json.loads(MARKETPLACES.read_text())
    markets = raw.get("marketplaces", raw) if isinstance(raw, dict) else raw
    items = markets if isinstance(markets, list) else [{"id": k, **v} for k, v in markets.items()]
    out = {}
    for m in items:
        mid = m.get("id")
        hosts = {str(h).lower() for h in (m.get("domains") or {}).values() if h}
        if mid and hosts:
            out[mid] = hosts
    return out


def _norm(host):
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def market_for_host(host, hosts_by_market):
    """Which marketplace owns `host` (equal to, or a subdomain of, one of its known hosts), or None."""
    h = _norm(host)
    if not h:
        return None
    for mid, hosts in hosts_by_market.items():
        for known in hosts:
            k = _norm(known)
            if h == k or h.endswith("." + k):
                return mid
    return None


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


def main():
    resource = (os.environ.get("BAZAAR_RESOURCE") or "").strip()
    if not resource:
        return 0  # unscoped pass → no opinion
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return 0  # unparseable → fail open
    if event.get("tool_name", "") != NAVIGATE_TOOL:
        return 0
    tool_input = event.get("tool_input", {}) or {}
    url = tool_input.get("url", "") if isinstance(tool_input, dict) else ""
    if not url:
        return 0
    try:
        owner = market_for_host(urlparse(url).netloc, _hosts_by_market())
    except (ValueError, OSError, json.JSONDecodeError):
        return 0  # registry unreadable / bad URL → fail open
    if owner and owner != resource:
        _deny(f"Bazaar worker scoped to '{resource}' — refusing to navigate to the '{owner}' "
              f"marketplace. Each marketplace is driven by its own worker (account-safety guard).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
