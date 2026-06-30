---
description: SELLY Skills — the buyer agent loop (channel + seller-reply threads); buy-scoped alias of /selly-run
---

# /buy-run — the buyer loop (alias)

The buyer half of the unified agent. This is **`/selly-run --scope buy`**: it watches the control
channel for new wants/decisions (`/search`, `/status`, `/pause`) and polls the **seller-reply threads**
for every want being pursued, continuing each negotiation hands-free and coordinating the handover —
and skips the sell side. The shared loop body lives in `.claude/commands/selly-run.md`.

→ Execute **`.claude/commands/selly-run.md`** with `--scope buy`. Apply `skills/voice.md`.

- Use **`/selly-run`** (no scope) to run buy + sell together in one pass.
- Use **`/sell-run`** for the sell side only.
- Use **`/buy-search`** to jump straight into a search without the loop.

Why a loop: sellers reply asynchronously, so the agent must poll for the reply and continue the
conversation (the buy-side mirror of how the sell loop polls async buyers). Keep a session open; wrap
with `/loop` for periodic polling (e.g. `/loop /buy-run`). Resumable and idempotent on per-thread
cursors. Honors `--dry-run` (browser actions and channel sends logged, not executed).
