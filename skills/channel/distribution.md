# DISTRIBUTION flow — detect existing listings, manage them, recommend more platforms

Two related, post-listing capabilities, one skill (they share the trigger points, the
`bin/distribution.py` set logic, and the cross-list mechanism — they differ only in which set
they act on):

- **Detect & manage** — find listings the seller created **outside** Bazaar on a connected
  marketplace, and bring them under management (import → ask floor + size → watched + negotiated)
  and cross-list them to the seller's other enabled marketplaces.
- **Recommend** — suggest marketplaces the seller has NOT enabled but that suit an item
  (region + category match), set them up (enable + confirm login), and cross-list the item there.

> Talks to the seller via `channel.md` verbs; drives marketplaces via `browser-actions.md` and the
> per-site `skills/listing-flows/<market>.md` recipes; uses `bin/distribution.py` for the
> deterministic "where can this item live" sets. Requires onboarding done.
> **Approval:** every offer here reads `config.approvals.steps.distribution`
> (`skills/bazaar-config.md`): `auto` acts on obvious candidates, `confirm` asks first (the
> `balanced` default), `escalate` surfaces and parks. Cross-list publishing still honors
> `approvals.steps.publish`; setup login still uses `confirm()` like onboarding.

This is turn-based and resumable, exactly like `listing.md`: one step per pass, persisted in
`data/distribution_session.json`, so a long scan + a couple of seller questions never block a pass.

## `data/distribution_session.json`
```json
{ "active": true,
  "phase": "scan | import | distribute",
  "market": "<the market being scanned/imported, when relevant>",
  "queue": [ { "market","title","url","price","decision": null } ],
  "current_item_id": "<kebab, during import>",
  "step": "awaiting_manage | awaiting_floor | awaiting_size | awaiting_distribute | null",
  "trigger_item_id": "<item that started a post-publish DISTRIBUTE, if any>",
  "updated_at": "<iso>" }
```
`phase`/`step` drive routing. As with listing, a seller reply mid-flow is applied to the current
`step`; otherwise a fresh `/sell-detect` (or the post-publish hand-off) starts the relevant phase.

## Entry points
- **`/sell-detect`** (`.claude/commands/sell-detect.md`) → start **SCAN** across **all** enabled markets.
- **autonomous cadence** (from `bazaar-run.md` §2b) → start **SCAN** for the **one** market that
  `bin/scan_state.py due` reports overdue (cursor: `data/scan_state.json`, cadence
  `config.scan_interval_hours`). This is how listings made **outside** Bazaar get detected without the
  seller having to run `/sell-detect`; the loop scans one due market per pass and stamps it.
- **post-publish** (from `listing.md` after an item goes live) → start **DISTRIBUTE** for that one
  item (recommend + cross-list). It does **not** trigger SCAN — detection is owned by the cadence above.
- **inbox sweep enqueue** (from `skills/inbox-detect.md` TAKEOVER, sell branch) → when the user accepts
  taking over a **seller-initiated** untracked chat, the sweep appends that chat's anchor listing to
  `data/distribution_session.json.queue` (the same `{market,title,url,price,decision:null}` row shape
  SCAN produces). The **normalized-url dedup** in SCAN/IMPORT means a listing the my-listings SCAN
  already queued is never added twice. IMPORT then brings the listing under management as usual.

---

## SCAN — find listings Bazaar isn't managing
Scope: `/sell-detect` scans **all** enabled markets; the autonomous cadence (`bazaar-run.md` §2b)
passes a **single** `market` — scan only that one. `markets = [market]` when scoped, else every
enabled id.
```
for each id, sel in markets (enabled only):                                 # array→object shim first
    follow skills/listing-flows/<id>.md "Read my listings recipe"  → rows [{title, url, price}]
    on logged-out/checkpoint → notify re-auth for THIS market only (notifications.md), keep others
    for row in rows:
        u = normalize(row.url)                       # strip query/fragment, lower host, trim slash
        if any data/items/*.json has u in listing_urls.values() (normalized) → MANAGED, skip
        elif a managed item has a close title match (fuzzy) → candidate; mark row needs_confirm
        else → UNMANAGED → append to session.queue { market:id, ...row, decision:null }
if queue empty: say nothing if invoked post-publish; on /sell-detect say "✅ Scanned <markets> —
     everything live is already managed by me." ; session.active=false ; return
say "🔎 Found <n> listing(s) I'm not managing yet." ; phase=import ; write session ; return
```
Dedup anchor is the **normalized listing URL**; a fuzzy title match is only a *candidate* and must
be confirmed before it's treated as the same item (avoids a false merge). One market per pass is
fine — scanning is not on the hot loop.

## IMPORT — bring one unmanaged listing under management (per queue entry)
Pop the next `queue` entry with `decision == null`. Floor is **mandatory** (negotiation can't run
without it), so this reuses `listing.md`'s `awaiting_floor` / `awaiting_size` sub-steps verbatim.
```
ask (distribution gate) "📥 Found “<title>” (<price>) on <market> that I'm not managing.
     Manage it (I'll watch its chats + negotiate) and cross-list it elsewhere?"
     actions=[manage=Manage + distribute, skip=Leave it]          → step=awaiting_manage
```
### awaiting_manage → manage
```
if skip: mark entry decision="skip" ; next entry or → DISTRIBUTE/done
item_id = kebab(title) (+ short hash on collision)
guard: if any item already holds normalize(url) in listing_urls → already managed, skip (idempotent)
[vision/keyword] map to category_tag (fixed taxonomy in marketplaces.md; default "other" if unsure)
write data/items/<item_id>.json  (SAME buyer-safe shape listing.md PUBLISH writes):
    title, category, category_tag, condition, list_price=<scraped price>, currency,
    description (honest, ship-P2P, delivery quoted, NO meetup — regenerate if the source is thin),
    photos (optional; copy from the listing if readable, else []), size_bucket:null,
    fulfillment:"ship_only", listing_urls:{ <market>:<url> }, status:"imported_incomplete",
    managed:true, source:"imported", imported_at:<today>, distribution_offered_at:null
ask "🔒 Your lowest acceptable price for “<title>”? PRIVATE, never shown to buyers."
                                                                  → step=awaiting_floor
return
```
### awaiting_floor → (number)  [reuses listing.md awaiting_floor]
```
write data/floors/<item_id>.json { item_id, list_price, floor, auto_counter_step,
                                   auto_counter_rounds, currency }   # FLOOR goes ONLY here
ask "📦 Item size for delivery?" options=[small,medium,large,bulky]  → step=awaiting_size
return
```
### awaiting_size → (choice)  [reuses listing.md awaiting_size]
```
item.size_bucket = choice
if item has floor + size → item.status = "live"          # now fully managed
say "✅ Now managing “<title>”, I'll watch its <market> chats and negotiate for you."
current_item_id = item_id ; → DISTRIBUTE this item (offer cross-list), then next queue entry
```
Until floor+size are set the item stays `imported_incomplete`; `bin/floor_gate.py` already exits 3
on a missing floor, so the buyer loop safely **escalates** rather than mis-pricing. The buyer loop
(`sell-run`/`sell-watch`) picks up the imported item automatically — it iterates enabled markets and
matches each thread to an item by listing url / item_hint.

## DISTRIBUTE — cross-list + recommend (for one item)
Run for the just-imported item, and for the post-publish trigger item.
```
d = python3 bin/distribution.py --item <item_id>
# 1) CROSS-LIST to enabled platforms not yet listed
if d.cross_list_candidates:
    ask (distribution gate) "🌐 “<title>” isn't on <joined display_names> yet. Cross-list it there?"
        actions=[yes=Cross-list, no=Not now]                      → step=awaiting_distribute
# 2) RECOMMEND new platforms (active only; mention stubs once, don't offer)
active_recs = [r for r in d.recommend_setup if r.status=="active"]
stub_recs   = [r for r in d.recommend_setup if r.status=="stub"]
if active_recs:
    ask (distribution gate) "✨ “<title>” would also do well on <joined display_names> — want me to
        set <them> up and list there too?" actions=[setup=Set up + list, no=No thanks]
if stub_recs: say once "<joined> aren't supported yet, I'll let you know when they are."  # never offered
mark item.distribution_offered_at=<today>   # so the post-publish offer never nags twice
```
### awaiting_distribute → on yes (and on setup, after SETUP)
For each target market:
```
SETUP(market) [only for an accepted recommendation]:
    seller_config.marketplaces[<id>] = { enabled:true, auth:"unknown", connector:<registry type> }
    confirm "Are you logged in to <display_name> in your Chrome?" → on yes auth="confirmed"
            (on no: notify how to log in, skip this market for now)
CROSS-LIST(market):
    follow skills/listing-flows/<market>.md publish steps with the SAME item record + photos
    publish gate = approvals.steps.publish (auto → publish; confirm → confirm the preview)
    on login/field failure → notify + skip THIS market, continue others (per-market isolation)
    read live url → item.listing_urls[<market>] = url   (immutable rewrite of the item)
# Per-item completion ping — for a FOREGROUND/single cross-list (e.g. the post-publish hand-off of
# one just-listed item). DO send it here:
say "🎉 “<title>” is now also live on <joined>, same floor + shipping, all chats watched."
# EXCEPTION — blanket-confirmed BACKGROUND batch (session.blanket_distribute==true with several
# queued items, auto-draining one per maint pass): stay QUIET per item and report ONCE at Done
# below, so a batch of N doesn't fire N separate pings.
```
Take-down is unchanged: `bin/negotiate.py confirm-sold` + `notifications.md` already iterate
`listing_urls`, so a cross-listed item is pulled from **every** copy when it sells.

## Done
When `queue` has no `decision==null` entries and any trigger item's DISTRIBUTE is resolved:
`session.active=false`. On `/sell-detect`, end with a one-line summary (imported / cross-listed /
recommended counts).

**Always report a finished blanket batch to the channel.** When a blanket-confirmed batch
(`blanket_distribute==true`) drains to `active=false` — including in a background maint pass, where
per-item pings were suppressed above — send exactly ONE summary so the seller learns it completed:
```
say "✅ All <N> items are now also live on <joined markets>:
  <per item: “<title>” → <market>: <url>>   (compact list)
Same floor + shipping, every chat watched."
```
This is an OUTBOUND push (telegram.py send), so it is sent even by a background pass that does not
poll inbound. Do NOT also re-summarize on the next pass (guard on `active==false`).

## Invariants (same as listing.md)
- **Floor** only in `data/floors/<id>.json` — never in the item, session, queue, replies, prompts,
  or in `bin/distribution.py` output.
- **Exact address** only in `seller_config.json` (read by `shipping.py`); buyers see a fee.
- **Ship-only**; **pacing/jitter/caps** stay on (cross-list publishes count against the hourly cap).
- **Resumable/idempotent:** state is the session file; normalized URL is the dedupe key, so a killed
  pass or a re-run of `/sell-detect` never imports or cross-lists the same listing twice.
- **Stubs are never published to** (`listing_flow:null`, status `stub`) — only mentioned as upcoming.
- Each pass does **one** step and returns (scan one market; ask one question; cross-list one item).

## `--dry-run`
Scan/read and the `distribution.py` call run normally (read-only); IMPORT writes, SETUP writes, and
CROSS-LIST publishes are **logged, not executed** (per `browser-actions.md` dry-run).
