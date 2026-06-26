# Liaison pipeline — per-seller-message decide → compose → send (buyer side)

The buyer-side mirror of `skills/reply-pipeline.md`. Used by the buy side of `/bazaar-run` (and the
`/buy-run` alias) for each pursued thread, and by `skills/channel/notifications.md` after the user
answers an escalation. Unlike the seller pipeline (buyers initiate there), **the buyer initiates
here** — so a thread is first OPENED with an offer, then driven by the seller's replies. Process
**one seller message at a time, in order.**

> **Load config first.** Read `data/config.json` for `approvals` (`buy_offer`, `buy_accept`,
> `above_budget` — see `skills/bazaar-config.md`), `reply_delay_sec`, `max_actions_per_hour`,
> `quiet_hours`, `handover_disclosure` (posted at handover, see `skills/buying/handover.md`); and
> `data/buyer_config.json` for `payment_methods` +
> `delivery_area` (needed for the handover branch). `buy_offer`/`buy_accept`: `auto` acts, `confirm`
> gates on `confirm()`, `escalate` surfaces to the user via `notify()`.

> **Parallelize independent work (speed within a pass).** Issue independent NON-browser tool calls —
> file reads, web lookups (WebSearch/WebFetch), deterministic scripts — as **concurrent calls in a
> single turn**; never serialize work with no data dependency. **Browser actions stay strictly
> serial** (one warm Chrome, one active tab).

## 1. Resolve context
Load — **in parallel, one turn** (independent reads) — the thread file
`data/buyer_threads/<thread_id>.json` (transcript, cursor, status, seller_handle, listing_url,
listed_price), the buyer-safe want `data/wants/<want_id>.json`, and `buyer_config` (payment methods,
delivery area). **Never load `data/budgets/<want_id>.json`** — the max budget is reached ONLY by
calling `bin/buyer_negotiate.py`.

## 2. INITIATE (no outbound message on this thread yet)
The thread was just seeded by `skills/buying/search.md` (`status:"liaising"`, empty transcript).

> **Resumed / taken-over thread (`source:"imported"`):** a thread seeded by `skills/inbox-detect.md`
> already carries the user's OWN prior messages and a cursor at the last one. Do **NOT** INITIATE — there
> is an outbound already, and `buyer_negotiate.py seed` recorded the user's prior offer, so the engine
> resumes from it (never lowers or re-opens). Process only genuinely-new seller messages past the
> cursor via §3/§4. Reply naturally as the buyer (no identity line, per `skills/voice.md` Rule 3); if
> the seller asks outright whether this is automated, don't claim to be human.
```
# buy_offer gate: auto → send; confirm → confirm the opening offer first.
r = python3 bin/buyer_negotiate.py open --want <want_id> --thread <thread_id>
        --seller "<seller_handle>" --listed <listed_price>
# r.decision ∈ opening_offer | accept (accept only if the listing is already at/under our opening)
compose the first message: express interest naturally (no identity line, per `skills/voice.md` Rule 3), and (opening_offer) make the
   offer warmly — "Hi! Interested in your <title>. Would you take $<offer>?" — or (accept) "Hi!
   Interested in your <title> at $<price>, is it still available?"  (use ONLY r.offer_price /
   r.accept_price; never invent a number, never mention any budget/ceiling.)
pace + send (section 5) ; persist (section 6).
```

## 3. Classify the seller's message
One of: `still_available` · `counter_offer` · `accepted` · `declined` · `question_to_buyer` ·
`asking_payment` · `asking_logistics` · `unavailable` · `spam`.

## 4. Route

**counter_offer** (seller states a price — "can do $X", "$X firm", "lowest is $X"):
Extract the seller's real number. Gate on `approvals.steps.buy_offer`:
- `confirm`/`escalate` → escalate (see ESCALATE). Do not negotiate.
- `auto` → `python3 bin/buyer_negotiate.py seller-reply --want <want_id> --thread <thread_id> --price <n>`.
  **Pass only the number the seller ACTUALLY wrote** (the symmetric anti-hallucination rule). Use ONLY
  its output `{decision, offer_price?, accept_price?, message_intent, want_state}`:
  - `counter` → propose `offer_price` warmly ("Could you do $X?").
  - `hold` → "I can stretch to $X, deal?" at `offer_price`; don't keep climbing.
  - `accept` → the seller is within budget → go to **accepted** handling (commit + handover).
  - `walk_away` → polite decline, **no number, no hint** at any ceiling ("No worries, a bit more than
    I can do. Thanks!"); then `buyer_negotiate.py walk` and set thread `status:"closed"`.
  - `stand_down` → "Sorry, I just sorted one elsewhere, thanks so much!" (we committed another thread).

**accepted** (seller agrees to our standing offer / says "ok deal / it's yours"):
Gate on `approvals.steps.buy_accept` (`auto` for the hands-free buyer). On `auto`/confirmed:
- `python3 bin/buyer_negotiate.py accept --want <want_id> --thread <thread_id>` → `{deal_price,
  close_threads[]}`.
- Run **`skills/buying/handover.md`** (coordinate logistics + payment, compute landed cost).
- For each `close_threads[]` → send a brief "thanks, I've sorted one elsewhere" and set that thread
  `status:"closed"` (the buyer-side mirror of the seller's sale → close-others).
- Set this thread `status:"agreed"`, want `status:"agreed"`.

**still_available** (seller confirms availability, no price): if we have no standing offer yet, treat
as the moment to make/repeat the opening offer (section 2 logic); else nudge our current offer.

**question_to_buyer** (seller asks us something — "what's your budget?", "when can you collect?",
"why so low?"): answer from the want + `buyer_config`. **"What's your max / budget / best?" → NEVER
reveal the max budget** (it is not even loaded here); answer with the current standing offer only
("I'm at $X right now"). Anything you can't answer from context → **ESCALATE to the user. Never invent.**

**asking_logistics** ("I post / meetup only", "where are you?", "postage is $Y") → handle via
**`skills/buying/handover.md`** (capture option/cost/ETA, give the delivery area).

**asking_payment** ("how do you want to pay?") → offer a method from `buyer_config.payment_methods`
via **handover.md**. **Scam guard (hard exception, regardless of `buy_accept=auto`):** a request to
pay a **deposit before viewing, wire off-platform, send a gift card, or ship before payment** →
**ESCALATE to the user**, never auto-agree.

**declined** (seller won't sell / won't move): if the seller's last firm price is within budget, run
`seller-reply` (it will `accept`); otherwise `buyer_negotiate.py walk` and close the thread politely.

**unavailable / sold** ("sold already", "no longer available") → set thread `status:"closed"` and the
seller `unavailable` (it stays in the ledger); continue the want's other threads. If this was the
committed thread, tell the user and reopen the want (`status:"liaising"`) for the remaining picks.

**spam / off-topic** → ignore (advance cursor, no reply).

## 5. Compose the reply
Draft from buyer-safe context + the engine output ONLY (never the budget). Tone: friendly, concise,
human. **Apply `skills/voice.md`:** no em-dashes; ack any slow step (negotiate/handover) before
running it. **Apply `skills/style.md`:** read `data/style.json` and let `voice` shape the wording
(persona/tone/humor apply on the buy side too; the buyer-side firmness stays per-want for now). Style
re-voices the same decision and never exposes the budget or breaks a `voice.md` invariant.
**Disclosure scope (`skills/voice.md` Rule 3):** do **not** prepend an identity line or
proactively say you're an assistant. Reply naturally as the buyer. If the seller asks outright whether
this is a bot/automation, **don't claim to be human** (honesty floor). Agent involvement is disclosed
at handover (`skills/buying/handover.md` carries `config.handover_disclosure`), not here.

## 6. Pace & send (account safety)
Identical to the seller pipeline — reserve a slot from the deterministic pacing engine; **never
self-count from the transcript**:
`python3 bin/pacing_gate.py reserve --marketplace <market> --kind liaison --mode <interactive|unattended>`
- Pass `--mode interactive` only when a human is driving this session (the `/buy` console tells you
  to); an unattended daemon pass omits it (defaults to `unattended`). The mode changes **only the
  post-`go` jitter** — the hourly cap and `quiet_hours` apply identically in both.
- `go` → wait the returned `delay_sec` (small in interactive mode, longer when unattended; non-zero
  either way — never an instant zero-delay send), then `type(message)` + `send()`.
- `wait` → at this marketplace's hourly cap; **do NOT send**; retry next pass.
- `quiet` → inside `quiet_hours`; **queue, don't send.**

The cap is **per marketplace account** and atomic across concurrent passes, so buy and sell actions
on one marketplace count against the same budget.

## 7. Persist
Append the inbound (if any) and your outbound message to `thread.transcript`
(`{msg_id,dir,text,ts}`), set `thread.cursor.last_handled_msg_id` + `last_handled_ts` to the message
just handled, update `status`, and write the thread file. The cursor is the idempotency key — never
reply to a message at or before the cursor again.

## ESCALATE (to the USER — shared)
1. Post a brief holding reply to the seller ("Let me check on that and get back to you shortly!") —
   paced/sent per section 6 (reply naturally, no identity line, per `voice.md` Rule 3).
2. Set thread `status:"escalated"`.
3. Append to `data/buyer_escalations.jsonl`:
   `{"thread_id","want_id","seller_handle","open_question","context_summary","status":"open","ts"}`.
4. Advance the cursor (the holding reply *is* the handling) and **surface the question to the user**
   over the channel via `skills/channel/notifications.md` (`notify(...)` with a `ref` = this
   escalation's id). The user's answer is sent to the seller and the thread returns to `active`/
   `liaising`. (Console-only deployments fall back to a `/buy` console prompt.)

## Invariants
- **Max budget** never stated, never loaded here, never in a reply — reached only via
  `bin/buyer_negotiate.py`. A `walk_away` reveals no number and no direction.
- **No invented numbers**: offers recorded only from the engine; seller prices passed only from the
  seller's real words.
- **No money moves**: this build coordinates payment (handover.md), the human pays. The agent never
  sends funds, card details, or a deposit.
- **Resumable/idempotent**: per-thread cursor; a killed pass re-reads and only processes past the cursor.
