# Listing flow — Mercari (browser-mode)

Per-site recipe for publishing one item to **Mercari** through the seller's real logged-in Chrome.
Uses only the `browser-actions.md` vocabulary (goal-style; re-find controls each run). Called by
`skills/channel/listing.md` once the item is eligible and (per `approvals.steps.publish`) confirmed.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

> **Region:** post on the seller's regional Mercari (US → `www.mercari.com`, JP → `jp.mercari.com`).
> Use `host = seller_config.marketplaces.mercari.site` (or `python3 bin/resolve_domain.py --market
> mercari --region <seller_config.region>`) for step 1.

## Steps
1. `navigate("https://<host>/sell")` — Mercari "List an item" / "Sell" flow (`<host>` resolved above).
2. `click("add-photos")` → attach the item's photo paths (first photo is the cover; Mercari allows up
   to 12).
3. `type("title", item.title)`
4. `type("description", item.description)` — honest, ship-P2P / delivery-quoted wording. **No meetup.**
5. `click("category")` → choose closest to `item.category`; fill **brand / size / color** from
   `item.attributes` where Mercari requests them.
6. `click("condition")` → set from `item.condition` (Mercari's 5-point scale: New … Poor).
7. **Shipping:** choose a **buyer-pays** shipping option (Mercari "Ship on your own" or its label
   flow). Do not pick a free-shipping/seller-pays option (the P2P fee is quoted in-chat via
   `shipping.py`). No local meetup.
8. `type("price", item.list_price)` — Mercari shows its fee/earnings estimate; ignore, list at
   `item.list_price`. Leave "Smart Pricing" / auto-markdown **off**.
9. `read` the preview → return it for the caller's final `confirm`. On confirm: `click("list")`.
10. `read` the live listing URL (`/item/<id>`) from the published page → return it.
    **Only return a URL you actually read from the DOM — never compose one.** No readable permalink →
    return no URL (caller treats the market as failed; every URL is checked by `bin/verify_listing_url.py`).

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(listing_urls.mercari)` → open the listing's **Edit / manage** menu → **"Mark as sold"** if
offered, else **"Delete listing"**. Confirm it no longer shows as available.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
`navigate` to the seller's profile → **"Listings" / "For sale"** → `read_listing()` over the
seller's own active items (scroll to load all; skip Sold). Return `[{title, url, price}]`, where
`url` is each listing's permalink (`/item/<id>`) — the dedupe key SCAN matches against managed
items' `listing_urls`. Read-only; never edits or re-lists here.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Guardrails
- Logged-out / checkpoint / phone-verify → **stop and escalate** ("re-auth your Mercari"), no
  tight-loop retries.
- Leave Smart Pricing / auto-offers **off** — pricing & negotiation are SELLY's job.
- Publish gating follows `config.approvals.steps.publish` (see `skills/selly-config.md`).
- `--dry-run`: log each verb instead of executing.
