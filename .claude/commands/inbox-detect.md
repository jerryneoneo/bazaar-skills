---
description: Review every marketplace inbox and offer to take over chats you started on your own
---

# /inbox-detect — review the inbox, offer to take over untracked chats

Thin entry point into the inbox-sweep flow. Reads the **inbox** on every enabled marketplace (the
union of your seller and buyer markets), finds threads you started **outside** SELLY, and offers to
take each over — **purchase chats** (you messaged a seller about a listing) and **listing chats**
(someone messaged a listing you never imported). On a buy takeover it sets a private budget and
negotiates hands-free, resuming the conversation mid-thread; on a sell takeover it brings the listing
under management via the importer.

→ Execute **`skills/inbox-detect.md`** starting at **SWEEP** with `scope:"both"`. Apply `skills/voice.md`
to every message (no em-dashes; ack before the slow sweep).

Prerequisites:
- Onboarding done (`data/seller_config.json` and/or `data/buyer_config.json` exists). If not, run
  `skills/channel/onboarding.md` first.
- Logged in to the target marketplaces in your real Chrome (Claude in Chrome).

Notes:
- Dedup anchor is the namespaced thread id (`<market>:<id>`) — re-running this never re-offers a chat
  already managed, and a chat you said "leave it" to is recorded in `data/takeover_seen.json` and never
  re-offered.
- Direction is decided by the first message in each thread (you spoke first → a buy chat; the other
  party spoke first → a sell chat), with your owned-listing URLs as the tie-breaker.
- Buy takeovers ask you for a private target + max → written only to `data/budgets/<want_id>.json`
  (never shown to sellers). The agent resumes from your existing messages and never re-sends an
  opening offer. Sell takeovers reuse `/sell-detect`'s import (floor + size).
- The offer to step in is gated by `config.approvals.steps.takeover` (a hard floor: `confirm` default,
  never `auto`).
- Honors `--dry-run` (sweep/read run; budget writes, ledger seeds, thread/want seeds, and any
  cross-list enqueue are logged, not executed).
- You rarely need to run it by hand: `/selly-run` (§2b) auto-sweeps each enabled inbox on a cadence
  (`config.scan_interval_hours`, default 24h) and offers to take over anything it finds. `/inbox-detect`
  forces an immediate sweep of **all** inboxes now. For the buy side only, use `/buy-detect`.
