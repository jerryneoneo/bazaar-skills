# Listing flow — OfferUp (browser-mode)

Per-site recipe for publishing one item to **OfferUp** through the seller's real logged-in Chrome.
Uses only the `browser-actions.md` vocabulary (goal-style; re-find controls each run). Called by
`skills/channel/listing.md` once the item is eligible and (per `approvals.steps.publish`) confirmed.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

> **Region:** OfferUp is one US host. Use `host = seller_config.marketplaces.offerup.site` (or
> `python3 bin/resolve_domain.py --market offerup --region <seller_config.region>` → `offerup.com`)
> for step 1.

## Steps
1. `navigate("https://<host>/sell")` — OfferUp "Post / Sell an item" flow (`<host>` resolved above).
2. `click("add-photos")` → attach the item's photo paths (first is the cover).
3. `type("title", item.title)`
4. `click("category")` → choose closest to `item.category`.
5. `click("condition")` → set from `item.condition`.
6. `type("description", item.description)` — honest, ship-P2P / delivery-quoted wording. **No meetup.**
7. `type("price", item.list_price)`
8. **Delivery:** enable **"Ship to buyer" / nationwide shipping** (buyer pays); do **not** select
   local-pickup-only. (OfferUp is local-first; we force shipping so every deal flows through SELLY's
   P2P quote from `shipping.py`. If the account can't enable shipping for this category → **escalate**,
   don't fall back to local meetup.)
9. `read` the preview → return it for the caller's final `confirm`. On confirm: `click("post")`.
10. `read` the live listing URL (`/item/detail/<id>`) from the published page → return it.
    **Only return a URL you actually read from the DOM — never compose one.** No readable permalink →
    return no URL (caller treats the market as failed; every URL is checked by `bin/verify_listing_url.py`).

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(listing_urls.offerup)` → open the item's manage menu → **"Mark as Sold"** (preferred) or
**"Archive / Delete"**. Confirm it no longer shows as active.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
`navigate` to **account → "Selling" / "My items"** → `read_listing()` over the seller's own active
items (scroll to load all; skip Sold/Archived). Return `[{title, url, price}]`, where `url` is each
listing's permalink (`/item/detail/<id>`) — the dedupe key SCAN matches against managed items'
`listing_urls`. Read-only; never edits or re-lists here.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Guardrails
- Logged-out / checkpoint / phone-verify → **stop and escalate** ("re-auth your OfferUp"), no
  tight-loop retries.
- **Ship-only invariant:** never enable local-pickup-only or arrange an in-person meet — redirect to
  shipping. If shipping is unavailable for the category, escalate.
- Publish gating follows `config.approvals.steps.publish` (see `skills/selly-config.md`).
- `--dry-run`: log each verb instead of executing.
