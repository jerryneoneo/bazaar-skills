---
description: SELLY Skills — the seller agent loop (channel + buyer inboxes); sell-scoped alias of /selly-run
---

# /sell-run — the seller loop (alias)

The seller half of the unified agent. This is **`/selly-run --scope sell`**: it watches the control
channel for setup/listing/decisions and the **buyer inboxes** on every enabled marketplace, and skips
the buy side. Behavior is exactly what `/sell-run` has always done; the shared loop body now lives in
`.claude/commands/selly-run.md` so the sell and buy sides stay in one place.

→ Execute **`.claude/commands/selly-run.md`** with `--scope sell`. Apply `skills/voice.md`.

- Use **`/selly-run`** (no scope) to run sell + buy together in one pass.
- Use **`/buy-run`** for the buy side only.
- Use **`/sell-watch`** for the buyer inboxes only (no channel), for console/testing.

Keep a session open; wrap with `/loop` for periodic polling (e.g. `/loop /sell-run`). Resumable and
idempotent. Honors `--dry-run` (browser actions and channel sends logged, not executed).
