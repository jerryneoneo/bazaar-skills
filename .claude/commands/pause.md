---
description: SELLY — pause the agent mid-flight (stop acting; queue corrections)
---

# /pause — stop the agent so you can correct it

Sets the single pause flag (`data/control.json`, owner `bin/control.py`). This is the same flag the
Telegram `/pause` and `python3 bin/control.py pause` write — one source of truth, every interface.

Run:
```
python3 bin/control.py pause --source claude-code
python3 bin/control.py status
```
Then tell me you've paused, e.g. "⏸ Paused. I've stopped acting. Tell me what to fix, then /resume."

What pausing does (the always-on daemon honors all of it on its next tick):
- **Holds every action pass** (seller / buyer / buy / maint) between passes, and **interrupts any
  pass already running** within ~one poll cadence (the killed step is idempotent — cursors + pacing
  make it safe to re-run, and it won't until you resume).
- A **PreToolUse hook** blocks marketplace sends even inside an interactive `/sell` or `/buy` session.
- Any free text you send while paused is captured as a **correction** and applied on `/resume`
  (`skills/channel/corrections.md`) — re-price a listing, hold a thread, raise a budget, etc.

**Daemon vs interactive (the one gotcha):** if the always-on daemon is running, this pauses *it* —
the remote-control case. If you're inside a foreground `/sell` / `/buy` console session, there is no
background loop to halt, so "pause" just means you stop typing; the hook still blocks any send. A
pause survives a daemon restart because it is a file.

Clear it with `/resume`.
