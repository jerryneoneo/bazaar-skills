# Reply pipeline — per-message decide → compose → send

Shared logic used by the buyer-side loop (`/sell-run`, or the `/sell-watch` alias) and by
`skills/channel/notifications.md` (after the seller answers an escalation over the channel;
`/sell-resolve` is the console-only fallback). Process **one buyer message at a time, in order**.

> **Load config first.** Read `data/config.json` for `approvals` (per-step gates — see
> `skills/bazaar-config.md`), `reply_delay_sec`, `max_actions_per_hour`, `quiet_hours`,
> `checkout_disclosure` (posted with the checkout link at close, §3b); and
> `data/seller_config.json` for `currency` + `shipping` (needed for the
> delivery-fee branch). If `config.approvals` is absent, apply the migration shim in
> `bazaar-config.md` (derives the step map from the legacy `autonomy_mode`/`listing_autonomy`/
> `close_gate`). All decisions below read `approvals.steps.<key>`: `auto` acts, `confirm` gates
> on `confirm()`, `escalate` always surfaces via `notify()`.

> **Parallelize independent work (speed within a pass).** Issue independent NON-browser tool calls —
> file reads, web lookups (WebSearch/WebFetch), and deterministic scripts (`shipping.py`,
> `availability.py`) — as **concurrent calls in a single turn**; never serialize work that has no
> data dependency. **Browser actions stay strictly serial** (one warm Chrome, one active tab).

## 1. Resolve context
**Terminal guard (first):** if the thread `status` is `lost` (deal dead / item gone) or
`handover` (the seller has taken the chat over, see §3b), **do not reply** — skip this message,
no compose/send, like `spam / off-topic` below. Neither is re-engaged even if a fresh buyer
message arrives. (Do *not* short-circuit on `escalated` here: escalation resolution legitimately
re-enters this pipeline after the thread is flipped back to `active`.)

Load — **in parallel, one turn** (independent reads) — the thread file
`data/threads/<thread_id>.json`, the buyer-safe item `data/items/<item_id>.json`, and
`qa_bank.jsonl` entries where `item_id` matches the item or is `"*"`. **Never load
`data/floors/<item_id>.json`.** The floor is reached ONLY by calling `bin/floor_gate.py`.

## 2. Classify the message
One of: `question` · `shipping` · `availability` · `price_offer` · `meetup_request` ·
`ready_to_buy` · `spam` · `unknown`.

## 3. Route

**shipping** ("how much to deliver to <area>?", "what's my total?", "do you ship to X?"):
Extract the buyer's area (and distance if known), then run
`python3 bin/shipping.py --item <item_id> --dest-area "<area>" [--dest-km <km>]`. Use ONLY its
output: if `covered` → quote `buyer_total` ("That's <price> + <total_fee> delivery = <buyer_total>
<currency>, shipped to your door."); if `covered:false` → politely say you can't deliver there
yet. **The exact origin address is never revealed** — only the computed fee.

**Delivery or total is a buy signal, go to the close (do not grind for an area).** When a buyer
engages on delivery or their total ("what's my total", "how much to deliver to me", "do I pay for
delivery", or repeated fee questions), they want to complete the purchase. Do NOT ask for their
area or compute a manual fee to chase the sale, that pre-commits the deal to the manual path
before §3b chooses how to close. Post the neutral holding line and go to **§3b Close**; on a
checkout close the link collects the address and computes delivery end to end. Run
`bin/shipping.py` only as an on-request ballpark for a buyer who explicitly just wants an estimate
before deciding and already gives an area, never loop asking for one.

**meetup_request** ("can we meet?", "can I self-collect?", "pay cash on pickup?"):
The agent never arranges meetups or offline payment, but the **seller may want to**. Do NOT say
"no meetups", do NOT explain ship-only, and do NOT redirect to a delivery quote. Treat the meetup
request as the close point: go to **§3b Close** so the seller picks, send a checkout link, or take
the chat over and arrange the meetup / payment / delivery themselves. Post the neutral holding
line ("Let me sort the best way to get this to you, back shortly!"), and never agree to meet or
transact offline yourself.

**availability** — two distinct shapes:
- **"Still available?" / "in stock?" / "still for sale?"** (the classic opener): confirm warmly it's
  available at `<price>` (and remaining qty if multi-unit), then a light nudge to close ("keen to grab
  it?"). **Stop there.** Do NOT add shipping, delivery, "islandwide", "no meetups", or "what area are
  you in" — none of that is asked for yet, and it pre-commits the deal before the close (§3b). Delivery
  surfaces only if the buyer asks (the **shipping** route) or at the close.
- **"When will it ship?" / "how soon?"** (timing): answer handover timing per
  `seller_config.availability.source`:
  - `manual` → run `python3 bin/availability.py <from> <to>` and answer from the windows.
  - `calendar_mcp` → follow its `instruction`: query the connected Google Calendar MCP for free/busy
    and answer when the seller can hand to the courier. **Never invent availability.**
  - `skip` → keep it vague ("usually within a day or two of payment"); don't promise a slot.

**price_offer** (a number, "will you take $X", "$X firm?"):
Extract the numeric offer. Gate on `approvals.steps.offers`:
- If `offers` is `confirm` or `escalate` → **escalate** (see ESCALATE). Do not negotiate.
- If `offers == "auto"` → run the negotiation engine (it owns all per-buyer and
  cross-buyer state — you pass only this thread's offer):
  `python3 bin/negotiate.py offer --item <item_id> --thread <fb|carousell>:<id> --buyer <handle> --offer <n>`
  Use ONLY its output `{decision, needs_seller_confirm, counter_price, bar_to_beat,
  leading_amount, message_intent, item_state}`. **Pass only what the buyer ACTUALLY wrote** —
  never invent or infer an offer number (the Harry incident was a hallucinated $200). If the
  message isn't clearly a numeric offer, treat it as a question, not an offer.

  Below-list haggling (≤ list):
  - `counter` → propose `counter_price` warmly ("I could do $X, deal?").
  - `hold_firm` → "$X is the best I can do" at `counter_price`; don't keep conceding.
  - `deflect_lowball` → decline with **no number** (never hint at direction/floor). The *wording*
    follows `style.voice.lowball_response` (see `skills/style.md`): `polite` (warm decline),
    `firm` (short, unmoved), or `cheeky` (playful pushback, still friendly). The decision and the
    no-number invariant never change, only the voice.
  - `accept_fcfs` → **at/below list, close gate = AUTO**: confirm "it's yours at $X" (provisional,
    first-come), post a brief holding line ("locking it in, sorting out checkout, back shortly"),
    then go to **§3b Close**. Do NOT ask for the delivery area or pre-quote a manual delivery total
    here — that is settled at the close (checkout link, or by the seller on handover).

  Competition / bidding (multi-buyer, single inventory across both platforms):
  - `fcfs_taken` → "someone's just committed, it's pending; I'll let you know if it frees up."
  - `bid_lead` (offer **above list** — `needs_seller_confirm:true`) → **do NOT tell the buyer
    'it's yours.'** Say "great offer, you're currently top at <leading_amount>, just confirming
    with the seller, back shortly." Then **notify the seller to confirm the bid**
    (notifications.md). Only after `negotiate.py confirm-bid` do you tell them it's theirs.
  - `bid_outbid` → it's competitive; **reveal the bar to beat**: "there's a higher offer at
    <bar_to_beat>. Want to beat it?" (per the seller's bidding setting).
  - `sold` → "sorry, this one's sold."

  **Invariants:** never state the floor; an above-list/bidding close is **never** auto-confirmed
  (`approvals.steps.above_list_bids` is hard-floored to `confirm`/`escalate`, never `auto`, and
  `negotiate.py` returns `needs_seller_confirm:true` for above-list regardless — the seller always
  approves first); offers are recorded only from the buyer's real words.

**question** (condition, what's included, payment, specs):
Search `qa_bank` (keyword + your judgment). Hit → answer using it. Miss, or anything
touching specs/defects not in the item file → **ESCALATE. Never invent facts.**
A qa_bank hit is still gated by `approvals.steps.buyer_replies` (same for the `shipping` /
`availability` / `meetup_request` answers above): `auto` sends the composed reply; `confirm`/
`escalate` surfaces it to the seller before sending (a qa_bank *miss* always escalates regardless).

**ready_to_buy** / accepted price → the price is finalised. Confirm it warmly, post a brief
holding line ("locking it in, sorting out checkout, back shortly"), then go to **§3b Close**.
Do NOT ask for the delivery area or pre-quote a manual delivery total — delivery + payment are
resolved at the close (checkout link, or the seller on handover).

**spam / off-topic** → ignore (advance cursor, no reply).

## 3b. Close — checkout vs handover
Reached once the price is finalised (`accept_fcfs`, a confirmed bid, or `ready_to_buy`), on a
**meetup_request**, or when a buyer engages on delivery/total as a buy signal (the **shipping**
route). The item is reserved provisionally for this buyer. Resolve **how** the deal closes from
`seller_config.close.default_method`:

- `"checkout"` → run
  `python3 bin/checkout.py issue --item <item_id> --thread <market>:<id> --price <agreed>` and post
  the returned `checkout_url` to the buyer together with `config.checkout_disclosure` (this is the
  agent-involvement disclosure + review point; payment + delivery are handled there). Set thread
  `status:"agreed"`, `close_method:"checkout"`, then notify the seller
  (notifications.md → close → checkout).
- `"manual"` → **full handover**: post a brief hand-off line to the buyer (the seller takes the chat
  from here), set thread `status:"handover"` (terminal — the agent stops auto-replying), and notify
  the seller (notifications.md → close → manual).
- `null`/absent → **ask the seller** which way to close. This is an escalation: surface the choice via
  `skills/channel/notifications.md` (record `pending{kind:"close"}`), set thread `status:"escalated"`,
  and resume on the seller's pick. Until they choose, the buyer has only the holding line.

**Why checkout is the default to nudge (not a mandate):** on checkout the agent handles payment +
delivery end-to-end — escrow, buyer protection, tracked shipping, zero seller fees. Manual means the
seller arranges payment + delivery themselves. Checkout wins because it is less work and safer, never
because it is forced; `"manual"` is always available. The link is issued only at/above floor
(`checkout.py` re-validates and the floor never appears).

## 4. Compose the reply
Draft from buyer-safe context + the gate/availability output ONLY. Tone: friendly, concise, human.
**Apply `skills/voice.md`:** no em-dashes (rewrite with a comma, colon, or full stop), and ack any
slow step (shipping/negotiate/availability/publish) before running it.
**Apply `skills/style.md`:** read `data/style.json` and let `voice` (persona/tone/humor/
lowball_response) shape the wording. Style re-voices the same decision; it never changes a number,
the decision, the disclosure, or any invariant in `voice.md` (which outranks it).

**INVARIANT — answer only what was asked; never volunteer fulfilment.** Reply to the buyer's actual
question and nothing more. **Never write "no meetups" or "ship only" in chat, even when the buyer
asks about a meetup**, a meetup request escalates the close per §3, it is never refused with a
ship-only line. Do NOT tack on shipping, delivery, "islandwide", or "what area are you in / where
should I send it" unless the buyer asked about delivery. That framing pre-commits the deal to a
manual path before the close (§3b) chooses how to settle. The buyer-safe item `description` may
itself contain listing-page text like "Ships P2P islandwide. No meetups." for the listing, that is
for the listing page; **do not parrot it into chat.** Delivery is
discussed only on a real **shipping** question (then it's an estimate; the binding total is at
checkout), and how the deal closes is decided in **§3b**, not up front.

**Disclosure scope (`skills/voice.md` Rule 3):** do **not** prepend an identity line or proactively
say you're an assistant. Reply naturally as the seller. If the buyer asks outright whether this is a
bot/automation, **don't claim to be human** (honesty floor). Agent involvement is disclosed at the
checkout page, not here (§3b close → checkout carries `config.checkout_disclosure`).

## 5. Pace & send (account safety)
Before sending, reserve a slot from the deterministic pacing engine — it is the single
authority, atomic across concurrent passes. **Never self-count from the transcript** (you
cannot see actions other passes took on this account):
`python3 bin/pacing_gate.py reserve --marketplace <market> --kind reply --mode <interactive|unattended>`
- Pass `--mode interactive` only when a human is actively driving this session (the `/sell` or
  `/buy` console tells you to). An unattended daemon pass omits `--mode` (defaults to
  `unattended`). The mode changes **only the post-`go` jitter** — the hourly cap and `quiet_hours`
  are enforced identically in both modes, so it can never let you over-send or send during quiet
  hours.
- `go` → **bracket the send so a crash can never split-brain the ledger** (a reply on the
  marketplace with a stale cursor and no journaled outbound — the Olaf failure). Do it in this exact
  order:
  1. **Record the intent BEFORE the send** (deterministic Python, not a hand-edit):
     `python3 bin/journal_send.py intent --thread <market>:<id> --market <market> --in-msg "<inbound_msg_id>" --text "<your reply>"`
     Capture the printed `id` as `<intent_id>`.
  2. Wait the returned `delay_sec`, then `type(message)` + `send()`. The delay is small in interactive
     mode (a few seconds, the natural cadence of a live chat) and longer when unattended (the
     anti-automation jitter). **Even in interactive mode it is non-zero on purpose — wait it; never
     fire an instant, zero-delay send (that's the automation tell).**
  3. **Commit immediately AFTER `send()` returns** (this writes the thread file atomically):
     `python3 bin/journal_send.py commit --thread <market>:<id> --intent <intent_id> [--status <new_status>]`
- `wait` → at this marketplace's hourly cap; **do NOT send.** Leave the thread at its cursor
  (idempotent) and tell the seller you are pacing; it retries next pass.
- `quiet` → inside `quiet_hours`; **queue, don't send.**

The cap is enforced **per marketplace account** and shared across sell and buy work, so a
buyer reply and a buy-side message on the same marketplace draw from one budget.

## 6. Persist
The bracket in §5 already persists. `bin/journal_send.py commit` writes the thread file
**atomically**: it appends both the inbound and your outbound row to `thread.transcript`
(`{msg_id,dir,text,ts}`; the outbound `msg_id` is `out|<iso-ts>`), advances
`thread.cursor.last_handled_msg_id` + `last_handled_ts` to the message just handled, sets `status`
(when you pass `--status`), and refreshes `updated_at`. The cursor is the idempotency key — never
reply to a message at or before the cursor again, and `commit` is itself idempotent (re-running the
same commit adds no duplicate rows and never double-advances).

**Do NOT hand-edit `data/threads/<id>.json`** — never "append to the transcript" or "write the
thread file" yourself. The deterministic `commit` call is the ONLY journaling path; a hand-edit is
what crashed mid-pass and dropped the Olaf outbound. If you set additional fields the bracket does
not cover (e.g. an `agent_note`), do that by re-reading and writing the file only AFTER `commit` has
landed the send + cursor.

## 7. Proactive follow-up (when a buyer went quiet)
Reactive replies above handle a buyer who wrote to us. The **follow-up** path handles a buyer who did
NOT: when our last message goes unanswered, `bin/followup_state.py` schedules up to 2 gentle nudges
(gentle escalation, ~1d then ~3d), then marks the buyer not interested (~3d later). It is OFF unless
`followup_enabled` (config) is on, and is driven by the buyer pass only when `$BAZAAR_FOLLOWUP=1`
(see the FOLLOW-UP MODE block in the buyer prompt). A nudge is **not** a new mechanism: compose a
short friendly line and send it through the EXACT §5 bracket (`journal_send.py intent` → pace →
`type`+`send()` → `journal_send.py commit`), then `python3 bin/followup_state.py mark-nudge --thread
<id> --side sell`. Always re-read the tail first and skip the nudge if the buyer has replied since the
scan (handle their reply normally instead). The not-interested **drop** is deterministic and never
touches thread `status` — a re-engaging buyer is still answered by §1-§6 as usual.

## ESCALATE (shared)
1. Post a brief holding reply to the buyer ("Let me check on that and get right back
   to you!"), **bracketed exactly like a normal send (step 5)** — `journal_send.py intent` →
   pace → `type`+`send()` → `journal_send.py commit --status escalated`. This is the precise
   Olaf failure: a holding reply went out but the cursor never advanced because the pass crashed
   before the hand-edit, so the next pass re-escalated. The bracket makes the holding reply +
   cursor advance atomic. Reply naturally, no identity line, per `voice.md` Rule 3.
2. The `commit --status escalated` in step 1 already set thread `status:"escalated"` **and** advanced
   the cursor (the holding reply *is* the handling) — do not re-advance or hand-edit the thread file.
3. Append to `data/escalations.jsonl`:
   `{"thread_id","item_id","buyer_handle","open_question","context_summary","status":"open","ts"}`
4. **Surface the open question to the seller over the channel** via
   `skills/channel/notifications.md` (`notify(...)` with a `ref` = this escalation's id; assist-mode
   offers carry `accept/counter/decline` actions). (Console-only deployments fall back to
   `/sell-resolve`.)
When the seller answers, the reply is sent and saved to `qa_bank` so the same question
auto-answers next time (the compounding loop).

**End-of-pass discipline (turn budget):** reserve your final turn to journal via
`bin/journal_send.py commit` and write the one-line summary; **never end a pass with an un-committed
send.** If you are running low on turns, stop opening new threads, commit what you have already sent,
summarise, and STOP — the next pass resumes from the cursors (per the buyer pass TURN BUDGET).
