---
description: SELLY — resume the agent, applying any corrections you left while paused
---

# /resume — continue, applying your corrections first

Clears the pause flag and applies anything you said while paused before normal work continues.

1. Read what's queued:
   ```
   python3 bin/control.py status
   ```
2. If there are pending corrections (`applied:false`), apply them per **`skills/channel/corrections.md`**:
   resolve each correction's target, write it to the durable state the relevant pass reads (re-price
   via `bin/floor_gate.py`, set a thread `status:"held"`, adjust a budget via `data/budgets/`, etc.),
   then `python3 bin/control.py mark-applied <id> ...`. Acknowledge each change in one short line.
3. Clear the pause:
   ```
   python3 bin/control.py resume --source claude-code
   ```
4. Tell me you've resumed and what you applied, e.g. "▶️ Resumed. Re-priced the camera to $80 and
   left that buyer alone."

The always-on daemon picks this up on its next tick: because its cadence timers were frozen while
paused, the buyer/buy/maint passes become due immediately and catch up — now reading the corrected
state. Corrections are applied exactly once (`mark-applied`), so a daemon restart mid-resume can't
double-apply a price change or re-hold a thread.
