#!/usr/bin/env python3
"""verify_listing_url.py — deterministic guard against fabricated listing links (no LLM).

The listing flow records a listing URL only after the per-site recipe READS it from the live,
published page. This script is the hard gate that enforces it: a URL is accepted only when it
matches the marketplace's registered host + path pattern in ``data/marketplaces.json``
(e.g. fb → ``facebook.com`` + ``/marketplace/item/``). A hallucinated or wrong-host link
(``https://fb.com/item/abc``, a made-up shortlink, an empty string) fails closed.

Contract:
    verify_listing_url.py --market <id> --url <url> [--region <code>]
      → exit 0 and print {"ok": true,  "market": id, "url": url} when the URL is valid
      → exit 1 and print {"ok": false, "market": id, "reason": "..."} when it is not

With ``--region`` the host must match the *region-specific* domain (resolve_domain.py) — a listing
that landed on the wrong regional site (SG seller, but ``ebay.com``) fails closed. Without it, the
check is the legacy host-suffix match (back-compat).

Pure/stdlib; reads the registry but mutates nothing. Used by ``skills/channel/listing.md`` before
writing ``items.listing_urls[market]`` and before any "🎉 Live!" message.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from resolve_domain import resolve as resolve_domain

SELLER_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SELLER_DIR / "data" / "marketplaces.json"


def load_pattern(market: str) -> dict | None:
    """Return the {host, path} listing_url pattern for a market id, or None if absent."""
    try:
        registry = json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        return None
    for entry in registry.get("marketplaces", []):
        if entry.get("id") == market:
            return entry.get("listing_url")
    return None


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def verify(market: str, url: str, region: str | None = None) -> tuple[bool, str]:
    """Validate a listing URL against the market's registered host+path pattern.

    When ``region`` is given AND the market has a ``domains`` map, the host must match the
    *region-specific* domain (e.g. an SG seller's URL must be ``ebay.com.sg``, not ``ebay.com``) —
    a wrong-region link fails closed. Without ``region`` (or for markets with no domains map) the
    check falls back to the legacy ``listing_url.host`` suffix match. Returns (ok, reason)."""
    pattern = load_pattern(market)
    if not pattern or not pattern.get("host") or not pattern.get("path"):
        return False, f"no listing_url pattern registered for market '{market}'"

    url = (url or "").strip()
    if not url:
        return False, "empty url — nothing was read from the live page"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"url scheme must be http(s), got '{parsed.scheme or 'none'}'"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "url has no host"

    region_host = resolve_domain(market, region) if region else None
    if region and region_host and "." in region_host:
        # Region gate: host must equal the regional domain or be a subdomain of it.
        want = _strip_www(region_host.lower())
        got = _strip_www(host)
        if got != want and not got.endswith("." + want):
            return False, f"host '{host}' is not the {region} site (expected '{region_host}')"
    else:
        want_host = pattern["host"].lower()
        if want_host not in host:
            return False, f"host '{host}' does not match expected '{want_host}'"

    if pattern["path"] not in parsed.path:
        return False, f"path '{parsed.path}' does not contain expected '{pattern['path']}'"
    return True, "valid"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="verify_listing_url.py")
    p.add_argument("--market", required=True, help="marketplace id (fb, carousell, ebay, …)")
    p.add_argument("--url", required=True, help="the listing URL read from the live page")
    p.add_argument("--region", default="", help="seller region (SG, US, …) → enforce regional site")
    ns = p.parse_args(argv[1:])

    ok, reason = verify(ns.market, ns.url, ns.region.strip() or None)
    if ok:
        print(json.dumps({"ok": True, "market": ns.market, "url": ns.url.strip()}))
        return 0
    print(json.dumps({"ok": False, "market": ns.market, "reason": reason}))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
