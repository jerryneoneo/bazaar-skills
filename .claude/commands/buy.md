---
description: Drive the buyer agent interactively in this Claude Code session (console adapter)
---

# /buy — at-desk buyer console

Run the buyer agent **right here in Claude Code**, talking to you in the session. The UI streams
everything (thinking, tool calls, replies), so there's no typing/ack plumbing — this is the
"visibility for free" front-end. Same flows + engines as the Telegram bot; only the I/O differs.

**Before you start:** if the always-on daemon is running, **pause it** so you don't have two things
driving the browser/Telegram at once (single consumer + run-lock):
`launchd/install_daemon.sh uninstall` (re-`install` when done). See `DAEMON.md`.

## How to run
Apply `skills/voice.md` to every message (no em-dashes; ack before any slow step).
Bind the channel verbs to the **console adapter** (`skills/channel/channel.md` → console):
`say`=reply to me · `ask`=ask me, my next message is the answer · `confirm`=ask y/n ·
`notify`=tell me inline with the choices.

**This is an attended session — send fast.** You're driving in the console with me watching, so at
`skills/buying/liaison-pipeline.md` step 6 pass `--mode interactive` to every `pacing_gate.py
reserve`. Sends then go out in a few seconds instead of the unattended daemon's longer jitter. The
hourly cap and quiet hours are unchanged.

Then:
1. If `data/buyer_config.json` is missing → run `skills/channel/intro.md` → `onboarding.md`.
2. Otherwise act on what I ask:
   - **Find something to buy** (I'll describe it) → `skills/buying/search.md` (turn-based, but here
     it's just a normal conversation — search my enabled marketplaces, show a shortlist, ask my
     price range, let me pick).
   - **Negotiate / liaise the chosen one(s)** → run a buy pass per `.claude/commands/buy-run.md`
     (open the thread, offer + negotiate via `skills/buying/liaison-pipeline.md` + `bin/buyer_negotiate.py`,
     then coordinate the handover via `skills/buying/handover.md`).
   - **Escalations** (seller asks something I can't answer, or a scam-shaped request) →
     `skills/channel/notifications.md`.
   - **`/status`** → summarize active wants, shortlists, open seller threads, and any struck deals.

## Same guardrails as everywhere
- Max budget only in `data/budgets/<want_id>.json`; never shown or stated to a seller (a "what's your
  max?" gets the current standing offer, not the ceiling).
- The engine never offers above the max — it climbs to `walk_away` instead. Record seller prices only
  from their real words (no invented numbers).
- No money moves in this build: the agent coordinates payment + handover, **I pay and collect.**
- Ship-only; account-safety pacing on browser actions.
