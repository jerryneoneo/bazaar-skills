---
description: Sweep all listings, marketplaces, and setup for anything not attended to, then propose the work
---

# /selly-catchup — sweep everything, report what's waiting, propose the work

Thin entry point into the status-sweep flow. Does a deep, mostly read-only sweep across three
surfaces and sends one grouped digest of everything not yet attended to, then offers to act on each
group:
- **Interface + health** — channel bound, browser reachable, each enabled marketplace logged in,
  daemon loaded, agent paused or not.
- **Local state** — open escalations (both sides), unread managed threads (both sides), draft /
  undistributed listings, open checkouts, open wants, overdue cadence (`bin/triage.py`).
- **Live marketplaces (deep)** — per enabled, logged-in market: unmanaged / undistributed listings
  and sold/removed drift (the `distribution.md` SCAN read), plus chats you started solo (the
  `inbox-detect.md` SWEEP read). Both collect-only: they detect, they do not act.

→ Execute **`skills/selly-catchup.md`** starting at **HEALTH** with `scope:"both"`. Apply
`skills/voice.md` to every message (no em-dashes; ack before the slow sweep).

Prerequisites:
- Onboarding done (`data/seller_config.json` and/or `data/buyer_config.json` exists). If not, run
  `skills/channel/onboarding.md` first.
- Logged in to the target marketplaces in your real Chrome (Claude in Chrome). A logged-out market is
  reported as a re-auth task and skipped in the deep sweep.

Notes:
- **Acts on nothing during the sweep.** It reports, then proposes. Each accepted item hands off to the
  skill that already owns it (`/sell-resolve`, `/sell-run`, `/buy-run`, `/sell-detect`, `/inbox-detect`,
  `/resume`), and that skill applies its own approval gate. "Handle all" RESUMES existing flows, it
  never starts duplicates.
- **Read-only and secret-safe.** It opens only non-secret state and marketplace pages; the floor and
  the max budget are never read.
- Turn-based and resumable: one market (or one question) per pass, persisted in
  `data/catchup_session.json`. Never interrupts an in-flight listing / distribution / buy / inbox flow.
- Honors `--dry-run` (everything is read; the report is logged not sent; no handoff runs).
- On-demand only. The daemon already auto-sweeps one due market per pass (`/selly-run` §2b);
  `/selly-catchup` forces a full sweep of **all** enabled inboxes and listings now. Wrap with
  `/loop /selly-catchup` if you want a periodic digest.
