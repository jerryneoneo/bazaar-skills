# LISTING flow — resumable, turn-based (responsive)

Listing is a multi-turn wizard, but each daemon pass must be **short and responsive** — so this
is a **state machine** persisted in `data/listing_session.json`, not one long blocking pass.
Each seller pass: load the session, do ONE step, send a progress/question message, return.
The seller always knows what's happening (the daemon also fires an instant "👀 on it" ack
before each pass, covering the claude cold-start).
**Photo intake is settled + responsive:** the daemon coalesces a one-by-one photo burst (it waits a
few seconds for the rest) and sends an instant receipt ack, so START always sees the WHOLE batch in
one pass — never a partial set, and a later photo is never mis-read as a price reply. Identification
and pricing run as a **background research worker** when available (`data/research_results/`); this
flow reads its result if present and otherwise does the same vision+comps inline (the fail-closed
backstop), so a listing is never stranded by a worker that did not finish.

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
{ "active": true, "item_id": "<kebab>", "step": "researching",
  "batch_id": "<id|null>", "intent": "sell",
  "fields": { "title","category","category_tag","condition","attributes","photos":[],
              "comp_low","comp_med","comp_high","list_price","floor","size_bucket" },
  "updated_at": "<iso>" }
```
`step` ∈ `awaiting_intent → researching → awaiting_listing_inputs → publishing → done`.
One step per pass; write the session and return after each. `awaiting_intent` exists only for a
bare/unclear photo (ask sell-or-buy first, holding the photos); a clear sell caption skips straight
to `researching`. After the research, list price, floor, additional info, and delivery size are
gathered in **ONE combined ask** (the seller answers all of it in a single message). Item **size is
auto-determined** during research and stated for correction inside that combined ask, not asked as
its own step; see `awaiting_listing_inputs`.

## Routing (every seller pass)
Load `listing_session.json` **first**. If `active` and the new event is the seller's reply, apply it
to the current `step` **for `session.item_id`** — that file is the source of truth for which item
you're on. **Never ask the seller "which item is that for?" or re-identify the item while a session
is active; re-asking is a persistence bug** (e.g. a bare "$20" answering the price step belongs to
`session.item_id`, not a new item).
**Legacy step compat (migration):** a session persisted by an older version may carry a `step` of
`awaiting_price`, `awaiting_floor`, or `awaiting_details` (the pre-refactor names). Treat ANY of
these as `awaiting_listing_inputs` and continue there — its handler re-asks only the still-missing
required fields (list price, floor) and treats info as optional, so a stranded session resumes
cleanly. Write the session back with the canonical `awaiting_listing_inputs` so the legacy name is
migrated on first touch (never leave a session on a step name with no handler). A legacy `identify`
step (the old transient pre-research name) → resume at `researching` (re-run research from
`fields.photos`). A session on `awaiting_intent` is waiting for the seller's sell-or-buy choice;
apply their reply per that step (never re-ask which item — the photos are already in the session).
If photos arrive with no active session AND the caption is not
buy-intent, START. A photo whose caption asks to ACQUIRE the item ("get this", "want", "looking
for", "find me", "under $X", "max", "budget", "bid", or a marketplace link to buy) is a BUY, not a
listing — do NOT START here; the control-channel intent gate (bazaar-run.md §1) routes it to
search.md. A photo with no caption, a sell-intent caption ("selling", "for sale"), or a bare item
description routes here: START. Otherwise ignore (not a listing). START resolves intent FIRST — a
clear sell caption goes straight to research; a bare/description-only photo asks sell-or-buy
(`awaiting_intent`) before any work, holding the photos so they survive the answer.

### START (photos, no active session, routed here by the gate)
```
# The daemon already SETTLED the photo burst and sent an instant receipt ack ("📸 Got it…"), so the
# WHOLE batch is here in ONE pass — identify on the complete set, never a partial one, and do NOT
# re-ack mere receipt (that would be a double ack). Lead with the NEXT beat (the sell/buy ask, or the
# research narration). Never go silent between a beat and the work it promises (voice.md Rule 2).
[download] each pending photo → data/photos/<batch_id>/NN.jpg (channel.md ask_images / telegram.py
           getfile). Mint batch_id (short kebab from the caption, else a short uid). Stash
           fields.photos = [paths] and session.batch_id so they survive the sell/buy round-trip.
# INTENT — ask sell-or-buy ONLY when unclear (per the gate). A clear sell caption, or buy side
# unavailable, skips the ask.
if the routing caption clearly states SELL, OR the buy side is unavailable:
    session.intent = "sell" ; session.step = "researching" ; fall through to RESEARCH in THIS pass.
    # (RESEARCH writes the session at `researching` with fields.photos + batch_id, so the daemon
    #  spawns the background worker; this pass returns while it runs and presents when it lands.)
else (NEUTRAL — no caption / description-only):
    # FALLBACK ask (console / no-daemon): with the daemon, this branch is NOT reached — the daemon
    # asks sell-or-buy INSTANTLY (deterministic, no LLM latency) and opens the awaiting_intent
    # session itself, so a bare burst routes to `awaiting_intent` below, not here. This ask covers
    # the console adapter (no daemon) so the flow still works there.
    write listing_session { step:"awaiting_intent", intent:null, batch_id, fields.photos }
    ask "Quick one: want me to SELL this for you, or BUY one like it?"
        (options="sell=Sell it,buy=Buy one")
    return

### awaiting_intent → (waiting for the seller's sell-or-buy choice)
# The daemon OPENED this session (deterministic instant ask) and usually PRE-DOWNLOADED the photos
# into fields.photos already (so the background research worker can start immediately). Handle both
# what's pending and the choice:
if the inbound is PHOTOS and NO sell/buy choice yet (no "sell"/"buy" answer):
    # pass that just drains the pending burst — the daemon already asked sell-or-buy, so do NOT
    # re-ask and do NOT research yet. If fields.photos is ALREADY set (daemon pre-downloaded), just
    # consume the pending updates and return; only download here if fields.photos is still empty
    # (console / no-daemon fallback).
    if fields.photos is empty: [download] each pending photo → data/photos/<batch_id>/NN.jpg ; fields.photos = [paths]
    write listing_session ; return                      # stay on awaiting_intent
if the choice is BUY (button "buy" / "buy"-intent text):
    session.active=false (close listing_session) ; start skills/buying/search.md with fields.photos
    as the want's seed/visual context (a photo-seeded search). return.
if the choice is SELL (button "sell" / "sell"-intent text):
    if fields.photos is empty: [download] the pending photos → fields.photos   # answer arrived w/ photos
    session.intent="sell" ; step="researching" ; fall through to RESEARCH this pass.
# anything else (not photos, not a clear sell/buy) → re-ask the one sell/buy question; stay put.

### RESEARCH (step=researching) — present the BACKGROUND worker's findings (fail-closed to inline)
# A detached, browser-free worker (the daemon spawned it the moment the photos were stashed)
# identifies the item + finds comps WHILE the seller chooses/answers, and writes
# data/research_results/<batch_id>.json. So this step usually just PRESENTS a ready result; it does
# NOT block. The daemon re-fires this pass the instant the result (or a timeout failure) lands, so a
# proactive present needs no new seller message. The seller already saw an instant "identifying" ack.
ensure fields.photos is set (download the pending burst if it arrived with this event); keep batch_id.
# 1) RESULT READY → use it.
if data/research_results/<batch_id>.json exists:
    read it → fields.title, category, category_tag, condition, attributes, size_bucket,
              comp_low/med/high, currency.   # then PRESENT below.
# 2) WORKER FAILED/TIMED OUT (data/research_results/<batch_id>.failed exists) OR background research
#    is disabled (config.research_worker_enabled = false) → do it INLINE now (fail-closed; never
#    strand a listing on a worker that did not finish):
elif <batch_id>.failed exists OR config.research_worker_enabled is false:
    [vision] identify from fields.photos → title, category, category_tag (one of the fixed taxonomy
             in skills/marketplaces.md; drives the publish filter), condition, attributes, size_bucket
             (book/clothing=small, monitor/lamp=medium, large appliance=large, desk/sofa=bulky;
             default medium). Store in fields.*.
    [WebSearch] "<title> used price <region>" → comp_low/med/high   (parallel queries in ONE turn if
             more than one; comps depend on the title, so vision runs first).  # then PRESENT below.
# 3) WORKER STILL RUNNING → say NOTHING (the instant "identifying" ack already set expectations).
#    Keep step=researching and RETURN; the daemon presents the moment the result lands.
else:
    write listing_session (step=researching) ; return

# PRESENT (shared by 1 and 2):
say "🔎 Looks like a <title> (<condition>). Similar ones sell ~<low> to <high> <currency>.
     I'll get it ready for <your enabled marketplaces>."
     # <your enabled marketplaces> = the seller's ENABLED platforms named from
     # seller_config.marketplaces → marketplaces.json display_name (e.g. "Facebook Marketplace,
     # Carousell + eBay"). NEVER hardcode "FB + Carousell"; reflect what's actually enabled.
# ONE combined ask — list price + floor + optional info + the stated (auto) size. The seller answers
# all of it in a single message (e.g. "$12 listing, $10 floor, comes with dust jacket"). A bare
# number is read as the list price. NEVER split these across passes.
ask "To get it live, reply in one message:
     1) List price? (I suggest <med>)
     2) Your floor, the lowest you'd accept? 🔒 PRIVATE, never shown to buyers; I only ever quote
        your list price.
     3) Anything buyers should know (what's included / condition notes)? Optional, say 'skip'.
     I'll ship it as <fields.size_bucket> (typical for a <title>); tell me if that's off."
                                                          → step=awaiting_listing_inputs
write listing_session ; return
```

### awaiting_listing_inputs → (the seller's combined reply) ── parse, then PUBLISH (the one long step, fully messaged)
> `approvals.steps.price_floor`: with `auto`, skip the price+floor part of the ask (use the
> suggested median as `list_price` and a default floor ratio, still written ONLY to the floor file)
> and parse only any additional info. With `confirm` (the `balanced` default), require both list
> price and floor from the combined ask shown in START.
```
# PARSE the seller's free-text reply for THREE things (often all in one message, e.g.
# "$12 listing, $10 floor, comes with dust jacket"): list_price (required), floor (required),
# additional_info (optional). Rules:
#  - RESUMING a partial session (after a re-ask, or a migrated legacy session): if fields.list_price
#    is ALREADY set and floor is missing, a bare number is the FLOOR (NOT a new list price); if floor
#    is already set and list_price is missing, a bare number is the LIST PRICE. This takes precedence
#    over the bare-number rule below — only when BOTH are still unset does a bare number default to
#    the list price.
#  - a bare number with no floor cue (BOTH still unset) → it's the list_price; floor still missing.
#  - a number with a floor cue ("floor"/"lowest"/"min"), or a clear second number → that's the floor.
#  - a bare size keyword (small|medium|large|bulky) anywhere → SIZE CORRECTION: set
#    fields.size_bucket = that keyword (do NOT treat it as a price or as item details).
#  - 'skip'/'none'/no info text → additional_info empty.
#  - floor > list_price → ask the seller to confirm (a floor above list is unusual); stay in step, return.
# MISSING-REQUIRED: if list_price OR floor is still missing after parsing, ack what you DID capture
#   (NEVER echo the floor) and re-ask ONLY the missing field(s); keep step=awaiting_listing_inputs;
#   write session ; return.
fields.list_price = <parsed list price>
write data/floors/<item_id>.json { item_id, list_price, floor, auto_counter_step,
                                   auto_counter_rounds, currency }   # FLOOR goes ONLY here, never echoed
if additional_info given (not 'skip'):
     # TRUST GUARD (defense-in-depth, mirrors the buyer-safe "NO floor/address" item-file rule below):
     # additional_info is BUYER-FACING. Before storing, STRIP any exact address, phone/PayNow/bank
     # number, or offline-payment instruction ("paynow to <n>", "cash on collection", "pickup at
     # <address>", "leave it outside"). Those are private and must NEVER reach qa_bank, the item, or a
     # buyer reply (onboarding.md trust rules). If the seller's note is ONLY such details, store
     # nothing and say once: "I've kept your address and payment details private, they don't go on the
     # listing. Meetups and payment are arranged with the buyer at deal time, and the checkout link
     # handles payment + delivery for you (buyer protection, tracked shipping, zero fees)." Otherwise
     # store only the buyer-safe remainder:
     append to data/qa_bank.jsonl
     {item_id,q:"details",a:<buyer-safe additional_info>,source:"frontloader"}
# Size is already set (auto at START, or corrected from this reply). No fee preview here: shipping.py
# needs the item record (written at PUBLISH); buyers are quoted the fee in-chat via shipping.py later.
# IMMEDIATE ACK: the seller just answered, so they must hear back before any slow step. The "✅ All
# set, publishing…" say below (right before the publish loop) is that acknowledgement; never go
# silent after the seller replies.
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
say "✅ All set, listing on <eligible joined> now. I'll message you as each goes live."
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
    # A user-initiated listing is ATTENDED (the seller asked for this item, live), so use the SHORT
    # interactive jitter, and --block so the delay is slept server-side in ONE call (the LLM never idles
    # it across turns, which burns the turn budget and risks ending mid-publish). The per-hour cap and
    # quiet_hours floors are enforced identically in both modes — interactive only shortens the jitter.
    # `python3 bin/pacing_gate.py reserve --marketplace <market> --kind publish --mode interactive --block`
    #   wait/quiet → skip this market this pass (leave eligible; it retries next maint pass), don't post.
    #   go → publish immediately (--block already slept any jitter, so delay_sec=0 on return).
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
    items.status="draft" ; say "⚠️ Couldn't confirm a live listing on any platform, left as draft;
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
