# Listing flow — Carousell (browser-mode)

Per-site recipe for publishing one item to **Carousell** through the seller's real logged-in
Chrome. Uses only the `browser-actions.md` vocabulary (goal-style; re-find controls each run).
Called by `skills/channel/listing.md` after the seller confirms publish.

> A generic deployer drives Carousell via the browser (no consumer listing API). If a
> first-party Carousell API/MCP becomes available, it can replace this recipe behind the same
> step contract (architecture's api-mode connector) with no change to the listing flow.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

> **Region:** post on the seller's regional Carousell (SG → `www.carousell.sg`, MY →
> `www.carousell.com.my`, …). Use `host = seller_config.marketplaces.carousell.site` (or
> `python3 bin/resolve_domain.py --market carousell --region <seller_config.region>`) for step 1.

## Steps

> **Page-memory fast path** (per `skills/browser-actions.md` → "Page memory"). Before each `type`/
> `click` below, try the cached selector first: `python3 bin/ui_cache.py get --market carousell
> --flow listing --step <step>`, then act + verify with a **single** `browser_evaluate`; on any miss /
> ambiguity / failed verify, `invalidate` it and fall back to the goal-style step here, then
> `record` the freshly-found selector. Step → id map: upload-photos=`add_photos_button`,
> category=`category_selector`, title=`title_field`, price=`price_field`,
> description=`description_field`, condition=`condition_selector`, list-now=`publish_button`. The
> cache never bypasses step 9's `confirm`, the anomaly anchor, or the `pacing_gate.py` reservation —
> the final `click("list-now")` stays on the normal path below; do not submit it via `browser_evaluate`.

1. `navigate("https://<host>/sell/new")` — Carousell "Sell" / list-an-item flow on the regional
   site (`<host>` resolved above).
2. `click("upload-photos")` → attach the item's photo paths.
3. `click("category")` → choose closest to `item.category`.
4. `type("title", item.title)`
5. `type("price", item.list_price)`  (currency follows the seller's account locale)
6. `type("description", item.description)` — ship-P2P / delivery-quoted wording; **no meetup.**
7. `click("condition")` → set from `item.condition`.
8. Set delivery option to **mail/courier delivery enabled** (P2P), buyer pays delivery; do not
   set meetup-only. (Exact fee quoted in-chat via `shipping.py`.)
9. `read` the preview → return for the caller's final `confirm`. On confirm: `click("list-now")`.
10. `read` the live listing URL (`/p/<...>`) from the published page → return it.
    **Only return a URL you actually read from the DOM — never compose one.** If publish didn't
    complete or no permalink is readable, return no URL (the caller treats this market as failed and
    validates every URL via `bin/verify_listing_url.py` before recording it).

## Validated specifics (from a live run)
- Carousell auto-detects category, title, condition, brand, and a **suggested/sold price** from
  the photos — read that price widget and return it as the anomaly anchor (`listing.md` 5b).
- Upload via the hidden file input (`setInputFiles`); the "Save & close" photo dialog and the
  "List now" button need JS-click (overlay/focus-trap intercepts normal clicks).
- After "List now", dismiss the post-list **tour** ("Next" ×N → "Okay") and the **satisfaction
  survey** (close), then read the live URL from the seller's profile/listing.
- Set delivery = Carousell delivery enabled; **remove the saved meet-up location** (ship-only).

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(items.listing_urls.carousell)` → open the listing's manage menu (•••) →
**"Mark as sold"** (preferred) or "Delete". Confirm it no longer shows as available.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
`navigate` to the seller's profile → **"Listings" / "Selling"** tab → `read_listing()` over the
seller's own **active** listings (scroll to load all; skip Sold). Return `[{title, url, price}]`,
where `url` is each listing's permalink (`/p/<slug>-<id>/`) — the dedupe key SCAN matches against
managed items' `listing_urls`. Read-only; never edits or re-lists here.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Read buyer inbox recipe (called by `.claude/commands/sell-watch.md` / `selly-run.md` §2)
The **buyer-message** read path (distinct from "Read my listings"). Documented for parity with
`fb.md`: Carousell works today via the simple `/inbox/` URL the `bin/buyer_peek.py` probe watches,
but pinning the recipe keeps the buyer pass from silently failing if the DOM shifts.

1. `navigate("https://<host>/inbox/")` — the Carousell Inbox (chat list); the unread count is the
   nav badge the peek probe reads. You drive a dedicated Chrome, so a Carousell tab not already being
   open is NORMAL — this `navigate` OPENS one. A missing tab is NOT a failure and NOT "inbox
   unreadable"; never escalate or tell the seller to open Chrome for it. Escalate ONLY if, after
   navigating, Carousell is logged-out/checkpoint (→ "re-auth your Carousell") or the inbox still
   won't render after one retry.
2. `read_inbox()` → `[{thread_id, buyer_handle, item_hint, unread, last_snippet}]`. Namespace
   `thread_id` as **`carousell:<id>`** (the conversation/chat URL id if exposed, else a stable
   `buyer_handle + item_hint` key).
3. For each thread **past its cursor** (`data/threads/carousell:<id>.json`), `status` not in
   `{escalated, lost}` (`escalated` = waiting on the seller; `lost` = terminal, never re-engage):
   `read_thread(thread_id)` → ordered `[{msg_id, dir, text, ts}]`. Handle only
   new `dir:"in"` messages; never reply to your own (`dir:"out"`).
4. Hand each new buyer message to `skills/reply-pipeline.md`, then advance the cursor. Idempotent.

Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Relist offer recipe (called by `skills/channel/relist-offer.md`)
The Carousell **assistant** (`carousell_assistant`) proactively offers to relist / renew / bump a
seller's listing, or asks "Is this still available?" (confirming refreshes it). This recipe reads the
actual control, decides **free vs paid (fail-closed)**, and takes ONLY the free action. Inputs: the
target `item_id` + its `listing_urls.carousell`.

> **THE HARD RULE — never spend.** Never click a control that costs Carousell **coins** or money.
> Carousell's paid promotion (**"Spotlight"**, **"Promote"**, a coin-priced "Bump") is OUT of scope,
> always. Only a genuinely free relist / renew / "still available" confirmation may be clicked.

1. Reach the relist control by EITHER path the assistant presented it on:
   - in the assistant chat: `navigate("https://<host>/inbox/")` → open the `carousell_assistant`
     conversation → find the offered action (a "Relist" / "List it again" / "Yes, still available"
     button or link), **or**
   - on the listing: `navigate(items.listing_urls.carousell)` → open the manage menu (`•••`) → look
     for a free **"Relist"** / **"Renew"** option (NOT "Promote" / "Spotlight").
2. **Read the control + any confirmation dialog before clicking.** Classify the action:
   - `paid` if the button/dialog shows ANY of: a coin count or coin icon, a currency amount, the
     words "coin(s)", "purchase", "buy", "Spotlight", "Promote", "Boost <price>", or a balance/top-up
     prompt. **Return `paid` and click nothing.**
   - `free` ONLY if the action is explicitly free: a plain "Relist" / "List again" / "Renew" or a
     "Yes, it's still available" confirmation with **no charge anywhere** in the button or its
     confirmation. 
   - `unknown` if you cannot clearly confirm it is free (text unclear, dialog did not render, an
     unexpected screen). **Return `unknown` and click nothing.**
3. **Only on `free`** (and after the caller's `pacing_gate.py reserve`): click the free relist /
   confirm control. If a coin / top-up / payment screen appears at ANY point after the click, **stop
   immediately, dismiss it, and report `paid`** (do not proceed, do not confirm a purchase). If
   Carousell responds "already relisted recently" / "you can relist again in N days", treat it as
   **done** (the caller stamps the cooldown), not a failure.
4. Return the decision (`free` acted / `paid` / `unknown`) to the caller. `--dry-run` → log the read
   + the decision, click nothing.

Logged-out/checkpoint → stop + escalate re-auth (no tight retry), same as the recipes above.

## Guardrails
- Logged-out / checkpoint / verification → **stop and escalate** ("re-auth your Carousell"),
  no tight-loop retries.
- Publish gating follows `config.approvals.steps.publish` (auto → `click("list-now")`; confirm →
  `confirm()` the rendered preview first). See `skills/selly-config.md`.
- `--dry-run`: log each verb instead of executing.
