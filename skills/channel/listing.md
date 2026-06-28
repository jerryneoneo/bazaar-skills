# LISTING flow — resumable, turn-based (responsive)

Listing is a multi-turn wizard, but each daemon pass must be **short and responsive** — so this
is a **state machine** persisted in `data/listing_session.json`, not one long blocking pass.
Each seller pass: load the session, do ONE step, send a progress/question message, return.
The seller always knows what's happening (the daemon also fires an instant "👀 on it" ack
before each pass, covering the claude cold-start).

> Talks to the seller via `channel.md` verbs; drives marketplaces via `browser-actions.md`;
> money via `bin/floor_gate.py` / `bin/negotiate.py`; fee preview via `bin/shipping.py`.
> Requires onboarding done. **Approvals** (`config.approvals.steps`, see `skills/bazaar-config.md`):
> each gated step below reads its key — `price_floor`, `listing_description`, `listing_platforms`,
> `publish` — where `auto` proceeds, `confirm` gates on `confirm()`. With the default `balanced`
> preset these are auto except `price_floor` (publishes without a confirm; pauses on a real anomaly).
> Eligible platforms are filtered by region (at onboarding) and item category (here) per
> `skills/marketplaces.md`.

## `data/listing_session.json`
```json
{ "active": true, "item_id": "<kebab>", "step": "awaiting_price",
  "fields": { "title","category","category_tag","condition","attributes","photos":[],
              "comp_low","comp_med","comp_high","list_price","floor","size_bucket" },
  "updated_at": "<iso>" }
```
`step` ∈ `identify → awaiting_price → awaiting_floor → awaiting_details → publishing → done`.
One step per pass; write the session and return after each. (Item **size is auto-determined** at
START and stated, not asked — a separate size question was removed; see `awaiting_floor`.)

## Routing (every seller pass)
Load `listing_session.json` **first**. If `active` and the new event is the seller's reply, apply it
to the current `step` **for `session.item_id`** — that file is the source of truth for which item
you're on. **Never ask the seller "which item is that for?" or re-identify the item while a session
is active; re-asking is a persistence bug** (e.g. a bare "$20" answering the price step belongs to
`session.item_id`, not a new item). If photos arrive with no active session AND the caption is not
buy-intent, START. A photo whose caption asks to ACQUIRE the item ("get this", "want", "looking
for", "find me", "under $X", "max", "budget", "bid", or a marketplace link to buy) is a BUY, not a
listing — do NOT START here; the control-channel intent gate (bazaar-run.md §1) routes it to
search.md. A photo with no caption, a sell-intent caption ("selling", "for sale"), or a bare item
description is a listing: START. Otherwise ignore (not a listing).

### START (photos, no active session, not buy-intent)
```
say "📸 Got your photos! I'll identify the item, research a fair price, and get it ready for
     <your enabled marketplaces>. I'll check the price with you, then publish, and message you at
     each step. (I only ship, no meetups.)"
     # <your enabled marketplaces> = the seller's ENABLED platforms named from
     # seller_config.marketplaces → marketplaces.json display_name (e.g. "Facebook Marketplace,
     # Carousell + eBay"). NEVER hardcode "FB + Carousell"; reflect what's actually enabled.
[vision] identify from the downloaded photos → title, category, condition, attributes,
         category_tag (one of the fixed taxonomy in skills/marketplaces.md; drives the publish
         filter), and size_bucket — auto-determine the delivery size (small|medium|large|bulky)
         from the item type (e.g. book/clothing=small, monitor/lamp=medium, large appliance=large,
         desk/sofa=bulky; default medium when unsure). Store in fields.size_bucket.
[WebSearch] "<title> used price <region>" → comp_low/med/high   (fast; skip slow browser comps here).
         If you run more than one comp query (e.g. add a "<title> sold price <region>" lookup for a
         tighter range), issue them as PARALLEL calls in ONE turn — never back-to-back. Comps depend
         on the title from [vision] above, so [vision] runs first; the comp queries then fan out.
say "🔎 Looks like a <title> (<condition>). Similar ones sell ~<low> to <high> <currency>."
ask "What list price do you want? (suggested <med>)"     → step=awaiting_price
write listing_session ; return
```

### awaiting_price → (seller replies a number)
```
fields.list_price = reply
ask "🔒 Your lowest acceptable price? PRIVATE, never shown to buyers, never in any reply.
     I'll only ever quote your <list_price>."          → step=awaiting_floor
return
```
> `approvals.steps.price_floor`: with `auto`, you may skip these two asks and use the suggested
> median as `list_price` and a default floor ratio (still written ONLY to the floor file). With
> `confirm` (the `balanced` default), ask both as shown above.

### awaiting_floor → (number)
```
write data/floors/<item_id>.json { item_id, list_price, floor, auto_counter_step,
                                   auto_counter_rounds, currency }   # FLOOR goes ONLY here
# Size is already auto-determined (fields.size_bucket from START's vision). STATE it, don't ask —
# the seller can correct it; otherwise we proceed. (No fee preview here: shipping.py needs the
# item record, which is written at PUBLISH; buyers are quoted the fee in-chat via shipping.py later.)
say "📦 I'll set delivery size to **<fields.size_bucket>** (typical for a <title>) — reply
     small / medium / large / bulky if that's off."
ask "Anything buyers should know? (what's included / condition notes), or reply 'skip'."
                                                          → step=awaiting_details
return
```

### awaiting_details → (text or 'skip')  ── then PUBLISH (the one long step, fully messaged)
```
# SIZE CORRECTION: if the reply is just a size keyword (small|medium|large|bulky), it's correcting
# the stated size, NOT item details → set fields.size_bucket = reply, re-ask the details question
# once (step stays awaiting_details), and return. Otherwise treat the reply as details below.
if not 'skip': append to data/qa_bank.jsonl {item_id,q:"details",a:<reply>,source:"frontloader"}
# description draft is gated by approvals.steps.listing_description: auto → use as drafted;
# confirm → show the draft and let the seller edit before writing the item.
write data/items/<item_id>.json (buyer-safe: title,category,category_tag,condition,list_price,
     currency, description[honest, ship-P2P, delivery quoted, NO meetup], photos, size_bucket,
     fulfillment:"ship_only", listing_urls:{}, status:"draft",
     managed:true, source:"bazaar", distribution_offered_at:null)  # NO floor/address
# ELIGIBLE platforms (skills/marketplaces.md, Consumption 2): region was filtered at onboarding;
# here filter the seller's ENABLED platforms by this item's category_tag.
registry = load data/marketplaces.json
eligible = [ id for id, sel in seller_config.marketplaces.items()
             if sel.enabled and registry[id].status=="active"
             and (item.category_tag in registry[id].categories or "*" in registry[id].categories) ]
if any enabled platform was dropped: say once why (e.g. "Poshmark is fashion-only, so this goes to
     FB + eBay only.")
if eligible is empty: say "None of your platforms accept a <category_tag> item, nothing to publish."
     ; session.active=false ; return
# listing_platforms gate: confirm → confirm(the eligible list) before publishing; auto → proceed.
# ANOMALY gate (auto_anomaly): anchor = min(web median, platform suggested price seen at publish)
if list_price > price_anomaly_ratio*anchor OR < 0.5*anchor:
    notify price-anomaly (notifications.md) ; step=awaiting_anomaly_decision ; return
say "✅ All set, publishing to <eligible joined> now. ~2-3 min, I'll message you when they're live."
step=publishing ; write session
published = []
for market in eligible:
    # IDEMPOTENT PUBLISH GUARD (also protects against a mid-flight pause/kill): if this item already
    # has a recorded live URL for <market> (items.listing_urls[market] set), it was already posted —
    # SKIP it (mark published, continue). A pass killed AFTER the platform published but BEFORE the
    # URL was written must NOT re-post on the next maint pass → that would create a duplicate live
    # listing. If unsure, re-read the live my-listings page for an existing listing of this item
    # before re-posting; treat a hit as already-published.
    if items.listing_urls.get(market): published.append(market) ; continue
    # account-safety pacing: reserve a slot before posting (atomic, per-marketplace; never self-count).
    # `python3 bin/pacing_gate.py reserve --marketplace <market> --kind publish`
    #   wait/quiet → skip this market this pass (leave eligible; it retries next maint pass), don't post.
    #   go → wait the returned delay_sec, then publish.
    # publish gate (approvals.steps.publish): auto → publish; confirm → confirm(preview) per recipe
    follow skills/listing-flows/<market>.md ; on login/field failure → notify + skip that market
    url = read live listing URL from the published page (browser DOM only — NEVER compose one)
    # HALLUCINATION + WRONG-REGION GUARD (deterministic): a URL is recorded ONLY if it passes the
    # registry check, AND (with --region) lands on the seller's regional site, not a foreign one.
    if not url OR `python3 bin/verify_listing_url.py --market <market> --url "<url>" --region <seller_config.region>` exits non-zero:
        notify the seller that <platform> publish failed (couldn't confirm a live regional link) ; skip market
        continue
    items.listing_urls[market] = url ; published.append(market)
if published is empty:
    items.status="draft" ; say "⚠️ Couldn't confirm a live listing on any platform — left as draft,
        nothing was posted. I'll retry / flag for you."  ; session.active=false ; return
items.status="live"
if items.published_at is unset: items.published_at = <now ISO-8601>   # stale-listing clock (listing_health.py);
     # idempotent — set once, never reset on re-publish/cross-list, so the silence window stays honest
say "🎉 Live!\n  <per-published market: <platform>: <url>>\nI'll watch for buyers and ping you
     for any offer or question."   # only markets in `published` (real, verified URLs) are listed
# DISTRIBUTE (gated by approvals.steps.distribution): after the item is live, see if it can reach
# further. Run only once per item (guard on items.distribution_offered_at).
session.active=false (step=done)   # the listing itself is complete
if items.distribution_offered_at is unset:
    d = python3 bin/distribution.py --item <item_id>
    if d.cross_list_candidates OR any(r.status=="active" for r in d.recommend_setup):
        # close listing_session (above) and open a distribution_session so the next pass continues there
        start skills/channel/distribution.md DISTRIBUTE (trigger_item_id=<item_id>)
    else: items.distribution_offered_at = <today>   # nothing to offer; don't re-check next pass
```
> The post-publish hand-off only DISTRIBUTEs the **just-listed** item (cross-list + recommend). It does
> **not** scan for the seller's other (manually-created) listings — that detection is owned by the
> autonomous cadence in `bazaar-run.md` §2b (`bin/scan_state.py`), and is also reachable on demand via
> `/sell-detect`. With `approvals.steps.distribution = auto`, obvious cross-lists happen silently; with
> `confirm` (the `balanced` default), the seller is asked first.

### awaiting_anomaly_decision → (list anyway / change price / skip)
Apply the seller's choice (change price → update floor/item, re-anchor), then resume at PUBLISH.

## Invariants (unchanged)
- **Listing URLs are READ, never written.** NEVER compose, infer, or message a listing link you did
  not read from the live published page in the browser. Every URL must pass
  `bin/verify_listing_url.py` before it is stored or shown. If publish fails or the URL can't be read
  from the DOM, report that market as failed — never invent a link. (cf. the `reply-pipeline.md`
  offer-number guard: a fabricated link is the listing-side "Harry incident".)
- **Floor** only in `data/floors/<id>.json` — never in the item, session, qa_bank, replies, or prompts.
- **Exact address** only in `seller_config.json` (read by `shipping.py`); buyers see a fee, not an address.
- **Ship-only**; **pacing/jitter/caps** stay on even unattended.
- **Resumable/idempotent:** state is the session file + cursors; a killed pass resumes from `step`.
  A seller reply during an `active` session always applies to `session.item_id`'s current `step` —
  the agent must load the session before responding and never re-ask which item it is.
- Each pass does **one** step and returns (except the final publish, which is messaged up-front).
