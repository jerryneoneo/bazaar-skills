"""Ephemeral helper for /catchup deep sweep: list managed items + their listing_urls.

Prints one JSON object: {item_id: {status, listing_urls}}. Read-only.
"""
import json
import glob
import os

out = {}
for p in sorted(glob.glob("data/items/*.json")):
    try:
        d = json.load(open(p))
    except Exception as e:  # noqa: BLE001 - surface parse errors, don't crash the sweep
        out[os.path.basename(p)] = {"error": str(e)}
        continue
    iid = d.get("item_id") or os.path.basename(p)[:-5]
    out[iid] = {
        "status": d.get("status"),
        "listing_urls": d.get("listing_urls") or {},
        "managed": d.get("managed"),
    }
print(json.dumps(out, indent=2))
