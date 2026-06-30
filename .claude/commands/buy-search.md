---
description: Search the connected marketplaces for an item (need → search → rank → recommend → confirm)
---

# /buy-search — find something to buy

Thin entry point into the channel-agnostic discovery flow. Understands what you want, searches your
enabled marketplaces, ranks + recommends the best matches, asks your price range, and hands the
chosen listing(s) to the buyer agent loop to negotiate.

→ Execute **`skills/buying/search.md`**. Apply `skills/voice.md` to every message
(no em-dashes; ack before the slow search step).

Prerequisites:
- Onboarding done (`data/buyer_config.json` exists). If not, run `skills/channel/onboarding.md` first.
- Logged in to the target marketplaces in your real Chrome (Claude in Chrome).

Notes:
- The price range is asked **after** the shortlist is surfaced. Your **max** is secret — written only
  to `data/budgets/<want_id>.json` (read only by `bin/budget_gate.py` / `bin/buyer_negotiate.py`),
  never to the want record, a message, or a prompt. The buyer-safe want lives in `data/wants/<id>.json`.
- Pass a `want_id` arg to re-open and refine an existing search instead of starting a new one.
- Read-only browsing: searching never messages a seller — the liaison step (after you confirm) opens
  the thread and negotiates.
- Honors `--dry-run` (browser actions logged, not executed; all files still written).
- This is also reachable as a `/search` command over the buyer channel via `/selly-run`.
