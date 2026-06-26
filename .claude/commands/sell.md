---
description: Drive the seller agent interactively in this Claude Code session (console adapter)
---

# /sell — at-desk seller console

Run the seller agent **right here in Claude Code**, talking to you in the session. The UI streams
everything (thinking, tool calls, replies), so there's no typing/ack plumbing — this is the
"visibility for free" front-end. Same flows + engines as the Telegram bot; only the I/O differs.

**Before you start:** if the always-on daemon is running, **pause it** so you don't have two
things driving the browser/Telegram at once (single consumer + run-lock):
`launchd/install_daemon.sh uninstall` (re-`install` when done). See `DAEMON.md`.

## How to run
Apply `skills/voice.md` to every message (no em-dashes; ack before any slow step).
Bind the SellerChannel verbs to the **console adapter** (`skills/channel/channel.md` → console):
`say`=reply to me · `ask`=ask me, my next message is the answer · `ask_images`=I'll paste local
image paths · `confirm`=ask y/n · `notify`=tell me inline with the choices.

**This is an attended session — send fast.** You're driving in the console with me watching, so at
`skills/reply-pipeline.md` step 5 pass `--mode interactive` to every `pacing_gate.py reserve`. Sends
then go out in a few seconds instead of the unattended daemon's longer jitter. The hourly cap and
quiet hours are unchanged.

Then:
1. If `data/seller_config.json` is missing → run `skills/channel/intro.md` → `onboarding.md`.
2. Otherwise act on what I ask:
   - **List an item** (I'll give photos/paths) → `skills/channel/listing.md` (turn-based, but
     here it's just a normal conversation — ask me price/floor/size as we go, then publish via
     `skills/listing-flows/{fb,carousell}.md`).
   - **Handle buyers** → run a buyer pass per `.claude/commands/sell-watch.md` (reply/negotiate
     via `skills/reply-pipeline.md` + `bin/negotiate.py`).
   - **Escalations / confirm a bid / mark sold** → `skills/channel/notifications.md`.
   - **`/status`** → summarize live items, open negotiations, reservations.

## Same guardrails as everywhere
- Floor only in `data/floors/<id>.json`; never shown or stated.
- Above-list offers → **bidding → confirm with me first** (never auto-commit). Record offers only
  from the buyer's real words (no invented numbers).
- Ship-only; account-safety pacing on browser actions; human confirms before publishing.
