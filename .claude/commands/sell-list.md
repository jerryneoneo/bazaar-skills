---
description: List an item (photos → vision → comps → price → floor → shipping → frontloader → publish)
---

# /sell-list — create a listing

Thin entry point into the channel-agnostic listing flow. Runs the full smart listing pipeline
and publishes to the seller's enabled marketplaces (whatever is enabled in `seller_config.json`,
e.g. FB, Carousell, eBay).

→ Execute **`skills/channel/listing.md`**. Apply `skills/voice.md` to every message
(no em-dashes; ack before any slow comps/pricing/publish step).

Prerequisites:
- Onboarding done (`data/seller_config.json` exists). If not, run
  `skills/channel/onboarding.md` first.
- Logged in to the target marketplaces in your real Chrome (Claude in Chrome).

Notes:
- The hidden floor is written only to `data/floors/<item_id>.json`; the exact pickup address
  stays in `seller_config.json` and is used only by `bin/shipping.py`.
- Honors `--dry-run` (browser actions logged, not executed; all files still written).
- This is also reachable as a `/list` command over the seller channel via `/sell-run`.
