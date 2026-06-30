---
description: Detect existing listings on connected marketplaces, manage + cross-list them
---

# /sell-detect — find and distribute existing listings

Thin entry point into the distribution flow. Scans every **enabled** marketplace for listings the
seller created **outside** SELLY, offers to bring each under management (watch chats + negotiate),
and cross-lists items to the seller's other suitable marketplaces. Also recommends platforms the
seller hasn't set up yet when an item is a good fit.

→ Execute **`skills/channel/distribution.md`** starting at **SCAN**. Apply `skills/voice.md` to
every message (no em-dashes; ack before any slow scan/publish step).

Prerequisites:
- Onboarding done (`data/seller_config.json` exists). If not, run `skills/channel/onboarding.md` first.
- Logged in to the target marketplaces in your real Chrome (Claude in Chrome).

Notes:
- Dedup anchor is the normalized listing URL — re-running this never re-imports or re-cross-lists
  the same listing. A fuzzy title match is confirmed with you before merging.
- Imported items ask you for the hidden floor + size; the floor is written only to
  `data/floors/<item_id>.json` (never elsewhere). The exact pickup address stays in
  `seller_config.json` (used only by `bin/shipping.py`).
- Offers are gated by `config.approvals.steps.distribution` (`balanced` default: `confirm`).
- Honors `--dry-run` (scan/read run; imports, setups, and cross-list publishes are logged, not executed).
- Also reachable as a `/detect` command over the seller channel via `/sell-run`. You rarely need to
  run it by hand: `/selly-run` (§2b) auto-scans each enabled market on a cadence
  (`config.scan_interval_hours`, default 24h) and offers to manage + cross-list anything it finds.
  `/sell-detect` forces an immediate scan of **all** markets now.
