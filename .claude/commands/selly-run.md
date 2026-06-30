---
description: SELLY Skills — the unified agent loop (channel + sell inboxes + buy threads)
---

# /selly-run — the one main loop

One semi-attended loop that runs the whole agent. It watches the **control channel** (per
`channel.adapter` — Telegram / iMessage / WhatsApp / console) for setup/listing/buying/decisions, and
each pass it sweeps **both sides**: the **sell side** (buyer inboxes on every enabled marketplace) and
the **buy side** (seller-reply threads for every want being pursued). Keep a session open (Claude Code
or Codex); wrap with `/loop` for periodic polling (e.g. `/loop /selly-run`). Resumable and idempotent
on every side.

Read first: `skills/channel/channel.md`, `skills/channel/notifications.md`, `skills/reply-pipeline.md`,
`skills/buying/liaison-pipeline.md`, `skills/browser-actions.md`, `skills/voice.md`.

**Scope.** Optional `--scope both|sell|buy` (default `both`). The aliases set it: `/sell-run` →
`sell`, `/buy-run` → `buy`. A side is skipped if its config is missing.

## Setup (once per pass)
Load `data/config.json` (`approvals`, pacing). Load whichever side configs exist: `data/seller_config.json`
(sell side) and/or `data/buyer_config.json` (buy side). Bind the channel verbs to the shared
`channel.adapter` (from either config — onboarding writes them consistently; any of `console`,
`telegram`, `imessage`, `whatsapp` — see `skills/channel/adapters.md`). Apply the back-compat read-shims
(`marketplaces` array→object in `skills/marketplaces.md`; `approvals`-absent in `skills/selly-config.md`).
If **neither** side config exists → run `skills/channel/intro.md` (which leads into onboarding) and return.

**Single-consumer guard (at session start):** run `python3 bin/daemon_conflict.py`. If it reports
`conflict:true` (the always-on daemon is loaded AND the channel is single-consumer like Telegram), the
daemon and this interactive session would steal each other's messages. WARN the seller with its
`reason` and offer to either stop the daemon (`launchd/install_daemon.sh uninstall`) or close this
session and let the daemon run. Do this once per session, not every pass. (console = no conflict.)

## Each pass
```
# 1. CONTROL CHANNEL — drain it (both command sets)
events = channel.watch()                       # Telegram: telegram.py poll (offset-cursored)
for e in events (in order):
   command (sell):  /list    -> skills/channel/listing.md
                    /detect  -> skills/channel/distribution.md (SCAN/manage/cross-list)
                    /delist  -> skills/channel/delist.md (seller-initiated take-down of a LIVE listing;
                                also matches free-text "delete/remove/take down my <item> listing" —
                                NOT a sale; resolve item by id, take down each platform, then
                                bin/delist_item.py writes the durable record. Never the session file.)
   command (buy):   /search  -> skills/buying/search.md (need -> search -> recommend -> price range -> confirm)
   command (both):  /inbox-detect -> skills/inbox-detect.md SWEEP (review every inbox, offer to take over
                                chats the user started solo: buy = purchase chats, sell = listing chats).
                                /buy-detect = same, scope:buy only. Also matches free-text "check my
                                inbox / chats / messages", "take over my <item> chats".
                    /onboard -> skills/channel/onboarding.md
                    /status  -> say a summary (live items + open buyer threads;
                                active wants + shortlists + open seller threads; escalations).
                                If `python3 bin/control.py status` shows paused, LEAD with:
                                "⏸ PAUSED since <since> (via <source>) — <N> correction(s) queued.
                                 Send /resume to continue." then the usual summary.
                                For the full deep sweep (every listing + marketplace + setup, with
                                proposed work), point to /selly-catchup.
                    /catchup -> skills/selly-catchup.md (also matches /selly-catchup and free-text
                                "catch me up / what have I missed / sweep everything"): START at HEALTH
                                with scope:"both". Deep, read-only sweep of every listing + inbox +
                                setup surface; reports ONE grouped digest and PROPOSES the work, acting
                                on nothing during the sweep. Turn-based/resumable in
                                data/catchup_session.json (one market or one question per pass); ack the
                                slow sweep first, do one step, let later passes continue it.
                    /pause   -> `python3 bin/control.py pause --source <adapter>`. Do NOT send your
                                own "Paused" line: the system sends the SINGLE confirmation
                                deterministically (the non-LLM drain bin/channel_control.py, gated by
                                control.claim_pause_ack — exactly once per pause episode), so an
                                LLM-authored ack here would just duplicate it. The daemon holds all
                                action passes, preempts any live worker / interrupts a running pass
                                within ~one poll cadence, and a /pause at the channel is recognized
                                deterministically (no seller pass needed); while paused, free-text you
                                send is captured as a CORRECTION (control.py correct) — see RESUME
                                below. The drain handles /pause,/resume + correction capture without an
                                LLM, so a paused agent costs ~$0.
                    /resume  -> `python3 bin/control.py resume --source <adapter>`. BEFORE resuming normal
                                work, run skills/channel/corrections.md: drain pending corrections,
                                apply each to the durable state the relevant pass reads, mark applied.
   action:   a button-tap. If its ref is `intent-<batch>` (the daemon's instant sell/buy question for
             a bare photo burst) OR a listing_session is active at step `awaiting_intent`, it is the
             SELL/BUY CHOICE -> route to skills/channel/listing.md `awaiting_intent` (choice = the
             button key). Otherwise resolve the matching pending notify via
             skills/channel/notifications.md (sell escalations/bids/sale, AND buy escalations/deals).
   text while a flow is mid-step -> feed it as that flow's awaited input
   # A listing_session at `awaiting_intent` is waiting for the sell/buy choice (the daemon asked it
   # instantly): a "sell"/"buy" reply (button or text) routes to listing.md awaiting_intent.
   # Mid-step = data/listing_session.json OR data/distribution_session.json OR data/buy_session.json
   # is active; route the reply to whichever is active (only one runs at a time). Never re-ask which
   # item/want it is — that's a persistence bug (see listing.md / search.md routing rules).
   PHOTO while a LISTING session is mid-step -> route to listing.md; NEVER a price/floor answer.
   # A photo can never be the awaited price/floor text.
   #  - step `awaiting_intent` with fields.photos still EMPTY: these are the item's FIRST photos (the
   #    daemon opened the session + asked sell/buy before consuming them). listing.md awaiting_intent
   #    downloads them into fields.photos and keeps waiting for the sell/buy choice. NOT a straggler.
   #  - any LATER step (researching / awaiting_listing_inputs): the daemon's settle window already
   #    coalesced the initial burst, so this is a STRAGGLER (an extra angle). If it's more angles of
   #    session.item_id, append to fields.photos (refine identification only if material) and ack
   #    briefly ("added 📷"). If it's clearly a DIFFERENT item, do NOT silently fold it in: ask
   #    "More angles of <current item>, or a new item to list?" and act on the answer next pass.

   text (free reply) that ANSWERS an OPEN pending escalation -> resolve that pending, NOT the gate.
   # Before the FRESH-MESSAGE INTENT GATE, check channel_state.pending[] (notifications.md:8 already
   # states a free-text reply resolves a pending). If the message reads as a reply to an OPEN pending
   # — by kind + the item/buyer it names + your last [out] turn — dispatch it to
   # skills/channel/notifications.md "Inbound" for that ref; do NOT run the intent gate, do NOT start
   # a listing/search. In particular: a message describing an OFFLINE deal (meetup / self-collect /
   # a pickup address / "leave it outside" / an offline payment instruction such as a PayNow or bank
   # number / "cash on collection") in reply to a pending `close` = the seller choosing "Deal other
   # ways" -> resolve that close as `manual` (notifications.md close → manual). NEVER treat it as a
   # new listing or a listing edit, and NEVER copy the address or payment number into the listing /
   # item / qa_bank / config — discard it (private; see onboarding.md trust rules). If no pending
   # close exists, handle per the no-pending case in notifications.md (arrange-it-yourself + promote
   # the rail); still start NOTHING.

   text/photo with NO active session (a FRESH message) -> run the INTENT GATE below, then
   start AT MOST ONE flow. This gate is authoritative: it decides sell vs buy ONCE, so the
   skills' own START rules never both fire on the same message. (listing.md START and
   search.md START are written to defer to this decision.)

   # ---- FRESH-MESSAGE INTENT GATE (control channel; runs before any skill START) ----
   # Available sides = which side configs exist AND are in --scope:
   #   sell available  = seller_config.json present AND scope in {both, sell}
   #   buy  available  = buyer_config.json  present AND scope in {both, buy}
   # Classify intent from the caption/text (LLM judgment on the words, not keywords):
   #   BUY  = user wants to ACQUIRE it. Signals: "get this", "want", "looking for", "find me",
   #          "buy", "under $X", "max", "budget", "bid", or a marketplace URL to acquire.
   #   SELL = user wants to OFFLOAD it. Signals: "selling", "for sale", "list this",
   #          "how much can I get", or a bare item description / no verb.
   #          NOT SELL: a message that only states meetup / pickup / offline-payment LOGISTICS for an
   #          item already in play ("can be picked up at <address>", "leave it outside", "paynow to
   #          <number>", "cash on collection") is a deal-handling message, not "list this". Route it
   #          per the pending-answer branch above (resolve a pending `close` to `manual`); if no
   #          pending exists, reply per notifications.md's no-pending case. Start NO listing, and
   #          never copy the address/payment into the listing/item/qa_bank/config.
   #   NEUTRAL = photo with no caption, or a caption that is only a description.
   #   FOLLOW-UP = a no-signal message that only makes sense as a reply to your OWN last [out]
   #          turn in the RECENT CONTROL-CHANNEL CONVERSATION block (injected into the channel
   #          pass; data/channel_transcript.jsonl). Signals: "do all", "do all tasks", "take over
   #          all", "both", "yes"/"go ahead", "auto", "the first one"/"#2"/"that one" — a bare
   #          confirmation or selection with no item of its own.
   # Precedence (first match wins):
   #   0. FOLLOW-UP and a recent [out] turn exists -> RESOLVE against that turn; do NOT run the
   #        sell/buy word-classification below.
   #        - last [out] turn ENUMERATED tasks and the user says "do all"/"both"/"take over all"/
   #          "yes" -> ACT ON ALL listed tasks by RESUMING each existing flow (a want already in
   #          data/wants/ as searching|liaising; the cross-list batch in
   #          data/distribution_session.json) — reply "On it, running both"; do NOT start a NEW flow
   #          and do NOT re-enumerate. NOTE: "do all" spanning a BUY and a SELL task RESUMES two
   #          existing flows; it does NOT "start both" new flows, so the one-flow rule below is not
   #          violated. (The daemon's buy/maint passes, already gated on those files, drain the rest.)
   #        - last [out] turn ASKED A QUESTION and the user gives a short answer -> apply it to that
   #          question (its session), not a fresh classification.
   #        - follow-up referencing an item you offered to buy/sell -> route to that side's RESUME
   #          path, not a new START.
   #      FOLLOW-UP but NO recent [out] turn (or a stale/unresolvable offer) -> fall through to
   #      AMBIGUOUS (#5): ask ONE clarifying question, start nothing.
   #   1. text only, no photo, BUY intent   -> BUY  (search.md START)
   #   2. photo (+/- text), BUY intent      -> BUY  (search.md START; photo = visual context, not a listing)
   #   3. photo, SELL intent (clear caption) -> SELL (listing.md START; goes straight to research)
   #   4. text only, no photo, SELL intent  -> SELL (listing.md START; it will ask for photos)
   #   5. photo + NEUTRAL (no caption / description-only) -> SELL side entry (listing.md START). A photo
   #      almost always means sell-or-buy but WHICH is unclear, so listing.md START ASKS sell-or-buy
   #      FIRST (its awaiting_intent step) with the photos already downloaded + stashed in the session,
   #      and redirects to BUY (search.md) if the seller picks "buy". The photo routes to the listing
   #      skill because that is where photo intake + the session live, so the photos survive the ask;
   #      this honors "ask sell/buy only when unclear" without losing the images. (If the buy side is
   #      unavailable per the SCOPE guard, listing.md skips the ask and lists.)
   #   6. AMBIGUOUS text (both buy+sell signals, no photo) -> do NOT guess. ask() ONE question, start
   #      NOTHING until the answer; route on the next pass:
   #           ask("Want me to sell this for you, or buy one like it?", options="sell=Sell it,buy=Buy one")
   # SCOPE / CONFIG guard (every branch): only an AVAILABLE side may start.
   #   - chosen side available     -> start that one flow and return.
   #   - chosen side NOT available -> do NOT silently start the other side. say() it isn't set up
   #     ("This looks like something to buy, but the buyer side isn't set up yet. Run /onboard to add it.";
   #      mirror for sell), then return.
   # Exactly ONE flow starts (or one ask is sent). Never start both.
   # ------------------------------------------------------------------------------------

# 1b. RESUME the active listing wizard if it has READY work — even with NO new seller event. The
#     background research worker (Phase B) writes data/research_results/<batch_id>.json off to the
#     side, and the daemon fires this pass the moment it (or a timeout .failed marker) lands. So:
#     if data/listing_session.json is active at step `researching`, run skills/channel/listing.md
#     RESEARCH now — it PRESENTS a ready result (or does inline research on failure), or returns
#     quietly if the worker is still running. This is what lets findings surface proactively while
#     the seller is idle, instead of only on their next message.
if listing_session active AND step == "researching":
   run skills/channel/listing.md RESEARCH (present-or-wait); then continue.

# 2. SELL SIDE — one pass per ENABLED marketplace inbox   (skip if --scope buy or no seller_config)
for id, sel in seller_config.marketplaces.items() if sel.enabled:    # e.g. fb, carousell, ebay…
   navigate(<id> inbox) ; read_inbox()
   for thread (id namespaced <market>:<id>) past its cursor, status not in {escalated, lost, handover, held}:
      # `held` = the user said "stop replying to this buyer" via a pause correction; reversible (≠ escalated).
      read_thread -> for each new message in order -> skills/reply-pipeline.md
      # assist-mode offers + unknowns -> ESCALATE -> channel.notify (notifications.md)
   # Threads NOT in data/threads/ are untracked — left to the cadence-gated inbox SWEEP (§2b), which
   # classifies them and offers takeover. The hot loop only processes threads already managed here.
   # FREE RELIST OFFER — the in-app assistant (carousell_assistant) offered to relist/renew/bump our
   # OWN listing (a free visibility boost). inbox_scan flags it deterministically (platform_offers),
   # distinct from buyer routing. Take it ONLY if free; never spend.
   if python3 bin/inbox_scan.py platform-offers shows <id>:true:     # cheap, read-only, ~0 tokens
      run skills/channel/relist-offer.md for <id>                    # free->relist+stamp+FYI; paid/unknown->skip
      # (free_relist gate; per-item relist_cooldown_days; pacing; NEVER clicks a paid/coin control)

# 2b. AUTONOMOUS DETECT — unmanaged LISTINGS (my-listings page) AND untracked INBOX CHATS (the chat
#     list), both off the hot loop. Cadence-gated: at most ONE due market per pass, cursor
#     data/scan_state.json, cadence config.scan_interval_hours. Never interrupt an in-flight wizard.
if no data/listing_session.json AND no data/distribution_session.json AND no data/buy_session.json
   AND no data/inbox_detect_session.json AND no data/catchup_session.json is active:
   d = python3 bin/inbox_detect.py due        # most-overdue market across the UNION of enabled sell+buy markets
   if d.due_market:
      m = d.due_market
      # (i) my-listings SCAN — sell only (needs seller_config; skip if --scope buy)
      if scope in {both, sell} AND seller_config.marketplaces[m].enabled:
         run skills/channel/distribution.md SCAN for m only          # unmanaged listings -> manage + cross-list
                                                                     # (approvals.steps.distribution gate)
      # (ii) inbox SWEEP — review m's chat list for threads the user started solo and offer takeover.
      #      buyer_peek gates the open (quiet inbox costs ~0). Seller-initiated hits route to the
      #      distribution IMPORT queue; buyer-initiated hits to a budget+seed+liaison handoff.
      run skills/inbox-detect.md SWEEP for m only, scope=<both|buy|sell per --scope>   # takeover gate (hard-floor)
      python3 bin/scan_state.py mark --market m                      # one stamp covers BOTH detectors
   # SCAN queues unmanaged listings -> distribution IMPORT on later passes; SWEEP queues untracked chats
   # in data/inbox_detect_session.json -> TAKEOVER on later passes (takeover gate, balanced default `confirm`).

# 3. BUY SIDE — one pass per ACTIVE want's threads        (skip if --scope sell or no buyer_config)
for want in data/wants/*.json with status in {liaising, agreed}:
   for thread_id in want.thread_ids:                                 # namespaced <market>:<id>
      open data/buyer_threads/<thread_id>.json ; skip if status in {closed, escalated, held}
      # `held` = paused-correction "stop pursuing this seller"; reversible (clear it to resume the thread).
      navigate(thread) ; read_thread
      if no outbound message yet -> skills/buying/liaison-pipeline.md INITIATE (open + opening offer)
      else for each new seller message past the cursor in order -> skills/buying/liaison-pipeline.md
      # struck deal -> handover.md -> channel.notify(buy_deal); scam/unanswerable -> ESCALATE to user

# 4. PACING — one shared budget across ALL sides
enforce max_actions_per_hour over (channel notifies + buyer sends + seller sends + clicks/publishes);
honor quiet_hours; jitter every send (reply_delay_sec). Report a one-line summary:
channel cmds handled, per-market buyers handled/escalated, per-want sellers handled/deals/walked, holds.
```

## Resilience
- **Idempotent every side:** channel = the bound adapter's cursor in `channel_state.json`; sell =
  per-thread `last_handled_msg_id` in `data/threads/`; buy = per-thread `last_handled_msg_id` in
  `data/buyer_threads/`. Kill/restart mid-pass → no double work.
- **Namespaced threads** `<marketplace>:<id>` in separate dirs (`data/threads/` vs `data/buyer_threads/`)
  — sell and buy inboxes never collide.
- **Per-market failure:** a logged-out/checkpoint on one marketplace → stop that market's pass,
  `notify` the user to re-auth, and keep the other markets + the other side + the channel running.
- **Secrets:** the floor (`data/floors/`) and the max budget (`data/budgets/`) never enter context —
  only `bin/floor_gate.py` / `bin/negotiate.py` and `bin/budget_gate.py` / `bin/buyer_negotiate.py`
  read them. Channel tokens live in the harness env (read only by the adapter shims).

## Aliases
- `/sell-run` — this loop, `--scope sell` (channel + buyer inboxes only; the seller half).
- `/buy-run` — this loop, `--scope buy` (channel + seller-reply threads only; the buyer half).
- `/sell-watch` — sell buyer-inboxes only, **no channel** (console/testing).
- `/sell-list` — jump straight into the listing flow. `/buy-search` — jump straight into discovery.
- `/sell-detect` — jump into distribution. `/sell-resolve` — console fallback for sell escalations.
- `/inbox-detect` — sweep every inbox now, offer to take over chats the user started solo (both sides).
  `/buy-detect` — the same, buy-scoped (purchase chats only).
- `/selly-catchup` — deep read-only sweep of every listing, marketplace, and setup surface; reports
  one digest of what's not attended to and proposes the work (no acting during the sweep).

Honor `--dry-run`: browser actions and channel sends are **logged**, not executed.
