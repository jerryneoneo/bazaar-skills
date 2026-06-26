---
description: Buyer-side inbox watch loop across marketplaces (alias; /sell-run is the full agent)
---

# /sell-watch — the buyer-side inbox loop (alias)

Watches the buyer inboxes on every enabled marketplace through the seller's
**real Chrome session** (Claude in Chrome) and handles new messages via the reply pipeline.
This is the **buyer side only** — for the full agent (seller channel + buyers together) use
**`/sell-run`**. Use `/sell-watch` for console/testing without a channel adapter.

**Semi-attended:** keep the session open; wrap with `/loop` (e.g. `/loop 5m /sell-watch`).

Read `skills/browser-actions.md`, `skills/reply-pipeline.md`, and `skills/voice.md` before acting.
(Under the daemon, read `reply-pipeline.md` in full only when a message needs the escalation path
or a non-trivial route — for plain price/shipping/availability replies the routing summary below
plus the deterministic scripts are enough. Don't re-read every skill on every pass.)

## Cost discipline — fast path (daemon passes)
A cheap non-LLM probe (`bin/buyer_peek.py`) already confirmed there's new activity before this
pass launched, and seeded `$BAZAAR_BUYER_PEEK_TEXT` with the `[marketplace] snippet` that changed.
- Go **straight to that marketplace** and open only the thread(s) with messages past their cursor.
- Prefer a **targeted thread read** over a full-page `browser_snapshot` of the inbox — snapshots are
  the single biggest token cost per pass. Snapshot only when you can't otherwise locate the thread.
- If `$BAZAAR_BUYER_PEEK_FORCED=1`, this is the periodic safety-net sweep (the probe found nothing
  but we check anyway): do a light scan of enabled inboxes, reply to anything genuinely past cursor.

## One pass of the loop
1. For each enabled marketplace (`id, sel in seller_config.marketplaces.items() if sel.enabled`;
   apply the array→object read-shim in `skills/marketplaces.md`) — **prioritising the marketplace
   named in `$BAZAAR_BUYER_PEEK_TEXT`**: `navigate(<id> inbox)`, then locate the active thread
   (targeted read preferred; `read_inbox()` only if needed). Thread ids are namespaced `<id>:<thread>`.
2. For each thread with activity:
   a. Load `data/threads/<thread_id>.json` (create a fresh record if new: fields
      `thread_id, item_id, buyer_handle, cursor, status, round_number, transcript`).
      Match `item_id` from the thread's item hint to a `data/items/*.json`.
   b. `read_thread(thread_id)` → ordered messages.
   c. **Filter to messages newer than `cursor.last_handled_msg_id`.** If none, skip
      this thread (this is the double-reply guard — never re-handle at/before cursor).
   d. Skip threads whose `status in {escalated, lost, handover}`. `escalated` is waiting on the
      seller — it resolves via the channel (`notifications.md`) or `/sell-resolve`. `lost` is
      terminal (deal dead / item gone) and `handover` is terminal (the seller chose "deal other
      ways" and now runs the chat themselves) — **never re-engage** either, even if a new buyer
      message arrives. In all cases do nothing and move on (no reply, no cursor change).
   e. For each new message **in order**, run the per-message pipeline in
      `skills/reply-pipeline.md` (resolve → classify → route → compose → pace → send
      → persist + advance cursor).
3. After all threads: report a one-line summary — handled, escalated (list the open
   questions), skipped, and any pacing cap / quiet-hours hold.

## Resumability
State lives entirely in the thread files' cursors. If this command is killed
mid-pass and re-run, it re-reads each thread and only processes messages past the
cursor — no double-replies. Safe to stop and restart anytime.

## Pacing & safety (enforced every pass)
- Respect `max_actions_per_hour`, `reply_delay_sec` jitter, and `quiet_hours` from
  config (details in the pipeline, step 5). Never send instantly; never exceed caps.
- Process each thread's messages in order; handle threads sequentially.
- **Floor never enters context** — only `bin/floor_gate.py` sees it, only in `auto` mode.
- Logged-out / checkpoint / captcha on a marketplace → **stop that market's pass and tell the
  seller to re-auth**, keep the other market running. Do not retry rapidly.

## Modes (`config.approvals.steps`, see `skills/bazaar-config.md`)
- `buyer_replies` → `auto` sends answers to questions/shipping/availability; `confirm`/`escalate`
  surfaces them first.
- `offers` → `auto` negotiates below/at-list offers via the floor gate; `confirm`/`escalate`
  surfaces every offer to the seller.
- `above_list_bids` is always `confirm`/`escalate` (never auto) — the seller approves first.
- (A missing `approvals` block is derived from the legacy `autonomy_mode` via the migration shim.)

Honor a `--dry-run` argument: log the actions and intended replies, send nothing.
