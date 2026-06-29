# NOTIFICATIONS flow — escalations & alerts over the channel

Routes the buyer-side pipeline's escalations to the seller via `channel.md` verbs, and feeds
the seller's answer back. This **replaces** the console-only `/sell-resolve` (which remains a
fallback when `adapter=console`). It preserves the teach-the-Q&A-bank compounding loop.

Correlation: each `notify` is sent with a `ref` = the escalation id and recorded in
`channel_state.json → pending[]`. When a matching `action` (button) or `text` (free reply)
event arrives from `watch()`, resolve that pending entry and clear it.

> **Concurrency (when `$BAZAAR_RESOURCE` is set — you are one of several per-marketplace workers).**
> Do NOT write the control channel directly for a **background completion notice** ("✅ listed",
> "cross-listed to eBay", "done") — two workers sending at once would interleave. Instead **enqueue**
> it: `python3 bin/channel_outbox.py enqueue --kind notify --text "<notice>" [--ref <id>]`. The
> supervisor is the single writer and drains the outbox to the channel in FIFO order. Interactive
> escalations that need a seller decision (the `pending[]`/button flows below) are unaffected — they
> still `notify()` as usual.

## Outbound — when the pipeline escalates (from `reply-pipeline.md` ESCALATE)

**Price offer (assist mode):**
```
ref = escalation_id ; record pending {ref, kind:"offer", thread_id}
notify "💰 <buyer> offered <offer> on “<item.title>” (your list <list_price> <currency>)."
       actions=[accept=Accept, counter=Counter, decline=Decline]
```

**Unknown question:**
```
ref = escalation_id ; record pending {ref, kind:"question", thread_id, question}
notify "❓ <buyer> asks on “<item.title>”: \"<question>\"\nHow should I answer?"
       (free-text reply expected)
```

**Above-list BID — needs your confirmation (`approvals.steps.above_list_bids`; the Harry guard):**
This key is hard-floored to `confirm`/`escalate` (never `auto`), and `negotiate.py` independently
returns `needs_seller_confirm:true`, so an above-list close always surfaces here.
When `negotiate.py offer` returns `bid_lead` (`needs_seller_confirm:true`), the agent has told
the buyer "you're top, confirming with the seller" but has NOT committed. Surface it:
```
ref = bid_id ; record pending {ref, kind:"bid", item_id, thread_id, amount}
notify "📈 <buyer> bid <amount> on “<item.title>”, ABOVE your list (<list_price>).
        Real & want to accept? (I won't commit until you say so.)"
       actions=[accept=Accept bid, decline=Decline, ignore=Ignore]
```
A hallucinated/misread above-list number lands HERE, not in a sent "it's yours" — you catch it.

**Deal finalised → resolve the close method, then confirm completion (auto-negotiation):**
When `negotiate.py` returns `accept` (or a bid is confirmed), the item is now **reserved** for that
buyer and other active buyers are told it's pending. How the deal closes runs per
`reply-pipeline.md §3b`, keyed on `seller_config.close.default_method`:

- **Default set** (`"checkout"` or `"manual"`) → the agent acts without prompting (issues the link, or
  hands over) and sends the matching FYI from the inbound `close → …` rows below. No choice is surfaced.
- **Default unset** (`null`) → surface the choice and park until the seller picks:
```
ref = close_id ; record pending {ref, kind:"close", item_id, thread_id, price, buyer}
notify "🎉 Deal! <buyer> agreed <price> on “<item.title>”. How do you want to close?
        🔗 Checkout link: I handle payment + delivery end to end (escrow, buyer protection,
           tracked shipping, zero fees to you).
        🤝 Deal other ways: I hand the chat over and you arrange payment + delivery yourself."
       actions=[checkout=Send checkout link 🔗, manual=Deal other ways 🤝]
```
**Meetup / self-collect / offline request (never refuse with "no meetups"):** the agent does not
arrange meetups but the seller may want to. On `null` default, surface the choice (the buyer has
only a neutral holding line until the seller picks):
```
ref = close_id ; record pending {ref, kind:"close", item_id, thread_id, price:<list_price>, buyer}
notify "🤝 <buyer> wants to meet / self-collect for “<item.title>” (<list_price> <currency>).
        I don't arrange meetups. How do you want to handle it?
        🔗 Checkout link: I send a link, payment + delivery handled end to end (escrow, buyer
           protection, tracked shipping, zero fees to you).
        🤝 Deal other ways: I hand the chat over, you arrange the meetup / payment / delivery."
       actions=[checkout=Send checkout link 🔗, manual=Deal other ways 🤝]
```
If a default is set (`checkout`/`manual`), act per it without prompting.

Reserving the item is identical either way — only the *close* differs. (If the seller says "always do
this", persist their pick to `seller_config.close.default_method` so it stops asking.)

Once the link is sent (checkout) or the chat is handed over (manual), confirm completion so the other
listings come down — the **sale** gate (`approvals.steps.mark_sold`):
```
ref = sale_id ; record pending {ref, kind:"sale", item_id, thread_id, price}
notify "Did this sale go through?" actions=[done=Sold ✅, fell=Fell through]
```

**Listing anomaly (auto-listing pause, from `listing.md`):**
Auto-listing only pauses for a real anomaly. Each pauses the publish and waits:
```
price ≫/≪ market:  notify "⚠️ You set <price> but market looks like ~<median> (<source>).
                          List anyway, change price, or skip?" actions=[list=List anyway,
                          change=Change price, skip=Skip]
login/checkpoint:  notify "🔒 <platform> needs re-auth (login/checkpoint). I've paused,
                          log in in Chrome, then reply 'retry'." (free text)
field not found:   notify "🧩 Couldn't find <field> on <platform> (page may have changed).
                          Skip this platform, or retry?" actions=[retry=Retry, skip=Skip]
```

**Bazaar update available (from the daemon's throttled `update_check`):** a one-way heads-up, NOT a
pending escalation. The always-on daemon checks upstream on a slow cadence (`config.update_check_interval_hours`,
default 24h, read-only `git fetch`) and, when a newer Bazaar is available, sends ONE notice. It is
**NOTIFY-only — never auto-applied** (account safety); the seller runs `/bazaar-upgrade` when they
choose. Deduped per version via `update_check snooze` (a newer release still breaks through). The
supervisor ENQUEUEs it (single-writer drain); the single-flight loop sends directly.
```
notify "🆙 Bazaar update available: v<current> -> v<latest>. Run /bazaar-upgrade when convenient
        (I won't auto-update)."   # kind=notify, no actions — informational
```

## Inbound — resolving the seller's answer (dispatched by the loop on an `action`/`text` event)

Look up `pending[]` by `payload.ref`. Then:

**offer → accept:** run the buyer reply via `reply-pipeline.md`: confirm the price to the buyer and a
brief holding line, then **resolve the close per §3b** (issue link / hand over / ask, by
`close.default_method`). Do NOT ask the delivery area or pre-quote a manual total — that's settled at
the close.

**close → checkout:** run `python3 bin/checkout.py issue --item <item_id> --thread <thread> --price
<agreed>`, post its `checkout_url` to the buyer via the pipeline together with
`config.checkout_disclosure` (the agent-involvement disclosure + review point, per `voice.md` Rule 3),
set thread `status:"agreed"` + `close_method:"checkout"`. Confirm to the seller: "✅ Checkout link sent to
<buyer> for <price>. I'll handle payment + delivery, and ping you to ship once it's paid." Then emit
the **sale** completion confirm (Sold ✅ / Fell through) so the take-down fires when it closes.
(`checkout.py` rejects a below-floor price — the floor never appears here.)

**close → manual:** resolves from EITHER the button OR a free-text "deal other ways" / offline-terms
disclosure (a pickup address, "leave it outside", a PayNow/bank number, "cash on collection" — the
seller choosing to handle it themselves; routed here by `bazaar-run.md` §1). Post a brief hand-off
line to the buyer ("You're all set at <price>! The seller will take it from here to sort out payment
and delivery with you directly. Thanks 🙏"), set thread `status:"handover"` (terminal — the agent
stops auto-replying on it). Confirm to the seller, spelling out what they now own and promoting the
rail ONCE (reversible): "🤝 Handed <buyer> to you on “<item.title>” at <price>. You'll sort the
pickup time, place, and payment (e.g. PayNow) with them directly. I won't add your address or PayNow
to the listing, those stay private. If you'd rather not coordinate it, the checkout link handles
payment + delivery for you (buyer protection, tracked shipping, zero fees), just say 'checkout' and
I'll send it. Otherwise you're all set; I've stopped auto-replying on that chat." NEVER write the
volunteered address or payment number to the listing, item file, qa_bank, or config — discard it.
Then emit the **sale** completion confirm so the take-down still fires when you mark it sold.

**Offline terms volunteered with NO pending close** (the seller states meetup/pickup/offline-payment
logistics with no deal in flight): do NOT start or edit a listing, and do NOT store the address or
payment number anywhere. Reply that meetups and offline payment are theirs to arrange directly with
the buyer at deal time (the agent ships / runs checkout, it doesn't arrange meetups), that those
details won't appear on the listing, and offer to make checkout the default close (promote the rail):
"Meetups and PayNow are yours to arrange with the buyer directly, and I keep them off the listing.
Want me to make the checkout link your default close? It handles payment + delivery end to end
(buyer protection, tracked shipping, zero fees)." If they say yes, persist
`seller_config.close.default_method:"checkout"`.

**offer → counter:** `ask "Counter at what price?"` → send the seller's number to the buyer
("I could do $X, deal?"). (The floor gate is **not** used for seller-entered numbers.) Keep
thread `active`.

**offer → decline:** reply politely holding at list price; keep thread `active`.

**question → (free-text answer):** send the seller's answer to the buyer via the pipeline
(reply naturally, no identity line, per `voice.md` Rule 3), then append to `data/qa_bank.jsonl`
`{item_id, q:<question>, a:<seller answer>, tags:[], source:"escalation", added_at:<today>}`
so the same question auto-answers next time.

**bid → accept:** run `python3 bin/negotiate.py confirm-bid --item <item_id> --thread <thread>`.
Then tell the winning buyer "🎉 it's yours at <amount>!" and **resolve the close per §3b** (don't ask
for the address — that's handled at checkout, or by the seller on handover). Tell any
`tell_others`/outbid buyers the item's no longer available. The seller still confirms completion via
the `sale → done` step before the other listing is taken down.

**bid → decline / ignore:** do nothing binding. If decline, optionally tell the buyer "thanks,
I'll stick with my asking price." Leave the listing live; clear the pending entry.

**sale → done (Sold ✅):** gated by `approvals.steps.mark_sold` — the "Did the sale go through?"
notify above IS the `confirm` gate (default `balanced`); under `auto`, skip that notify and run the
take-down immediately on `negotiate.py accept`. Run `python3 bin/negotiate.py confirm-sold
--item <item_id> --thread <winning thread>`. Then act on its output: for each `take_down[]` entry,
follow `skills/listing-flows/<platform>.md` **take-down recipe** to remove that platform's listing
(prevents double-selling); for each `close_threads[]` id, send "sorry, this just sold" and set
the thread `lost`. Confirm back to the seller: "Done, removed the <other> listing and closed
the other chats."

**sale → fell through:** run `python3 bin/negotiate.py release --item <item_id>` (item back to
`available`, other buyers re-open). Tell the seller "released, back on the market."

**listing anomaly → list anyway / retry:** resume the paused publish. **change price:** `ask`
the new price, update the floor/listing, re-run the anomaly check, then publish. **skip:** abort
that platform's publish, leave the listing unposted there.

After any resolution: mark the escalation `resolved` in `data/escalations.jsonl`, remove the entry
from `channel_state.pending[]`, and set the thread status the resolution implies — `active` for
offers/questions, `agreed` for `close → checkout`, terminal `handover` for `close → manual`, `lost`
for `sale → done`.

## Guardrails
- Never fabricate an answer for the seller — if they don't know, leave it open.
- Seller-entered prices go straight through; the **floor never appears** here (no `floor_gate`
  read; the floor isn't part of any notify).
- Idempotent: a resolved/absent `ref` is a no-op (handles double taps / restarts).
