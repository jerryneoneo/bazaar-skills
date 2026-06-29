# Free relist offer — take the platform's free visibility refresh

When a marketplace's in-app **assistant** offers to relist / renew / bump one of the seller's own
listings, that is a FREE way to push the listing back to the top of search and buyer feeds. This
skill takes the offer **only if it is genuinely free**, respects a per-item cooldown and pacing, and
then drops a short positive note on the control channel so the seller sees the agent working. It is
**marketplace-agnostic**: the per-market browser steps and the free-vs-paid read live in
`skills/listing-flows/<market>.md` ("Relist offer recipe"); this skill is the shared decision +
guardrail + notice.

Triggered from the sell pass (`.claude/commands/bazaar-run.md` §2) when a platform relist offer is
pending for an enabled market. Detection is deterministic: `bin/inbox_scan.py` flags
`platform_offers[<market>]` when a fresh `carousell_assistant` (or other `ASSISTANT_HANDLES`) row
matches a relist/renew/bump intent (`RELIST_OFFER_RE`). Other promo from the assistant is ignored.

> **THE HARD RULE — never spend.** Taking a relist is allowed ONLY when the action is free. A coin
> cost, a currency amount, "X coins", "purchase", a paid "Spotlight"/"Promote"/boost, or ANY
> ambiguity means **do not act** (treat as paid). This is a money guard in the same spirit as
> `floor_gate` / `budget_gate`: it fails CLOSED. There is no paid path and no config switch that
> turns paying on. The seller is never asked to approve a spend here.

> **Load config first.** `data/config.json` for `approvals.steps.free_relist`, `max_actions_per_hour`
> / `quiet_hours` (pacing applies even though most steps read-only), and `relist_cooldown_days`;
> `data/seller_config.json` for enabled `marketplaces` + `currency`. Voice per `skills/style.md`
> (warm, plain) and `skills/voice.md` — **no em-dashes**.

## 1. Identify the target listing(s)
Open the assistant offer (per the market recipe) and resolve which of the seller's listings it is
about (the offer names a title / links a listing). Match it to a managed item in `data/items/<id>.json`
by `listing_urls.<market>` or title. If the offer is generic ("relist your items") and resolves to
several, handle each in turn, one relist action at a time. If it resolves to **nothing managed**, do
nothing (advance the assistant-thread cursor) and stop — never relist an item we do not manage.

## 2. Cooldown gate (per item, per market)
For each candidate item: `python3 bin/relist_state.py due --item <item_id> --market <market>`.
If `due` is false, skip that item silently (no action, no message) — the cooldown stops us relisting
the same listing every time the assistant nudges. Continue to the next candidate.

## 3. Read the offer + decide free vs paid (fail-closed)
Run the market's **Relist offer recipe** (`skills/listing-flows/<market>.md`). It reads the actual
relist control / confirmation and returns one of:
- `free` — an explicitly free relist / renew / "yes, still available" with **no charge** shown.
- `paid` — any coin/currency/purchase/Spotlight/Promote/boost cost is shown.
- `unknown` — could not confirm it is free.

**Only `free` proceeds.** `paid` and `unknown` → **skip silently** (step 5).

## 4. Free → take it (gated, paced, stamped)
Read `mode = config.approvals.steps.free_relist` (default `auto`; see `skills/bazaar-config.md`):
- `auto` → act. `confirm` → `confirm()` the one-line preview first, proceed only on yes.
  (`escalate` is not a meaningful value here — a free action never needs a money escalation; treat
  it as `confirm`.)
- Reserve a slot: `python3 bin/pacing_gate.py reserve --marketplace <market> --kind relist --mode
  <interactive|unattended>`; wait the returned `delay_sec`. If pacing denies, defer to a later pass.
- Execute the free relist via the market recipe. If the platform reports "already relisted / bumped
  recently", treat it as done (not a failure).
- Stamp the cooldown: `python3 bin/relist_state.py mark --item <item_id> --market <market>`.
- Advance the assistant-thread cursor so the same offer is not re-handled next pass.

## 5. Paid / unknown / not-due → skip silently
Do not spend, do not message the seller. Advance the assistant-thread cursor (so a `paid` offer does
not re-fire a pass every cycle) and move on. Paid promotion is simply never taken.

## 6. Inform the seller (only when something was actually relisted)
After the pass, if one or more listings were relisted for free, send **one** warm routine update —
the seller likes knowing the agent is actively working. Batch them into a single line; **never**
message when nothing was relisted (no "I checked and skipped a paid one" noise).

- When `$BAZAAR_RESOURCE` is set (you are one of several per-marketplace workers), **enqueue** it so
  the supervisor is the single writer:
  `python3 bin/channel_outbox.py enqueue --kind notify --text "<notice>"`.
- Otherwise send directly via `skills/channel/notifications.md` `notify`.

Voice per `skills/style.md` + `skills/voice.md` (no em-dashes), framed as a done routine update, not
a question. Example shapes (adapt, do not template):

> "Gave \"<title>\" a free bump on Carousell, fresh eyes on it."
> "Relisted 3 of your listings on Carousell for free today, back near the top of search."

## Guardrails
- **Never spend** (step 3's hard rule). A paid control is never clicked, no matter the config.
- Logged-out / checkpoint / verification on the market → stop and escalate re-auth ("re-auth your
  <market>"), same as the other recipes; no tight-loop retries.
- Idempotent: the cooldown ledger + the cursor advance mean a relist is taken at most once per item
  per `relist_cooldown_days`, even across crashes / re-runs.
- `--dry-run` → log each decision (free/paid/skip) and the verbs, click nothing, stamp nothing.
