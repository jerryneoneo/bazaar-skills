# Search flow — Facebook Marketplace (browser-mode)

Per-site recipe for SEARCHING Facebook Marketplace through the buyer's real logged-in Chrome. Uses
only the `browser-actions.md` vocabulary (goal-style; re-find controls each run). Called by
`skills/buying/search.md` once the want's query + category + region are known. **Read-only: never
messages a seller or clicks "Message"** — opening a thread is the liaison engine's job.

> A generic searcher drives FB via the browser (no consumer search API). If a first-party API/MCP
> becomes available it replaces this recipe behind the same step contract.

Inputs: the want record `data/wants/<want_id>.json` (query, category_tag, region, condition_pref,
optional `target_price`) + the buyer's `delivery_area` from `buyer_config.json` (for the search
radius). The secret max is NOT an input (at most an upper price bound passed after the user sets it).

Domain: `www.facebook.com/marketplace` (FB is global — `fb.domains["*"]`).

## Steps
1. `navigate("marketplace/search")` — FB Marketplace search; enter the want's `query`.
2. Apply the filters the page exposes (re-find each visually):
   - `location` + `radius` → set to the buyer's `delivery_area` (area/postcode) and a sensible radius
     so distance/locality shows.
   - `price`    → set min/max ONLY if `search.md` passed a price bound; else leave open.
   - `category` → choose the closest to `want.category_tag` if FB offers a category facet for the query.
   - `condition` → set if `want.condition_pref` is specific and FB exposes the facet; else leave all.
   - `sort`     → default "Suggested"; ranking re-scores anyway, so sort only shapes the first page.
3. `read_listing()` over the result grid → normalize each to the candidate schema in `search.md`
   (title, price, currency, url `/marketplace/item/<id>/`, thumbnail, location). **FB grid rows
   usually do NOT expose `seller_handle`, `condition`, or `posted_time` — set those `null`.** Parse
   `listing_id` from the `/item/<id>/` permalink. Skip rows marked Sold/Pending.
4. PAGINATE: scroll to load more until ~N good candidates or 2-3 screens, whichever comes first.
   Human-paced; **no tight loop**.
5. Return the candidate list to `search.md`.

## Validated specifics (fill in from a live run, mirrors listing-flows/fb.md)
- FB Marketplace search is location-scoped: the location+radius control gates everything, so set it
  first from `delivery_area`.
- Grid rows are sparse (title, price, thumbnail, locality). Richer fields (condition, seller, posted
  time) appear only on the item page, which search does NOT open — leave them `null` and let ranking
  work from what the grid gives.
- The price the grid shows is occasionally a range or "Free"; normalize to a number, skip rows with
  no parseable price.

## Guardrails (identical to listing-flows)
- Logged-out / checkpoint / captcha → **stop this market's pass and escalate** ("re-auth your
  Facebook"); keep the other markets running. No tight-loop retries.
- Human-paced: jittered scroll/clicks under the shared `max_actions_per_hour` cap.
- `--dry-run`: log each `navigate` / `read_listing` / scroll instead of executing.
