# INBOX-SWEEP flow — review every marketplace inbox, offer to take over untracked chats

Bazaar normally acts only on conversations it already tracks. This skill reviews the **inbox itself**
on each enabled marketplace, finds threads the user started on their **own** (outside Bazaar), and
offers to take them over — both **purchase chats** (the user messaged a seller about a listing) and
**listing chats** (someone messaged a listing the user never imported). It is the buy-and-sell
counterpart to `distribution.md`'s my-listings SCAN, run against the chat list rather than the
"your listings" page.

> Talks to the user via `channel.md` verbs; drives marketplaces via `browser-actions.md` and the
> per-site `skills/listing-flows/<market>.md` "Read buyer inbox recipe"; uses `bin/inbox_detect.py`
> for the deterministic classify / untracked-diff / declined-set, and the cheap `bin/buyer_peek.py`
> unread probe to avoid opening threads when nothing changed. Requires onboarding done (at least one
> of `seller_config.json` / `buyer_config.json`).
> **Approval:** the offer to step into a thread reads `config.approvals.steps.takeover`
> (`skills/bazaar-config.md`) — a **hard floor**: `confirm` (default) asks first, `escalate` parks;
> it is never `auto`. The private budget ask on a buy takeover is independently `confirm`/`escalate`.
> Once the user accepts, per-message handling reverts to the normal side gates (`buy_offer`/`buy_accept`
> for buys; `offers`/`buyer_replies` for sells).

This is turn-based and resumable, exactly like `listing.md` / `distribution.md`: one step per pass,
persisted in `data/inbox_detect_session.json`, so a long sweep plus a couple of questions never block
a pass. Apply `skills/voice.md` to every message (no em-dashes; ack before any slow sweep).

## `data/inbox_detect_session.json`
```json
{ "active": true,
  "phase": "sweep | takeover",
  "scope": "both | buy | sell",
  "market": "<the market being swept, when scoped to one>",
  "queue": [ { "market","thread_id","tid","side": "buy|sell",
               "seller_handle","item_hint","listing_url","listed_price","last_snippet",
               "group_key","transcript": [...], "decision": null } ],
  "current_group": "<group_key during takeover>",
  "current_want_id": "<kebab, set when a buy want is created/linked>",
  "step": "awaiting_takeover | awaiting_price_range | null",
  "updated_at": "<iso>" }
```
`phase`/`step` drive routing exactly like `distribution.md`. A user reply mid-flow is applied to the
current `step` for `current_group` — **never re-ask which chats those were** (that is a persistence
bug, same rule as listing/search). `decision` per entry ∈ `null | takeover | skip`. Only **one** of
`buy_session.json` / `inbox_detect_session.json` / `distribution_session.json` is active at a time;
guard before starting (never interrupt an in-flight wizard).

## Entry points
- **`/inbox-detect`** (`.claude/commands/inbox-detect.md`) → start **SWEEP** across **all** enabled
  inboxes (union of seller + buyer markets), `scope:"both"`.
- **`/buy-detect`** (`.claude/commands/buy-detect.md`) → start **SWEEP** with `scope:"buy"` (only
  buyer-initiated threads are offered; seller-initiated ones are left to `/sell-detect`).
- **autonomous cadence** (from `bazaar-run.md` §2b) → start **SWEEP** for the **one** market that
  `python3 bin/inbox_detect.py due` reports overdue (cursor: `data/scan_state.json`, cadence
  `config.scan_interval_hours`) — the same slot as the my-listings SCAN, so a due market is swept
  for both unmanaged listings *and* untracked chats, then stamped once.

---

## SWEEP — find inbox threads Bazaar isn't managing
Scope: `/inbox-detect` sweeps **all** enabled markets; `/buy-detect` and the autonomous cadence pass a
narrower set. `markets = [market]` when the cadence scopes one, else every enabled id (union of
`seller_config` + `buyer_config`). Gate the open with the cheap unread probe so a quiet inbox costs
nothing.
```
peek = python3 bin/buyer_peek.py            # per-market unread counts (~0 tokens, fail-open)
for each id in markets (enabled on either side):
    if peek.markets[id] exists and not peek.markets[id].new and not /inbox-detect (forced): skip id
    follow skills/listing-flows/<id>.md "Read buyer inbox recipe" → read_inbox() rows
        rows = [{thread_id, buyer_handle/seller_handle, item_hint, unread, last_snippet}]
    on logged-out/checkpoint → notify re-auth for THIS market only (notifications.md), keep others
    untracked = python3 bin/inbox_detect.py diff --market <id> --rows <rows.json>   # drops tracked + declined
    for row in untracked.untracked:
        if not row.unread and not /inbox-detect (forced): continue      # only open fresh ones
        read_thread(row.thread_id) → transcript [{msg_id, dir, text, ts}]
        url = the listing this thread is about (read from the thread/listing; verify_listing_url.py)
        owned = url normalizes to a value in any data/items/*.json listing_urls (the user's own listing)
        d = python3 bin/inbox_detect.py classify --thread <transcript.json>   # buyer_initiated | seller_initiated | empty
        side = "sell" if (owned or d=="seller_initiated") else "buy" if d=="buyer_initiated" else None
        # AMBIGUOUS (empty, or owned-vs-direction conflict we can't resolve) → do NOT guess:
        #   mark seen (inbox_detect.py decline --thread <tid> --side unknown) and continue. No offer.
        if scope=="buy" and side=="sell": continue         # /buy-detect ignores the sell side
        if scope=="sell" and side=="buy": continue          # sell-scoped sweep (e.g. /sell-run) ignores buys
        if side is None: inbox_detect.py decline --thread row.tid --side unknown ; continue
        append to session.queue { ...row, tid:row.tid, side, listing_url:url, transcript, decision:null }
group buyer-side entries by inferred item (group_key = LLM normalize of item_hint + first outbound
    text, e.g. "iPhone 13 128gb" → "iphone-13"); sell-side entries group per anchor listing url.
if queue empty:
    say nothing if invoked from the cadence; on /inbox-detect say "✅ Swept <markets> — every chat is
    already managed by me." ; session.active=false ; return
say "🔎 Found <n> chat(s) you started that I'm not managing yet." ; phase=takeover ; write ; return
```
The diff anchor is the **namespaced thread id** `<market>:<thread_id>` (the same key as
`data/buyer_threads/<tid>.json` / `data/threads/<tid>.json`). Classification keys on the FIRST in/out
message; the owned-listing check is the tie-breaker for buy+sell accounts (eBay). One market per pass
is fine — sweeping is not on the hot loop.

## TAKEOVER — offer one group per pass, then adopt it
Pop the next `group` with `decision == null`. The offer is gated by `takeover` (hard floor — confirm).
```
buy group:   ask (takeover gate) "📥 You started <n> chat(s) about “<item>” on <market(s)>
                  (<seller handles>). Take them over and negotiate within a budget you set?"
                  actions=[takeover=Take over, skip=Leave them]              → step=awaiting_takeover
sell group:  ask (takeover gate) "📥 Someone messaged your “<item>” listing on <market> and I'm not
                  managing it yet. Bring it under management (I'll watch its chats + negotiate)?"
                  actions=[takeover=Manage it, skip=Leave it]                → step=awaiting_takeover
```

### awaiting_takeover → on skip
Mark every entry in the group `decision="skip"` and `python3 bin/inbox_detect.py decline --thread <tid>
--side <side>` for each (so it is never re-offered). Next group or → Done.

### awaiting_takeover → on takeover (SELL group)
Hand the anchor listing to the existing importer — do NOT duplicate its logic:
```
enqueue the listing into data/distribution_session.json.queue { market, title:item_hint, url, price,
    decision:null }  (same row shape distribution SCAN produces; normalized-url dedup means a listing
    the my-listings SCAN already queued is not added twice).
for each tid in the group: inbox_detect.py decline --thread <tid> --side sell  # mark seen as managed
say "On it, I'll bring “<item>” under management." ; → distribution.md IMPORT runs on later passes
   (it asks the floor + size, then sell-run/§2 handles the thread). Next group or → Done.
```

### awaiting_takeover → on takeover (BUY group)
```
want_id = LINK or CREATE:
   if an open want (data/wants/*.json, status in {searching, liaising}) clearly covers this item
      → LINK: want.thread_ids += this group's tids (dedup) ; keep its existing budget.
   else → CREATE via search.md's want-writing shape (buyer-safe):
      { want_id:kebab(query), query:<from item_hint + first outbound text>, category, category_tag
        (fixed taxonomy in marketplaces.md; default "other"), condition_pref, region, currency,
        target_price:null, candidates:[ the verified rows we read ], shortlist:[], chosen:[],
        thread_ids:[<group tids>], status:"shortlisted", source:"imported", created_at:now }
current_want_id = want_id
if the linked want already has a budget (data/budgets/<want_id>.json) → skip the ask, go straight to
   the seed step below using the existing budget.
else ask "🔒 What's your max for “<item>”? And a target you'd love? PRIVATE, never shown to sellers."
   → step=awaiting_price_range ; return
```

### awaiting_price_range → (target + max)  [reuses search.md's secret-boundary write verbatim]
```
want.target_price = <target>
write data/budgets/<want_id>.json { want_id, target_price:<target>, max_budget:<max>,
   opening_ratio:0.8, auto_counter_step:5, auto_counter_rounds:3, give_up_polls:6, currency }
   # SECRET BOUNDARY — the max goes ONLY here, never the want/session/messages/prompts.
for each entry (tid) in the current group:
   # (a) seed the negotiation ledger from what the user ALREADY offered, WITHOUT emitting an offer,
   #     so the engine never lowers or re-opens it:
   our_last  = the user's last numeric offer in this thread's transcript (a dir:"out" price), else omit
   sell_ask  = the seller's last numeric ask in the transcript, else omit
   python3 bin/buyer_negotiate.py seed --want <want_id> --thread <tid> --seller "<handle>"
       [--listed <listed_price>] [--our-last <our_last>] [--seller-ask <sell_ask>] --currency <currency>
   # (b) seed the thread file so liaison RESUMES mid-conversation (the key trick — cursor at the TAIL):
   write data/buyer_threads/<tid>.json {
       thread_id:tid, want_id, seller_handle:"<handle>", listing_url:"<verified url>",
       listed_price:<listed_price>, status:"liaising", source:"imported",
       transcript:<the FULL transcript captured in SWEEP>,
       cursor:{ last_handled_msg_id:<id of the LAST msg in transcript>, last_handled_ts:<its ts> } }
   inbox_detect.py decline --thread <tid> --side buy        # mark seen as managed
want.chosen += group tids ; want.status = "liaising" ; write want
say "On it — I'll pick up your <market> chats about “<item>” and negotiate within range. I'll ping you
     the moment a deal lands."
next group with decision==null, or → Done
```
**Why no opening offer fires:** `liaison-pipeline.md` §2 INITIATE runs only when a thread has *no
outbound message yet*. Here the seeded transcript already has outbound messages **and** the cursor sits
at the last message, so the buy side of `/bazaar-run` (§3) finds nothing past the cursor — it neither
re-INITIATEs nor re-replies; it simply waits for the seller's next message, then liaison replies once
(naturally as the buyer, no identity line, per `skills/voice.md` Rule 3). No duplicate offer, no
re-greeting, no contradicting the user's own last line.

## Done
When no group has `decision==null`: `session.active=false`. On `/inbox-detect` / `/buy-detect`, end with
a one-line summary (taken over / left counts, buy vs sell).

## Invariants
- **Max budget** only in `data/budgets/<want_id>.json` — never in the want, thread, session, queue,
  replies, prompts, or in `bin/inbox_detect.py` / `bin/buyer_negotiate.py seed` output.
- **Floor** (sell takeovers) only in `data/floors/<item_id>.json` (written by `distribution.md` IMPORT).
- **Listing URLs are READ, never composed** — every `listing_url` passes `bin/verify_listing_url.py`
  before it is stored or messaged.
- **Never nag:** a declined thread is recorded in `data/takeover_seen.json` and never re-offered; the
  cadence stamp + this set together guarantee it.
- **Single writer per thread:** this skill only *seeds* state and sends the *offer*; it never replies
  to a marketplace thread itself. After adoption, §2 (sell) / §3 (buy) own the thread.
- **Resumable/idempotent:** state is the session file + `takeover_seen.json` + the seeded cursors; a
  killed pass re-sweeps, the diff excludes anything already tracked/declined, no double-import.
- **Disclosure:** a taken-over buy thread follows `skills/voice.md` Rule 3 like any other — no identity
  line is prepended; the agent replies naturally and never claims to be human if asked outright. Agent
  involvement is disclosed at handover (`skills/buying/handover.md`).
- Each pass does **one** step and returns (sweep one market; ask one question; adopt one group).

## `--dry-run`
SWEEP (read_inbox / read_thread / classify / diff) and the engine calls run normally (read-only).
The budget write, the `buyer_negotiate.py seed`, the thread/want seeds, and any distribution enqueue
are **logged, not executed** (per `browser-actions.md` dry-run). No channel message is sent.
