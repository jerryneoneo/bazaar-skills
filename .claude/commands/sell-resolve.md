---
description: Answer escalated buyer questions, grow the Q&A bank, and resume the thread
---

# /sell-resolve — clear the escalation queue (console fallback)

> **When to use this.** With a channel adapter (Telegram), escalations are handled
> interactively over the channel by `skills/channel/notifications.md` — you don't need this.
> `/sell-resolve` is the **console-only fallback** for deployments with no channel adapter.

When the buyer loop can't answer something (an unknown question, or — in `assist`
mode — a price offer), it posts a holding reply and parks the thread as
`escalated`. This command lets the seller answer those, send the reply, and **teach
the agent** so it auto-answers next time.

Read `skills/reply-pipeline.md`, `skills/browser-actions.md`, and `skills/voice.md` first.

## Step 1 — Show the open escalations
Read `data/escalations.jsonl`; list every record with `status:"open"`:
the buyer handle, the item, the open question, and the 1-line context. If none,
say so and stop.

## Step 2 — Get the seller's answer
For each open escalation, ask the seller how to respond.
- For an unknown **question**: capture their answer (a fact about the item).
- For an escalated **price offer** (assist mode): ask the seller to accept / counter
  (give a number) / decline. The seller decides — the floor gate is not used here.

## Step 3 — Send the reply
Send the seller's answer to the buyer via the reply pipeline (compose → pace → send
→ persist + advance cursor). Reply naturally, no identity line (`skills/voice.md` Rule 3).

## Step 4 — Teach the bank (compounding)
For resolved **questions**, append the new Q&A to `data/qa_bank.jsonl`:
`{"item_id":"<id or *>","q":"<the question>","a":"<seller's answer>","tags":[...],"source":"escalation","added_at":"<today>"}`
Use `"*"` if it's generally applicable. Next time a similar question arrives,
`/sell-watch` answers it automatically.

## Step 5 — Reopen the thread
Set the escalation record `status:"resolved"` (rewrite the line in
`data/escalations.jsonl`) and flip the thread `status` back to `"active"` so
`/sell-watch` resumes handling it.

## Step 6 — Summary
Report: how many resolved, how many Q&A entries added, any still open.

## Guardrails
- Never fabricate an answer on the seller's behalf — if they don't know, leave it open.
- Floor stays out of everything here too; seller-decided prices are entered directly.
