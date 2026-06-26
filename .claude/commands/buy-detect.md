---
description: Review marketplace inboxes for purchase chats you started and offer to take them over
---

# /buy-detect — find purchase chats you started, offer to take them over

Buy-scoped alias of `/inbox-detect`. Reads the **inbox** on every enabled marketplace and finds
**purchase chats** you started on your own — you messaged a seller about their listing (e.g. you
pinged a few sellers about iPhones on Carousell). It groups them by item ("3 iPhone chats"), asks if
you want them handled, and on yes sets a private budget and negotiates each hands-free — resuming your
existing conversation mid-thread (no re-greeting, no duplicate offers). Listing chats (someone
messaging your own listing) are left to `/sell-detect`.

→ Execute **`skills/inbox-detect.md`** starting at **SWEEP** with `scope:"buy"`. Apply `skills/voice.md`
to every message (no em-dashes; ack before the slow sweep).

Prerequisites:
- Buyer onboarding done (`data/buyer_config.json` exists). If not, run `skills/channel/onboarding.md`.
- Logged in to the target marketplaces in your real Chrome (Claude in Chrome).

Notes:
- Same dedup, declined-set, secret-budget, and resume behavior as `/inbox-detect` (see that command),
  but only buyer-initiated threads are offered.
- The offer to step in is gated by `config.approvals.steps.takeover` (a hard floor: `confirm` default,
  never `auto`). Honors `--dry-run`.
- `/bazaar-run` (§2b) already sweeps inboxes on a cadence; `/buy-detect` forces an immediate buy-side
  sweep of all inboxes now.
