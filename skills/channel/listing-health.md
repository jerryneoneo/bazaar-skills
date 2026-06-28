# Listing health — suggest fixes for a stale listing

When a LIVE listing has had no buyer interest (no inbound message on any of its threads) for
`stale_days` (default 7), the agent proposes CONCRETE ways to improve it. Detection is deterministic
(`bin/listing_health.py` — see `skills/bazaar-config.md` for the config keys); THIS skill is the
compose step the **MAINT pass** runs, one item per pass, from the session baton
`data/listing_health_session.json`. The agent only **suggests** — it never auto-applies a change.

> **Load config first.** `data/config.json` for `max_actions_per_hour` / `quiet_hours` (pacing still
> applies even though this pass only sends a channel message, not a marketplace action) and
> `data/seller_config.json` for `currency` + enabled `marketplaces`. Voice per `skills/style.md`:
> warm, plain, **no em-dashes**.

## 1. Read the baton + the listing
Read `data/listing_health_session.json`: `item_id` + `stale_row` (`silent_days`, `basis`
[`since_inbound` | `no_inbound`], `last_inbound_ts`, `list_price`, `currency`). Load
`data/items/<item_id>.json` for title, category, condition, description, `photos`, `listing_urls`.

**Terminal guard (first, hard):** if `items.status != "live"` (sold / removed / cancelled since the
episode opened), send NOTHING, set the session `active=false`, and STOP. Do **not** run
`listing_health.py mark` — the episode is void (a removed item is not "stale", it is gone).

## 2. Research comps for THIS one item (cost-disciplined)
At most ~2 **parallel** `WebSearch`/`WebFetch` queries for the item's current resale comps (same
make/model/condition, the seller's region). NO browser comps, NO touring other listings. One item per
pass — the cadence (`listing_health_interval_hours`) drips the backlog out so this stays cheap.

## 3. Compose CONCRETE suggestions (only the ones that genuinely apply)
Pick the few highest-leverage fixes. Be specific and actionable, not generic advice:

- **Price vs comps** — if `list_price` is above the current median, suggest a specific number or
  range ("drop from $40 to about $32, that's where similar ones are selling"). **Do NOT suggest a
  drop if the price is already at or below comps** (this is the "already dropped" guard — re-read the
  current price against fresh comps before suggesting).
- **Photos** — if there are fewer than 3, suggest adding angles / better lighting / a scale or
  in-use shot. Call out a missing hero shot.
- **Title / description** — name the keyword or spec gaps vs comps (model number, size, condition
  detail, what's included). Flag a thin or vague description.
- **Reach / distribution** — if `listing_urls` is missing an enabled, eligible market, suggest
  cross-listing. **Skip this if the item is already flagged `undistributed` in the catch-up digest**
  (triage owns that signal — do not duplicate the nudge).
- **Bump / relist** — platform-specific freshness (Carousell bump, FB relist) as the fallback when
  nothing else clearly applies.

Frame the basis honestly: `no_inbound` = "no one has messaged about it yet in N days";
`since_inbound` = "no new interest since the last message N days ago".

## 4. Send ONE message + close the episode
Send ONE control-channel notice via `skills/channel/notifications.md` (`notify`, `ref=item_id`),
voice per `skills/style.md`, **no em-dashes**, framed as suggestions the seller can approve — never
as changes you already made. Example shape (adapt, do not template):

> "Heads up: \"<title>\" has had no buyer interest in <silent_days> days. A few things that could
> help: <2-4 concrete suggestions>. Want me to do any of these?"

Then run `python3 bin/listing_health.py mark --item <item_id>` (stamps the warn ledger so the seller
is not nagged again until `rewarn_days` or the item re-engages then goes cold again), set the session
`active=false`, and STOP. One item per pass.

## 5. If the seller replies
Their reply lands on the control channel and is handled by the channel pass
(`skills/channel/notifications.md`). Applying an accepted fix reuses the normal flows: a price change
or relist runs the relevant listing/distribution step; a cross-list opens `distribution.md`. Nothing
here auto-applies.
