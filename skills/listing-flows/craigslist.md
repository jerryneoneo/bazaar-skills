# Listing flow — Craigslist (browser-mode)

Per-site recipe for publishing one item to **Craigslist** through the seller's Chrome. Uses only the
`browser-actions.md` vocabulary (goal-style; re-find controls each run). Called by
`skills/channel/listing.md` once the item is eligible and (per `approvals.steps.publish`) confirmed.

> **Auth is `none`** in the registry — Craigslist posts via an email-confirmation flow, not a logged-in
> account. The seller's Craigslist email must be reachable. If posting requires an account login on
> this run, treat it like any auth wall → **escalate**.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

## Steps
1. `navigate("<region>.craigslist.org → post")` — pick the seller's metro from `seller_config.region`/
   area; if ambiguous, **escalate** to ask which city site.
2. Choose posting type **"for sale by owner"** → choose the category closest to `item.category`.
3. `type("title", item.title)`
4. `type("price", item.list_price)`
5. `type("postal", seller_config.origin.postcode)` — **postal code only** (area-level), never the
   full street address (privacy invariant; the exact address stays in `seller_config.json`).
6. `type("description", item.description)` — honest, ship-P2P / delivery-quoted wording. **No meetup,
   no full address, no phone unless the seller opted in.** Add "delivery quoted via message."
7. `click("add-images")` → attach the item's photo paths.
8. `click("publish")` → Craigslist sends a **confirmation email**; follow its confirm link (via the
   connected mail MCP if available, else **escalate**: "confirm the Craigslist email I just triggered").
9. `read` the live posting URL (`/d/...`) from the published page → return it.
   **Only return a URL you actually read from the DOM — never compose one.** No readable permalink →
   return no URL (caller treats the market as failed; every URL is checked by `bin/verify_listing_url.py`).

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(listing_urls.craigslist)` or use the **manage link** from the confirmation email →
**"delete this posting"**. Confirm it 404s / shows removed.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
If the seller uses a Craigslist **account**, `navigate` to **"my account" → active postings** →
`read_listing()` → `[{title, url, price}]` (`url` = the posting permalink, the SCAN dedupe key).
**Caveat:** `auth:"none"` postings made purely via email-confirmation have no account list to read —
those aren't scannable here; `say` that once and skip (don't guess). Read-only; never edits.
`--dry-run` → log only.

## Guardrails
- Posting blocked / flagged / login wall / phone-verify → **stop and escalate**, no tight-loop retries.
- **Privacy:** postal code only, never the street address; ship-only (no meetup phrasing).
- Publish gating follows `config.approvals.steps.publish` (see `skills/selly-config.md`).
- `--dry-run`: log each verb instead of executing.
