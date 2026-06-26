# Handover — logistics + payment coordination (buyer side)

Runs once a deal is struck (the liaison pipeline hit `accept` → `bin/buyer_negotiate.py accept`).
Coordinates how the buyer gets the item and how they'll pay, then hands the user a summary. **No money
moves in this build** — there is no checkout/escrow rail (that is the separate Phase 5 workstream), so
the agent agrees the method and details, and the **human completes the actual payment and collection.**

> Why not `bin/shipping.py`? That engine computes the *seller's* delivery fee from the seller's own
> `origin` + zone table — it does NOT apply to an external counterparty whose address/zones we don't
> have. On the buy side we **ask the seller** for their postage cost instead, and do plain arithmetic
> on the numbers they state. The authoritative `deal_price` comes from `bin/buyer_negotiate.py`.

Inputs: the thread file `data/buyer_threads/<thread_id>.json`, the want, `buyer_config`
(`delivery_area`, `payment_methods`), and `deal_price` from the `accept` call.

## Steps
1. **Logistics (ack first, per voice.md):** this is the buy-side review point, so prepend
   `config.handover_disclosure` to your first message here (agent involvement is disclosed at
   handover, since there's no buy-side checkout rail yet; `voice.md` Rule 3). Then ask the seller
   their option + cost + timing, e.g.
   "Great! Do you post or prefer a meetup? If posting, how much to <delivery_area.area> and roughly
   how long?" Capture `shipping_cost` (0 if self-collect / free), `method` (`post` | `meetup` |
   `collect`), and `eta`. If the seller posts, give the delivery area/postcode when they ask (the
   `delivery_area` is shared only now, at deal time — not volunteered earlier).
2. **Landed cost:** `landed = deal_price + shipping_cost` (plain arithmetic on the seller-stated
   postage + the engine's `deal_price`). Record `shipping_cost`, `method`, `eta`, `landed` on the
   thread's seller entry in the ledger.
3. **Payment method:** agree one from `buyer_config.payment_methods` that the seller accepts (e.g.
   PayNow, bank transfer, cash on collection). **Scam guard:** if the seller demands a deposit before
   viewing, an off-platform wire, a gift card, or shipping before payment → do NOT agree; **ESCALATE
   to the user** (see liaison-pipeline ESCALATE) regardless of `buy_accept`.
4. **Hand-off summary to the user** (the hands-free deal ping) via `skills/channel/notifications.md`
   `notify(...)`, kind `buy_deal` (the buyer-side mirror of the seller's `sale`):
   > "🎉 Deal on <title>: <seller_handle> agreed <deal_price>, +<shipping_cost> <method> to
   > <delivery_area.area> = <landed> <currency>. Payment: <method agreed>. You pay + collect — want me
   > to forward their PayNow/bank details, or anything else?"
   Gated by `approvals.steps.buy_accept` (the hands-free buyer has this `auto`, so the summary is sent;
   the user still performs the payment, which is the real money gate).
5. Set the thread `status:"agreed"` and the want `status:"agreed"`. The user takes it from here
   (pays + collects); when they confirm it's done, mark the want `status:"bought"`.

## Invariants
- **No funds move** — the agent never sends payment, card numbers, or a deposit. It coordinates and
  summarizes; the human pays.
- **Delivery area** is revealed to the seller only at this step (deal agreed), never earlier — the
  buyer mirror of how the seller's address is only ever turned into a quoted fee, never volunteered.
- **Apply `skills/voice.md`** (no em-dashes; ack the slow ask before sending; no proactive identity
  line in chat, per Rule 3). The agent-involvement disclosure is the `config.handover_disclosure`
  prepended on the first logistics message (step 1), the buy-side review point.
