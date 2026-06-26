# Search flow — Carousell (browser-mode)

Per-site recipe for SEARCHING Carousell through the buyer's real logged-in Chrome. Uses only the
`browser-actions.md` vocabulary (goal-style; re-find controls each run). Called by
`skills/buying/search.md` once the want's query + category + region are known. **Read-only: this
recipe never messages a seller or clicks "Chat"** — opening a thread is the liaison engine's job.

> A generic searcher drives Carousell via the browser (no consumer search API). If a first-party
> Carousell search API/MCP becomes available, it replaces this recipe behind the same step contract
> with no change to the search flow (the architecture's api-mode connector).

Inputs: the want record `data/wants/<want_id>.json` (query, category_tag, region, condition_pref,
optional `target_price`) + the buyer's `delivery_area` from `buyer_config.json` (for distance). The
secret max is NOT an input here (it is at most an upper price-filter bound, passed by `search.md`
only after the user sets it).

Domain: pick the regional host from `marketplaces.json` `carousell.domains[region]` (e.g. SG →
`www.carousell.sg`); fall back to the account's logged-in locale.

## Steps
1. `navigate("search")` — Carousell search; enter the want's `query` string.
2. Apply the filters the page exposes (re-find each visually):
   - `category`  → choose the closest to `want.category_tag`.
   - `price`     → set max (and an optional min) ONLY if `search.md` passed a price bound; otherwise leave open.
   - `condition` → if `want.condition_pref` is `new`/`used`, set it; else leave all.
   - `location`  → set to the buyer's `delivery_area.area`/region so distance shows; prefer a
     "mail/delivery available" toggle if offered (this is a ship-only buyer).
   - `sort`      → "Recent" by default (recency matters for second-hand); ranking re-scores anyway.
3. `read_listing()` over the result rows → normalize each to the candidate schema in `search.md`
   (title, price, currency, url `/p/<slug>-<id>/`, thumbnail, location, distance_km if shown,
   seller_handle, condition, posted_time). **Skip Sold/Reserved rows.** Parse `listing_id` from the
   permalink (the `-<id>/` suffix) — the dedupe key + thread namespace.
4. PAGINATE: scroll to load more (or click "next") until ~N good candidates or 2-3 pages, whichever
   comes first. Account-safety: human-paced scrolling, **no tight loop**; stop at the cap.
5. Return the candidate list to `search.md` (it merges + de-dupes across markets and ranks).

## Validated specifics (fill in from a live run, mirrors listing-flows/carousell.md)
- The search query goes in the top search box; category/price/condition are facet controls in the
  left/Top filter bar; "delivery available" is a checkbox facet. Re-find them visually (DOM volatility).
- Distance/location shows on a row only when the account location is set; otherwise `distance_km` is
  `null` and ranking falls back to area-name match.
- Carousell shows `seller_handle` and a relative `posted_time` ("3 days ago") on most rows; normalize
  the relative time to an ISO date when possible, else leave `posted_time` null.

## Guardrails (identical to listing-flows)
- Logged-out / checkpoint / captcha / "confirm it's you" → **stop this market's pass and escalate**
  ("re-auth your Carousell"); keep the other markets running. No tight-loop retries (the
  anti-automation tell).
- Human-paced: jittered scroll/clicks under the shared `max_actions_per_hour` cap; never hammer
  pagination.
- `--dry-run`: log each `navigate` / `read_listing` / scroll instead of executing (zero side effects;
  search.md still writes the want/session files).
