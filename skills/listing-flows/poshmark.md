# Listing flow — Poshmark (browser-mode)

Per-site recipe for publishing one item to **Poshmark** through the seller's real logged-in Chrome.
Uses only the `browser-actions.md` vocabulary (goal-style; re-find controls each run). Called by
`skills/channel/listing.md` once the item is eligible and (per `approvals.steps.publish`) confirmed.

> **Category-gated.** Poshmark is fashion-only. The registry (`data/marketplaces.json`) lists its
> `categories` as `fashion, apparel, shoes, accessories, beauty, home_decor`; `listing.md`'s eligible
> filter already excludes non-fashion items before this recipe runs. Treat a non-fashion item
> reaching here as a bug → **escalate**, don't post.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

> **Region:** post on the seller's regional Poshmark (US → `poshmark.com`, CA → `poshmark.ca`,
> AU → `poshmark.com.au`). Use `host = seller_config.marketplaces.poshmark.site` (or
> `python3 bin/resolve_domain.py --market poshmark --region <seller_config.region>`) for step 1.

## Steps
1. `navigate("https://<host>/create-listing")` — Poshmark "List an Item" flow (`<host>` resolved above).
2. `click("add-photos")` → attach the item's photo paths (cover first; Poshmark wants ≥1, allows 16).
3. `type("title", item.title)`
4. `type("description", item.description)` — honest, ship-P2P wording. **No meetup.** (Poshmark ships
   via prepaid label by default; the buyer-facing total is still quoted in-chat via `shipping.py`.)
5. `click("category")` → choose Department → Category → Subcategory closest to `item.category`.
6. Fill **brand** and **size** from `item.attributes` (Poshmark prominently surfaces both).
7. `click("condition")` → NWT vs used, from `item.condition`.
8. `type("price", item.list_price)` — set "Original Price" if asked (use comp_high or list_price);
   listing price = `item.list_price`.
9. `read` the preview → return it for the caller's final `confirm`. On confirm: `click("list-item")`.
10. `read` the live listing URL (`/listing/<slug>-<id>`) from the published page → return it.
    **Only return a URL you actually read from the DOM — never compose one.** No readable permalink →
    return no URL (caller treats the market as failed; every URL is checked by `bin/verify_listing_url.py`).

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(listing_urls.poshmark)` → **Edit Listing** → if Poshmark fulfilled the sale itself it is
already marked sold; otherwise use **"Delete Listing" / mark unavailable**. Confirm it no longer shows
as for-sale.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
`navigate` to the seller's **closet → "Available"** → `read_listing()` over the seller's own
available listings (scroll to load all; skip Sold). Return `[{title, url, price}]`, where `url` is
each listing's permalink (`/listing/<slug>-<id>`) — the dedupe key SCAN matches against managed
items' `listing_urls`. Read-only; never edits or re-lists here.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Guardrails
- Logged-out / checkpoint → **stop and escalate** ("re-auth your Poshmark"), no tight-loop retries.
- Category mismatch (non-fashion item) → **escalate**, never force-post.
- Publish gating follows `config.approvals.steps.publish` (see `skills/bazaar-config.md`).
- `--dry-run`: log each verb instead of executing.
