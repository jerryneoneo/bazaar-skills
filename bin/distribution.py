#!/usr/bin/env python3
"""distribution.py — where can this item live? The generalized marketplace filter.

This owns ONE definition of the region/category match rule from skills/marketplaces.md
(the same rule listing.md PUBLISH uses to pick `eligible`), generalized into the three
sets the distribution flow needs:

    already_listed        — registry ids already in item.listing_urls (the dedupe anchor)
    cross_list_candidates — enabled + active + category-match, NOT yet listed (publish now)
    recommend_setup       — NOT enabled + region-match + category-match, NOT listed
                            (active = offer setup+cross-list; stub = "coming soon")
    dropped_enabled       — enabled platforms excluded for this item (why-not, for messaging)

Pure / deterministic. Reads only reference data (data/marketplaces.json), the seller's
selection (seller_config.json -> marketplaces, region), and the buyer-safe item record.
It NEVER reads or emits a floor or an address — no secrets in, no secrets out.

Usage:
    python3 distribution.py --item <item_id>                  # category_tag + listing_urls from item
    python3 distribution.py --category-tag <tag> --region <r> # preview before an item exists (import)
Output (stdout, JSON):
    {"item_id","category_tag","region","already_listed":[...],"cross_list_candidates":[...],
     "recommend_setup":[{"id","display_name","status"}],"dropped_enabled":[{"id","reason"}]}

Exit codes: 0 ok · 2 bad input · 3 config/item/registry data missing or invalid.
"""

import argparse
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "seller_config.json"
REGISTRY_PATH = DATA_DIR / "marketplaces.json"
ITEMS_DIR = DATA_DIR / "items"

ANY = "*"


def _load_json(path, label):
    if not path.exists():
        raise FileNotFoundError(f"{label} not found at {path}")
    return json.loads(path.read_text())


def _registry_by_id(registry_doc):
    """Index data/marketplaces.json's list into {id: entry}."""
    entries = registry_doc.get("marketplaces")
    if not isinstance(entries, list) or not entries:
        raise ValueError("marketplaces.json has no marketplaces list")
    return {m["id"]: m for m in entries}


def _normalize_selection(marketplaces, registry):
    """Read-shim from skills/marketplaces.md: a legacy ARRAY selection becomes the object
    shape {id: {enabled, connector}}. Returns a new dict (never mutates the input)."""
    if isinstance(marketplaces, dict):
        return marketplaces
    if isinstance(marketplaces, list):
        return {
            mid: {
                "enabled": True,
                "connector": registry.get(mid, {}).get("connector", {}).get("type"),
            }
            for mid in marketplaces
        }
    return {}


def region_match(entry, region):
    regions = entry.get("regions", [])
    return ANY in regions or (region in regions if region else False)


def category_match(entry, category_tag):
    categories = entry.get("categories", [])
    return ANY in categories or category_tag in categories


def classify(registry, selection, region, category_tag, listing_urls):
    """Pure set computation over the registry. Order is registry order, deterministic."""
    listed_ids = [mid for mid in registry if mid in (listing_urls or {})]
    listed_set = set(listed_ids)

    cross_list = []
    dropped = []
    for mid, sel in selection.items():
        entry = registry.get(mid)
        if entry is None or not sel.get("enabled"):
            continue
        if mid in listed_set:
            continue
        if entry.get("status") != "active":
            dropped.append({"id": mid, "reason": "inactive"})
        elif not category_match(entry, category_tag):
            dropped.append({"id": mid, "reason": "category"})
        else:
            cross_list.append(mid)

    recommend = []
    for mid, entry in registry.items():
        if selection.get(mid, {}).get("enabled"):
            continue
        if mid in listed_set:
            continue
        if region_match(entry, region) and category_match(entry, category_tag):
            recommend.append(
                {
                    "id": mid,
                    "display_name": entry.get("display_name", mid),
                    "status": entry.get("status", "stub"),
                }
            )

    return {
        "already_listed": listed_ids,
        "cross_list_candidates": cross_list,
        "recommend_setup": recommend,
        "dropped_enabled": dropped,
    }


def run(item_id, category_tag, region):
    config = _load_json(CONFIG_PATH, "seller_config.json")
    registry = _registry_by_id(_load_json(REGISTRY_PATH, "marketplaces.json"))
    selection = _normalize_selection(config.get("marketplaces", {}), registry)

    listing_urls = {}
    if item_id:
        item = _load_json(ITEMS_DIR / f"{item_id}.json", f"item {item_id!r}")
        category_tag = item.get("category_tag") or category_tag
        listing_urls = item.get("listing_urls") or {}

    region = region or config.get("region")
    # Taxonomy fallback (skills/marketplaces.md): when an item record predates category_tag,
    # treat it as "other" — which only matches "*" platforms — rather than failing the scan.
    if not category_tag:
        category_tag = "other"

    sets = classify(registry, selection, region, category_tag, listing_urls)
    return {"item_id": item_id, "category_tag": category_tag, "region": region, **sets}


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="distribution.py", add_help=False)
    parser.add_argument("--item", default="")
    parser.add_argument("--category-tag", default="")
    parser.add_argument("--region", default="")
    ns = parser.parse_args(argv[1:])
    item = ns.item.strip()
    category_tag = ns.category_tag.strip()
    region = ns.region.strip()
    if not item and not category_tag:
        raise ValueError("provide --item, or --category-tag (with --region)")
    return item, category_tag, region


def main(argv):
    try:
        item_id, category_tag, region = _parse_args(argv)
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        result = run(item_id, category_tag, region)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
