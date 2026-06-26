# Listing flow — FB Marketplace (browser-mode)

Per-site recipe for publishing one item to **Facebook Marketplace** through the seller's real
logged-in Chrome. Uses only the `browser-actions.md` vocabulary (goal-style: re-find controls
visually each run; a moved button is re-found, not a break). Called by `skills/channel/listing.md`
once the seller confirms publish.

Inputs: the item record `data/items/<item_id>.json` (buyer-safe) + its photo paths.

> **Region:** Facebook is one global host. Use `host = seller_config.marketplaces.fb.site` (or
> `python3 bin/resolve_domain.py --market fb --region <seller_config.region>` → `www.facebook.com`)
> for step 1; Marketplace localizes by the logged-in account, not the domain.

## Steps

> **Page-memory fast path** (per `skills/browser-actions.md` → "Page memory"). Before each `type`/
> `click` below, try the cached selector first: `python3 bin/ui_cache.py get --market fb --flow
> listing --step <step>`, then act + verify with a **single** `browser_evaluate`; on any miss /
> ambiguity / failed verify, `invalidate` it and fall back to the goal-style step here, then
> `record` the freshly-found selector. Step → id map: add-photos=`add_photos_button`,
> title=`title_field`, price=`price_field`, category=`category_selector`,
> condition=`condition_selector`, description=`description_field`, Next (the details→audience step,
> see Validated specifics)=`next_button`, publish=`publish_button`. The cache never bypasses step 9's
> `confirm`, the anomaly anchor, or the `pacing_gate.py` reservation — the final `click("publish")`
> stays on the normal path below; do not submit it via `browser_evaluate`.

1. `navigate("https://<host>/marketplace/create/item")` — the "Create new listing → Item for sale"
   flow (`<host>` resolved above).
2. `click("add-photos")` → attach the item's photo paths (largest/clearest first).
3. `type("title", item.title)`
4. `type("price", item.list_price)`
5. `click("category")` → choose the closest match to `item.category`.
6. `click("condition")` → set from `item.condition`.
7. `type("description", item.description)` — includes the ship-P2P / delivery-quoted wording.
   **No meetup/pickup phrasing.**
8. Set delivery/shipping options to **shipping enabled** (buyer pays delivery); do **not** enable
   local pickup-only. (Exact delivery fee is quoted in-chat via `shipping.py`, not set here.)
9. `read` the rendered preview → return it to the seller for the final `confirm` (done by the
   caller). On confirm: `click("publish")`.
10. `read` the published listing URL from the live page (`/marketplace/item/<id>/`) → return it.
    **Only return a URL you actually read from the DOM — never compose one.** If publish didn't
    complete or no permalink is readable, return no URL (the caller treats this market as failed and
    validates every URL via `bin/verify_listing_url.py` before recording it).

## Validated specifics (from a live run)
- Two-step composer: fill details (Title, Price, Category="Electronics & computers",
  Condition, Brand, Description) → **Next** → audience step (leave groups unchecked) → **Publish**.
- Upload via the hidden file input (`setInputFiles`). FB SG has **no integrated courier** — only
  meet-up prefs; leave them unchecked and word the description as "ship via chat (buyer pays P2P
  courier)". After publish, FB redirects to `marketplace/you/selling`; close the "Boost" promo and
  read the item permalink (`/marketplace/item/<id>/`).
- FB shows no reliable sold-price widget → use the web-comp median as the anomaly anchor here.

## Take-down recipe (single inventory — called by notifications.md "sale → done")
`navigate(marketplace/you/selling)` (or the item permalink) → open the listing's **•••** menu →
**"Mark as Sold"** (preferred) or "Delete listing". Confirm it's no longer active.

## Read my listings recipe (called by `skills/channel/distribution.md` SCAN)
`navigate("marketplace/you/selling")` → `read_listing()` over the seller's own **active** listings
(scroll/paginate to load them all; skip ones already marked Sold). Return `[{title, url, price}]`,
where `url` is each item's permalink (`/marketplace/item/<id>/`) — the dedupe key SCAN matches
against managed items' `listing_urls`. Read-only; never edits or re-lists here.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

## Read buyer inbox recipe (called by `.claude/commands/sell-watch.md` / `bazaar-run.md` §2)
This is the **buyer-message** read path — distinct from "Read my listings" above. Without it the
buyer pass calls `navigate(fb inbox)` with nowhere to go and silently fails, so unread FB buyer
enquiries pile up (the cheap `bin/buyer_peek.py` probe sees the unread badge but the pass can't act).

1. `navigate("https://<host>/marketplace/inbox")` — the **Marketplace Inbox**, i.e. the seller's
   buyer chats about their listings. This is NOT the general Messenger inbox (`/messages`): FB
   Marketplace conversations live under Marketplace's own "Inbox". If that URL doesn't resolve, reach
   it from the Marketplace left rail **"Inbox"** link, or from `marketplace/you/selling` → a
   listing's message count. (Messenger's "Marketplace" folder is a last-resort fallback.)
2. `read_inbox()` → the conversation list: `[{thread_id, buyer_handle, item_hint, unread,
   last_snippet}]`. `item_hint` is the listing title shown on the row; `unread` is the blue dot /
   bold row. Namespace `thread_id` as **`fb:<id>`** (per `skills/browser-actions.md` §Identifiers):
   use the conversation URL id if exposed, else derive a stable key from `buyer_handle + item_hint`.
3. For each thread **past its cursor** (`data/threads/fb:<id>.json` → `cursor.last_handled_msg_id`)
   with new activity, and whose `status` is not in `{escalated, lost}` (`escalated` = waiting on the
   seller; `lost` = terminal, never re-engage): `read_thread(thread_id)` →
   ordered `[{msg_id, dir:"in"|"out", text, ts}]`. `dir:"in"` = the buyer; `dir:"out"` = you —
   **never reply to your own messages**; only handle new `dir:"in"` messages past the cursor.
4. Hand each new buyer message to `skills/reply-pipeline.md` (classify → gate → compose → `send()`),
   then advance the thread cursor. Idempotent: re-running the pass re-reads but never double-replies.

Tactics (goal-style — re-find visually each run, don't hard-code selectors): the conversation often
opens in a **side panel / overlay**; if a normal click on a row is intercepted, JS-click it
(`element.click()`, per `browser-actions.md`). The reply box is the message composer at the bottom of
the open thread (`type` the reply → `send()`). Opening a thread marks it read — fine, the cursor is
the source of truth, not the unread badge.
Logged-out/checkpoint → stop this market + escalate re-auth (no tight retry). `--dry-run` → log only.

> **Validated specifics (live FB desktop, 2026-06-26).** `https://www.facebook.com/marketplace/inbox/`
> renders the Marketplace messaging UI: left Marketplace rail, a middle list with **Selling | Buying**
> tabs, the open thread on the right. Read the **Selling** tab. **This UI resists plain
> `snapshot`+`click` (rows carry no stable ref/href), so drive it with `browser_evaluate`** (the buyer
> pass now ships the `evaluate` tool for exactly this). Without `evaluate` the pass detects FB unread
> but replies to none — the verified failure mode this recipe fixes. Concrete, validated flow:
>
> 1. **DO NOT use `[role="row"]` or `a[href*="/t/"]`** — those are the general Messenger "Chats" (Meta
>    Business, random DMs), NOT the marketplace Selling threads. Reading them makes the inbox look empty.
> 2. **List the Selling rows** (`browser_evaluate`): each renders as text `"<buyer> · <listing title>"`
>    followed by a preview/status line; unread/needs-reply shows `"<buyer> is waiting for your response."`
>    or `"<buyer> sent you a message about your listing: <title>"`. Collect `{buyer, item_hint}` for rows
>    whose item matches one of your `data/items/*.json` titles.
> 3. **Open one** (`browser_evaluate`): find the smallest element whose `textContent` contains
>    `"<buyer> · <item>"` and `.click()` it (walk to a clickable ancestor if needed). Confirm it opened:
>    the composer is `div[contenteditable="true"][aria-label^="Write to"]` and its `aria-label` reads
>    `"Write to <buyer> · <item>"` — verify this matches the buyer you intend BEFORE typing (guards
>    against replying to the wrong thread while it loads).
> 4. **Read the buyer's message** (`browser_evaluate`): the open thread's messages are exposed as
>    aria-labels of the form `"At <time>, <sender>: <text>"` / `"Message sent <time> by <sender>: <text>"`.
>    Read the buyer's latest text from those (the list preview only says "waiting for your response").
> 5. **Classify → route** (`skills/reply-pipeline.md`): a plain availability/condition/shipping question
>    → answer (gate `buyer_replies`, auto). A **price offer** (a number, "can do $X", "$X for both",
>    anything below list) → DO NOT auto-reply; escalate via `skills/channel/notifications.md` (gate
>    `offers` = confirm). A **meetup** request → reply ship-only (islandwide P2P, no meetups). All items
>    are `ship_only`; prices come from `data/items/<id>.json.list_price`; the floor stays in `floor_gate`.
> 6. **Reply** (`browser_type` into `div[contenteditable="true"][aria-label^="Write to"]`, submit=Enter).
>    Reply naturally, no identity line (`skills/voice.md` Rule 3). **Verify it sent**: the thread now
>    shows an aria-label `"At <time>, You: <your text>"`. Then move to the next row.
> 7. **Idempotency:** sending marks the thread read, so it drops its "waiting"/"sent you a message"
>    status; the next pass only opens rows that still show it, so no double-reply. A fresh buyer message
>    re-surfaces the status and is handled again. (Cheap gate: `bin/buyer_peek.py` reads the aggregated
>    `"Marketplace · N new messages"` row to decide there's FB selling work — non-zero ⇒ open + sweep.)
> 8. **One pass won't clear a 20+ backlog** — reply to as many as the turn budget allows; read-marking
>    makes progress stick across passes, and the cadence sweeps again until the list is clear.

## Guardrails
- Logged-out / checkpoint / captcha → **stop and escalate** ("re-auth your FB"), do not retry
  in a loop (account safety, `browser-actions.md`).
- Publish gating follows `config.approvals.steps.publish` (auto → `click("publish")`; confirm →
  `confirm()` the rendered preview first). See `skills/bazaar-config.md`.
- `--dry-run`: log each verb above instead of executing.
