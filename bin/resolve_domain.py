#!/usr/bin/env python3
"""resolve_domain.py — region → marketplace host resolver (deterministic, no LLM).

Single source of truth for "which regional site of a marketplace should the seller be connected
to / posting on". An SG seller lists to ``www.ebay.com.sg`` / ``www.carousell.sg`` — not the global
``ebay.com``. Reads the ``domains`` map in ``data/marketplaces.json``.

Resolution rule (first match wins):
    1. ``domains[region]``                  — exact regional host
    2. ``domains["*"]``                      — marketplace's global default host
    3. ``listing_url.host`` (suffix pattern) — back-compat fallback for entries with no domains map
                                               (e.g. craigslist, whose host is metro-derived)

Contract:
    resolve_domain.py --market <id> --region <code>
      → exit 0 and print {"market": id, "region": region, "host": host} when resolvable
      → exit 1 and print {"market": id, "region": region, "host": null, "reason": "..."} when not

Pure/stdlib; reads the registry but mutates nothing. Imported by ``verify_listing_url.py`` (region
gate) and invoked by ``skills/channel/onboarding.md`` (connect) + the listing-flow recipes (publish).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SELLER_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = SELLER_DIR / "data" / "marketplaces.json"

ANY = "*"


def _load_entry(market: str) -> dict | None:
    """Return the registry entry dict for a market id, or None if absent/unreadable."""
    try:
        registry = json.loads(REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        return None
    for entry in registry.get("marketplaces", []):
        if entry.get("id") == market:
            return entry
    return None


def resolve(market: str, region: str | None) -> str | None:
    """Resolve the region-specific host for a market. Returns the host string or None.

    Falls back to the marketplace's ``"*"`` default, then to the ``listing_url.host`` suffix
    pattern, so markets without a domains map keep working unchanged."""
    entry = _load_entry(market)
    if entry is None:
        return None

    domains = entry.get("domains") or {}
    if region and region in domains:
        return domains[region]
    if ANY in domains:
        return domains[ANY]

    listing_url = entry.get("listing_url") or {}
    return listing_url.get("host") or None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="resolve_domain.py")
    p.add_argument("--market", required=True, help="marketplace id (fb, carousell, ebay, …)")
    p.add_argument("--region", default="", help="seller region code (SG, US, …); blank → default")
    ns = p.parse_args(argv[1:])

    region = ns.region.strip() or None
    host = resolve(ns.market, region)
    if host:
        print(json.dumps({"market": ns.market, "region": region, "host": host}))
        return 0
    print(json.dumps({
        "market": ns.market,
        "region": region,
        "host": None,
        "reason": f"no resolvable host for market '{ns.market}'",
    }))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
