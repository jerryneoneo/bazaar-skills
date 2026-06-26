# Listing flow — eBay (browser-mode)

Per-site recipe for publishing one item to **eBay** through the seller's real logged-in Chrome.
Uses only the `browser-actions.md` vocabulary (goal-style: re-find controls visually each run).
Called by `skills/channel/listing.md` once the item is eligible and (per `approvals.steps.publish`)
the seller confirms.

> If a first-party eBay API/MCP becomes available it can replace this recipe behind the same step
> contract (the registry `connector.type=api` path) with no change to `listing.md`.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

> **Region:** post on the seller's regional eBay (SG → `www.ebay.com.sg`, never global `ebay.com`).
> Use `host = seller_config.marketplaces.ebay.site` (or `python3 bin/resolve_domain.py --market ebay
> --region <seller_config.region>`) as the base for step 1's navigation.

## Steps
1. `navigate("https://<host>/sl/sell")` — eBay's "Create listing / Sell your item" flow on the
   regional site (`<host>` resolved above).
2. `type("title", item.title)` → eBay surfaces a category suggestion; `click` the closest match to
   `item.category`. If it offers "Tell us what you're selling", paste the title and accept the match.
3. `click("add-photos")` → attach the item's photo paths (clearest first; eBay wants ≥1, allows 24).
4. `click("condition")` → set from `item.condition` (eBay's condition list, e.g. "Used - Good").
5. Fill required **item specifics** eBay marks mandatory (brand/size/color) from `item.attributes`;
   leave optional ones blank rather than guessing.
6. `type("description", item.description)` — honest, ship-P2P / delivery-quoted wording. **No meetup
   / local-pickup phrasing.**
7. `type("price", item.list_price)` — use **Buy It Now / fixed price** (not auction); the negotiation
   layer is Bazaar's, not eBay Best Offer. Leave Best Offer **off**.
   **Currency:** the price field / its currency selector must read `seller_config.currency` (SG site →
   SGD, not the global USD default). If eBay shows a currency dropdown next to price, `click` it and
   select `seller_config.currency`; `read` it back to confirm. If the regional site won't offer the
   seller's currency (mismatched account-of-registration), **stop and escalate** ("your eBay account
   currency isn't <currency> — fix it in eBay account settings") rather than list in the wrong currency.
8. Set **shipping** to a flat/calculated buyer-paid option; do **not** enable local pickup-only.
   (The exact P2P delivery fee is quoted in-chat via `shipping.py`, not set here.)
9. `read` the rendered preview → return it for the caller's final `confirm` (per `approvals.steps.publish`).
   On confirm: `click("list-it")`.
10. `read` the published listing URL (`/itm/<id>`) from the live page → return it.
    **Only return a URL you actually read from the DOM — never compose one.** If publish didn't
    complete or no permalink is readable, return no URL (the caller treats this market as failed and
    validates every URL via `bin/verify_listing_url.py` before recording it).

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(listing_urls.ebay)` or Seller Hub → **Active listings** → open the item's manage menu →
**"End listing"** (reason: "Sold via another channel" if asked) or **"Mark as sold"**. Confirm it
no longer shows as active.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
`navigate` to **Seller Hub → Active listings** (or My eBay → Selling → Active) → `read_listing()`
over the seller's own active items (paginate through all pages). Return `[{title, url, price}]`,
where `url` is each listing's permalink (`/itm/<id>`) — the dedupe key SCAN matches against managed
items' `listing_urls`. Read-only; never edits or re-lists here.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Guardrails
- Logged-out / checkpoint / 2FA / captcha → **stop and escalate** ("re-auth your eBay"), no tight
  retry loop (account safety, `browser-actions.md`).
- Never enable auction or eBay Best Offer — all haggling flows through Bazaar's negotiation engine.
- **Currency:** list only in `seller_config.currency` (regional site → regional currency, e.g. SG →
  SGD). If it can't be set, escalate — never publish a price in the wrong currency (step 7).
- Publish gating follows `config.approvals.steps.publish` (see `skills/bazaar-config.md`).
- `--dry-run`: log each verb above instead of executing.
