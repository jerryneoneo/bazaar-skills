# SELLY-CATCHUP flow — sweep everything, report what's waiting, propose the work

`/selly-catchup` answers one question: **is there anything I haven't attended to?** It does a deep,
mostly read-only sweep across three surfaces, then sends one grouped digest and offers to act on each
group by handing off to the skill that already owns it (each keeps its own approval gate). Nothing is
acted on during the sweep itself.

The three surfaces:
1. **Interface + health** — is the channel bound, the browser reachable, each enabled marketplace
   logged in, the daemon loaded, the agent paused? A logged-out market becomes a health task **and**
   is skipped in the deep sweep below.
2. **Local state** — every "awaiting you" signal already on disk (open escalations both sides, unread
   managed threads both sides, draft / undistributed listings, open checkouts, open wants, overdue
   cadence), via `python3 bin/triage.py --json`.
3. **Live marketplaces (deep)** — for each enabled, logged-in market, reconcile the platform against
   managed state: unmanaged / undistributed listings and on-platform sold/removed **drift** (the
   `distribution.md` SCAN read), plus untracked chats the user started solo (the `inbox-detect.md`
   SWEEP read). Both run **collect-only**: they detect and fold findings into the digest, they do not
   enqueue, import, or offer here.

> Talks to the user via `channel.md` verbs; drives marketplaces via `browser-actions.md` and the
> per-site `skills/listing-flows/<market>.md` recipes; reuses `bin/triage.py` (local digest),
> `bin/healthcheck.py --json` (health), `bin/control.py status` (pause), `bin/buyer_peek.py` (cheap
> unread gate), `bin/inbox_detect.py` (untracked-chat diff/classify), and `bin/scan_state.py` /
> `bin/eval_state.py` (cadence). Requires onboarding done (at least one of `seller_config.json` /
> `buyer_config.json`); if neither exists, run `skills/channel/onboarding.md` first.
> **Approval:** the sweep itself acts on nothing, so it reads no business gate. Each accepted proposal
> hands off to its owning skill, which applies its own gate (e.g. `takeover` for `/inbox-detect`,
> `distribution` for `/sell-detect`) per `skills/selly-config.md`.

This is turn-based and resumable, exactly like `inbox-detect.md` / `distribution.md`: one market (or
one question) per pass, persisted in `data/catchup_session.json`, so a long deep sweep never blocks a
pass. Apply `skills/voice.md` to every message (no em-dashes; **ack before the slow sweep**, since
navigating every marketplace takes more than a couple of seconds).

## `data/catchup_session.json`
```json
{ "active": true,
  "phase": "health | sweep | report | propose | act",
  "scope": "both | buy | sell",
  "markets_pending": ["<enabled, logged-in market ids still to sweep>"],
  "markets_done": ["<swept this run>"],
  "digest": { "health": {...}, "local": {...}, "deep": {...} },
  "proposals": [ { "key","group","label","command","items":[...],"decision": null } ],
  "current_proposal": "<key during act>",
  "step": "awaiting_action | null",
  "updated_at": "<iso>" }
```
`phase`/`step` drive routing exactly like `distribution.md`. A user reply mid-flow is applied to the
current `step` for `current_proposal` — **never re-ask** what the sweep already found (that is a
persistence bug, same rule as listing/search). Only **one** of `catchup_session.json` /
`listing_session.json` / `distribution_session.json` / `buy_session.json` / `inbox_detect_session.json`
is active at a time; guard before starting (never interrupt an in-flight wizard).

## Entry points
- **`/selly-catchup`** (`.claude/commands/selly-catchup.md`) → start at **HEALTH**, `scope:"both"`.
  Forces an immediate full sweep of every enabled, logged-in marketplace now.
- **`/catchup` on the control channel** (Telegram et al.) → the same start (HEALTH, `scope:"both"`).
  The channel pass routes it (`selly-run.md` §1) and, because the sweep is turn-based, drives one
  step per pass with the seller's mid-flow replies routed back via `selly-run.md` §0. Registered in
  the Telegram "/" menu (`bin/telegram.py` `BOT_COMMANDS`).
- It has **no autonomous cadence** of its own (decision: on-demand only). The daemon already sweeps one
  due market per pass via `selly-run.md` §2b; `/selly-catchup` is the manual "check all of it now".
  Wrap with `/loop` (e.g. `/loop /selly-catchup`) for a periodic digest if you want one.

---

## HEALTH — interface + per-market readiness (cheap, runs first)
```
ack a short contextual line first ("Sweeping your listings, inboxes, and setup now, one moment.")
h = python3 bin/healthcheck.py --json        # CDP, onboarded, per-market login, daemon (read-only)
c = python3 bin/control.py status            # paused? queued corrections?
notify_ok = bin/notify_watch.py / bin/trigger_resolver.py viability (notification path live?)
digest.health = {
   paused: c.paused, corrections: c.pending,
   logged_out: [enabled market ids whose auth != confirmed OR healthcheck flags re-auth],
   daemon: h daemon checks, cdp: h chrome-cdp, channel_bound: from seller/buyer_config.channel,
   notify_path: notify_ok }
markets_pending = enabled markets (union of scope-relevant sides) MINUS digest.health.logged_out
phase = sweep ; markets_done = [] ; write ; return         # one step per pass
```
A logged-out market is reported as a health task to re-auth (`/selly` → marketplaces) and is **not**
swept live this run (a deep read on a logged-out tab would just bounce to a login wall).

## SWEEP — local state, then one live market per pass
```
# (a) LOCAL (once, cheap): the full on-disk digest
digest.local = python3 bin/triage.py --json
   # escalations / buyers_waiting / sellers_waiting / wants_open / listings / checkouts / cadence

# (b) DEEP, one market per pass (resumable): pop the next id from markets_pending
m = markets_pending.pop(0)                                  # if none left -> phase=report
reserve a read slot: python3 bin/pacing_gate.py reserve --marketplace m --kind read --mode interactive
peek = python3 bin/buyer_peek.py                            # cheap unread gate (~0 tokens)

# listings: reconcile the platform's "your listings" page against data/items (COLLECT ONLY)
follow skills/channel/distribution.md SCAN *read* steps for m (use bin/ui_cache.py selectors):
   rows = read my-listings ; normalize each url (bin/verify_listing_url.py)
   unmanaged   = platform rows whose normalized url is in NO data/items/*.json listing_urls
   undistributed = data/items live for m's category but missing m in listing_urls (also in triage.local)
   drift       = data/items status==live whose url is absent / marked sold on the platform
   append to digest.deep[m].listings = { unmanaged, drift }   # do NOT enqueue distribution here

# inbox: find chats the user started solo (COLLECT ONLY)
if peek.markets[m].new OR forced:
   follow skills/inbox-detect.md SWEEP *read* steps for m:
      rows = read inbox ; untracked = bin/inbox_detect.py diff --market m --rows <rows.json>
      classify each (bin/inbox_detect.py classify) into buy / sell / unknown
      append to digest.deep[m].untracked_chats = [ {tid, side, item_hint} ]   # do NOT offer takeover
on logged-out / checkpoint for m -> move m to digest.health.logged_out, notify re-auth for THIS
   market only (notifications.md), keep the others going (selly-run per-market resilience)

markets_done += [m] ; write ; return        # next pass sweeps the next market; when empty -> report
```
The deep layer **reuses** the existing read recipes and never duplicates their act steps. Detection is
idempotent: the same dedup anchors apply (`<market>:<thread_id>` for chats, normalized url for
listings), so re-running mid-sweep re-detects without side effects.

## REPORT — one grouped digest, ordered by urgency
Merge `digest.local` + `digest.deep` + `digest.health` and `say()` a single message. Skip empty
groups. If everything is empty: `say("✅ All caught up across <markets>. Nothing waiting.")`,
`session.active=false`, return.
```
❗ Needs your decision   (escalations: sell offers/bids/close/questions; buy escalations)
💬 Buyers waiting        (unread inbound on managed sell threads)
🛒 Sellers waiting       (unread inbound on managed buy threads; wants agreed)
🏷 Listings              (drafts; undistributed; unmanaged on a platform; sold/removed drift)
📥 Untracked chats       (chats you started solo, buy + sell)
🛟 Open checkouts        (issued, payment not yet completed)
⚙️ Setup / maintenance   (logged-out market, daemon not loaded, channel unbound, PAUSED,
                          notification path down, listing re-scan or self-eval overdue)
```
If `digest.health.paused`, **lead** with the pause line (mirror `selly-run.md` /status): "⏸ PAUSED
since <since> (via <source>), <N> correction(s) queued. Send /resume to continue." Build `proposals`
(one per non-empty group, in the same order) and `phase=propose`; write; return.

## PROPOSE + ACT — offer the work, hand off on accept
Each group maps to the skill that already owns the fix; the sweep itself starts nothing.
```
escalations (sell)   -> /sell-resolve            escalations (buy) -> the buy escalation resolver
buyers_waiting       -> /sell-run                 sellers_waiting / wants -> /buy-run
listings (any)       -> /sell-detect              untracked_chats   -> /inbox-detect (/buy-detect = buy only)
open checkouts       -> follow up on the buyer (notifications.md close)
logged-out market    -> re-auth via /selly       paused            -> /resume
```
Present the numbered list once and ask one question:
`ask("Want me to handle all of these, pick one, or leave it for now?",
     options="all=Handle all, pick=Pick one, leave=Leave it")` → `step=awaiting_action`.
- **all** → for each proposal, RESUME its owning flow using the FOLLOW-UP / "do all" semantics in
  `selly-run.md` §0 (a want already in `data/wants/`; the distribution batch; the inbox sweep): say
  "On it" once, run them on the following passes, do **not** re-enumerate and do **not** start a new
  flow. Mark each `decision="act"`.
- **pick** → ask which number, then hand off only that one (its gate still applies). Mark it `act`,
  the rest `defer`.
- **leave** → mark all `decision="defer"`; nothing is recorded as declined (this is a status check,
  not a takeover offer, so there is no `takeover_seen` write). The next `/selly-catchup` will surface
  anything still open.
When no proposal has `decision==null`: `session.active=false`; end with a one-line summary
("Handled N, left M, all marketplaces swept").

## Invariants
- **Read-only sweep.** HEALTH and SWEEP open only non-secret state and read marketplace pages; the
  only writes are the session file, the report `say()`, and the handoff that RESUMES an existing flow.
  No floor / budget / address / token is ever read (the floor lives in `data/floors/`, the budget in
  `data/budgets/`; `bin/triage.py` never opens them).
- **Single writer per thread/listing.** The sweep detects; it never replies, imports, or enqueues.
  After a handoff, the owning skill (`/sell-detect` IMPORT, `/inbox-detect` TAKEOVER, the §2/§3 hot
  loop) is the single writer, with its own gate.
- **No double action.** "Handle all" RESUMES existing flows; it never starts a duplicate listing,
  offer, or import. Dedup anchors (`<market>:<thread_id>`, normalized url) hold across the sweep.
- **Never interrupt a wizard.** Guard on the other session files before starting; one step per pass.
- **Resumable / idempotent.** State is `data/catchup_session.json` (`markets_pending` / `markets_done`
  + `proposals`). A killed pass resumes the remaining markets and re-renders the same proposals; no
  market is swept twice and no send is duplicated.
- **Disclosure unchanged.** The sweep sends nothing to buyers or sellers; any taken-over thread
  follows `skills/voice.md` Rule 3 once its owning flow resumes.

## `--dry-run`
HEALTH and SWEEP (healthcheck, triage, peek, the SCAN/SWEEP reads, classify/diff) run normally
(read-only). The report is **logged, not sent**, and PROPOSE/ACT start nothing: the chosen handoffs
are logged, not executed (per `browser-actions.md` dry-run). No channel message is sent.
