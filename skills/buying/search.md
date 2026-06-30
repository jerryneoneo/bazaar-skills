# SEARCH flow — resumable, turn-based (the buyer-side mirror of listing.md)

Discovery is a multi-turn wizard, but each pass must be **short and responsive** — so this is a
**state machine** persisted in `data/buy_session.json`, not one long blocking pass. Each pass: load
the session, do ONE step, send a progress/question message, return. The user always knows what's
happening (the daemon also fires an instant "👀 on it" ack before each pass).

> Talks to the user via `channel.md` verbs; drives marketplaces via `browser-actions.md` (search,
> read rows); the secret max budget goes via `bin/budget_gate.py` (written here, read only there).
> Requires onboarding done (`data/buyer_config.json`). **Approvals** (`config.approvals.steps`, see
> `skills/selly-config.md`): `buy_search` gates running a search (`auto` runs it, `confirm` confirms
> the parsed query/filters first); `above_budget` gates pursuing a listing priced above the secret max
> (hard-floored to `confirm`/`escalate`, never `auto`). Eligible platforms are filtered by region (at
> onboarding) and the want's category (here) per `skills/marketplaces.md`.

## `data/buy_session.json`
```json
{ "active": true, "want_id": "<kebab>", "step": "recommend",
  "fields": { "query","category_tag","condition_pref","region",
              "candidates":[], "shortlist":[], "target_price","price_range_asked" },
  "updated_at": "<iso>" }
```
`step` ∈ `understand → searching → recommend → awaiting_price_range → awaiting_confirm → liaising → done`.
One step per pass; write the session and return after each. `candidates` in the session may be kept
empty/short — the authoritative copy is the want file (mirror of how `listing_session.fields` is a
working copy of the item).

## The want record `data/wants/<want_id>.json` (buyer-safe — the mirror of `data/items/<id>.json`)
```json
{ "want_id":"sony-wh1000xm5-silver", "query":"Sony WH-1000XM5 silver, used, with case",
  "category":"Audio / Headphones", "category_tag":"electronics", "condition_pref":"used",
  "region":"SG", "currency":"SGD", "target_price":90,
  "candidates":[ /* normalized candidate rows, see below */ ],
  "shortlist":["carousell:1444964225"], "chosen":["carousell:1444964225"],
  "thread_ids":["carousell:1444964225"],
  "status":"liaising", "source":"selly", "created_at":"<iso>" }
```
`status` ∈ `searching | shortlisted | liaising | agreed | bought | abandoned`. **No `max_budget`
field — ever.** The ceiling lives only in `data/budgets/<want_id>.json`.

> **`source`:** `"selly"` for a want created here via `/search`; `"imported"` for one created (or
> linked) by `skills/inbox-detect.md` when the user takes over chats they started by hand. An imported
> want skips the search steps — it is seeded straight into `liaising` with pre-filled `thread_ids` and
> a budget the takeover flow asks for. This want file stays the single source of truth either way.

**Normalized candidate row** (one per result, merged across markets):
```json
{ "market":"carousell", "listing_id":"1444964225", "title":"...", "price":95, "currency":"SGD",
  "url":"https://www.carousell.sg/p/...-1444964225/", "thumbnail":"...", "location":"Bishan",
  "distance_km":3.1, "seller_handle":"alberton10002", "condition":"Used - Good",
  "posted_time":"...", "rank_score":0.92, "rank_why":"exact model+colour, with case, 3km, under target" }
```
`market`+`listing_id` is the dedupe key and the `<market>:<listing_id>` thread namespace. Fields a
market doesn't expose are `null`. `url` is **READ from the DOM, never composed.**

## Routing (every buyer pass)
Load `buy_session.json` **first**. If `active` and the new event is the user's reply, apply it to the
current `step` **for `session.want_id`** — that file is the source of truth for which want you're on.
**Never ask "which want is that for?" while a session is active; re-asking is a persistence bug** (a
bare "$80" answering the price-range step belongs to `session.want_id`). If a fresh buy-intent
message arrives with no active session, START — whether it is plain text ("I want a …", "looking
for …", "find me …", "under $X") OR a photo with a buy-intent caption (a snapshot of an item the
user says they want). When a photo is present, use it as visual context to parse the want (model,
colour, condition); the photo is NOT a listing. Otherwise ignore (not a search). The control-channel
intent gate (selly-run.md §1) guarantees only ONE of search.md / listing.md starts on any fresh
message.

### START → understand (free-text want, no active session)
```
[LLM understand] parse the user's want → query, category_tag (one of the fixed taxonomy in
   skills/marketplaces.md), condition_pref (any|new|used), region (default buyer_config.region).
   # If the fresh message included a photo, [vision] read it for model/brand/colour/condition
   # and fold those into `query` (the photo is buy context, never a listing). No photo is fine.
want_id = kebab(query) (short hash on collision)
write data/wants/<want_id>.json (status:"searching", candidates:[], created_at:now)
# buy_search gate: auto → proceed; confirm → confirm the parsed query + filters first.
ack "On it, searching <your enabled marketplaces> for <query> now, back shortly."
     # <your enabled marketplaces> = buyer_config.marketplaces enabled × marketplaces.json
     # display_name (e.g. "Facebook Marketplace + Carousell"). NEVER hardcode the names.
step=searching ; write session ; (continue straight into searching this pass — it's the slow step)
```

### searching → (the one slow step, fully messaged up-front)
```
registry = load data/marketplaces.json  (apply the array→object / read-shims in skills/marketplaces.md)
eligible = [ id for id, sel in buyer_config.marketplaces.items()
             if sel.enabled and registry[id].status=="active"
             and (want.category_tag in registry[id].categories or "*" in registry[id].categories) ]
if a market was dropped for this category: say once why (e.g. "Poshmark is fashion-only, skipping it").
candidates = []
for market in eligible:
    follow skills/search-flows/<market>.md  (query, category, region/area, condition_pref; NO max yet)
    on logged-out/checkpoint/field failure → notify + skip that market (keep the others running)
    candidates += the normalized rows it returns
# de-dupe by (market, listing_id) and near-dupes (same title+price); drop rows whose url fails
#   `python3 bin/verify_listing_url.py --market <market> --url "<url>"` (hallucination guard).
write want.candidates = candidates ; step=recommend ; write session
```

### recommend → RANK, surface the shortlist, then ask the price range
```
# RANK = your judgment over the normalized rows (NOT a money calc): relevance to the stated want,
#   price, condition vs condition_pref, recency, location/distance. Write rank_score + a one-line
#   rank_why on each candidate; keep the top N (~5).
if no candidates: say "Couldn't find any <query> on <markets> right now. Want me to widen the search
   (different keywords / condition / price)?" ; step=understand ; return
say a compact, scannable shortlist:
   "Found <count>. Top picks:
     1. <title> — <price> <currency> · <condition> · <location/distance> · <market>
        <url>
     2. ...
   Want me to chase any of these? And what's your price range — a target you'd love, and a max you
   won't go over?"     # the price-range ask lands HERE, after surfacing (per the user)
step=awaiting_price_range ; write session
```

### awaiting_price_range → (user replies target + max)
```
fields.target_price = <target> ; want.target_price = <target> ; fields.price_range_asked = true
# SECRET BOUNDARY — write the max ONLY to the hidden budget file (mirror of how listing.md writes
#   data/floors/). The max never enters the want, the session, any message, or any prompt again.
write data/budgets/<want_id>.json {
   want_id, target_price:<target>, max_budget:<max>,
   opening_ratio:0.8, auto_counter_step:5, auto_counter_rounds:3, give_up_polls:6, currency }
# optionally re-rank / de-prioritize candidates whose price > <max> (max is used only as a numeric
#   bound here; never quoted to a seller). Candidates above max are flagged "above budget".
ask "Which one(s) should I pursue? (pick one or several and I'll chase them in parallel)"
step=awaiting_confirm ; write session
```

### awaiting_confirm → (user picks one or more)
```
chosen = the candidate ids the user picked
# above_budget gate: if any chosen candidate.price > max (read via budget_gate; the number stays
#   hidden) → this is the "above_budget" decision. hands-free user has it at escalate, so confirm
#   that specific pick with the user before pursuing ("that one's above your max, still chase it?").
want.chosen = chosen ; want.thread_ids = [ "<market>:<listing_id>" for each chosen ]
for each chosen: seed data/buyer_threads/<market>:<listing_id>.json
   { thread_id, want_id, seller_handle, listing_url, listed_price,
     cursor:{last_handled_msg_id:null,last_handled_ts:null}, status:"liaising", transcript:[] }
want.status = "liaising"
say "Great, I'll reach out on <markets of chosen> and negotiate within your range. I'll ping you the
     moment a deal lands."   # voice.md ack; hands-free — the next ping is the struck deal
# HANDOFF: the liaison engine (skills/buying/liaison-pipeline.md) opens each thread, makes the
#   opening offer via bin/buyer_negotiate.py, and /selly-run's buy side polls these threads for
#   seller replies. Discovery is done.
session.active=false (step=liaising)
```

## Invariants (mirror of listing.md)
- **Listing URLs are READ, never written.** Never compose or message a candidate link you did not
  read from a live page; every `url` must pass `bin/verify_listing_url.py` before it's stored. (The
  buyer-side "Harry guard".)
- **Max budget** only in `data/budgets/<want_id>.json` — never in the want, session, messages, or
  prompts. Read only by `bin/budget_gate.py` / `bin/buyer_negotiate.py`.
- **Read-only discovery:** the search recipes NEVER message a seller or click "Chat" — opening a
  thread is the liaison engine's job. Search only navigates + reads rows.
- **Account safety:** real logged-in Chrome only; human-paced; no tight pagination loops;
  logged-out/checkpoint → stop that market + escalate; pacing/jitter/caps stay on even unattended.
- **Resumable/idempotent:** state is the session file; a killed pass resumes from `step`. A user reply
  during an `active` session always applies to `session.want_id`'s current `step`.
- Each pass does **one** step and returns (except `searching`, which is messaged up-front).
- **Apply `skills/voice.md`** to every message (no em-dashes; ack before the slow `searching` step).
