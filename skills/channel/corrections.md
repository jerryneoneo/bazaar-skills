# Corrections — apply the user's pause-time steering, then resume

When the user pauses the agent mid-flight (`/pause` on any channel, a `/pause` slash command, or
`python3 bin/control.py pause`), every free-text message they then send is captured **deterministically**
as a *correction* in `data/control.json → corrections[]` (by `bin/channel_control.py`, no LLM). This
recipe is how those corrections get **applied** on `/resume`, before any normal work continues.

Why this exists: the buyer / buy / maint passes read **thread / want / item state**, not the control-
channel transcript. So a correction like "stop replying to that buyer" or "list it at $80 not $60" must
be written into the durable file the relevant pass reads next — not just acknowledged in chat. This is
state-routed correction, the only kind a transcript-blind background pass will honor.

## When to run
On the channel pass, **immediately after** `/resume` clears the pause (and before you start or resume
any flow). Trigger:
```
python3 bin/control.py status            # read paused + the corrections queue
```
If there are no `applied:false` corrections, skip this recipe. Otherwise apply each, oldest first.

## Apply one correction
For each pending correction `{id, text, target, source, ts}`:

1. **Resolve the target.** Use the explicit `target` hint when present
   (`{scope: thread|want|item|session|global, ref}`, set by `channel_control.py` when the text named a
   known id). When `target` is null, infer it from the RECENT CONTROL-CHANNEL CONVERSATION block and the
   active session: "that buyer" → the most-recent buyer/seller thread you were handling; "$80 not $60" →
   the listing/want in context. If you genuinely cannot resolve it, treat it as a `global` steering note
   (step 2d) — never guess at a price or a thread.

2. **Write the durable state the relevant pass reads:**
   - **a. Re-price a listing** ("list it at $80 not $60", "drop it to $X"): reuse the listing.md
     "change price → update floor/item, re-anchor, resume at PUBLISH" recipe. Update
     `data/items/<item_id>.list_price`, and write the floor via **`bin/floor_gate.py`** (the floor lives
     ONLY in `data/floors/<item_id>.json` — NEVER echo a floor number into chat or the transcript).
     If the item is already live, re-anchor and re-publish per the listing flow's PUBLISH step.
   - **b. Hold a thread** ("stop replying to that buyer / seller", "leave that chat alone"): set
     `status:"held"` in `data/threads/<market>:<id>.json` (sell side) or
     `data/buyer_threads/<market>:<id>.json` (buy side). Both loops in `.claude/commands/selly-run.md`
     skip `held` threads. `held` is **reversible** (unlike `escalated`) — a later "resume that chat"
     correction clears it back to its prior status.
   - **c. Re-scope a want / budget** ("raise the budget on <want>", "stop pursuing <want>"): adjust the
     want via its file; a budget change goes ONLY to `data/budgets/<want_id>.json` (read by
     `bin/budget_gate.py` / `bin/buyer_negotiate.py`) — never into the transcript. "Stop pursuing" → set
     the want's threads to `held`, or the want `status` to a terminal value the buy loop skips.
   - **d. General steering** with no concrete state target ("be more terse", "always ask before
     offering"): leave it in the transcript (the channel pass already reads the tail) so it shapes the
     next channel turn. If it implies a config change (e.g. an approvals/autonomy preference), point the
     user at `/selly` rather than silently rewriting `data/config.json`.
   - **e. Style / persona steering** ("be more terse", "give lowballers a harder time", "stop being so
     soft", "stand your ground more"): this is a durable preference, not a one-off, so record it as a
     **style proposal** instead of letting it die in the transcript. Map it to the closest field and run
     `python3 bin/style.py propose --field <voice.tone|voice.humor|voice.lowball_response|
     negotiation.sell_firmness|learning> --value <v> --rationale "<the user's words>" --source correction`
     (`bin/style.py` is the single source of truth and skips silently when `learning:"off"`). It does
     **not** rewrite the persona; tell the user you noted it and that they can apply it under
     `/selly -> style`. If `style.json learning` is `auto` and the mapping is unambiguous, you may
     apply it now via `python3 bin/style.py apply --id <id>` and say what you changed. Never hand-edit
     `data/style.json` directly here, and never let a "troll harder" note cross into abusive wording
     (the `skills/style.md` invariants hold).

3. **Mark it applied** (exactly-once — re-pricing is NOT naturally idempotent, so a retried resume pass
   must not double-apply):
   ```
   python3 bin/control.py mark-applied <id> [<id> ...]
   ```

4. **Acknowledge** what you changed in one short line per correction
   (e.g. "Done, re-priced the camera to $80 and updated the floor." / "Got it, I'll leave that buyer
   alone."). Per `skills/voice.md`: no em-dashes, no fixed templates.

## After applying
Resume normal work. Because the daemon froze its cadence timers while paused, the buyer/buy/maint passes
become due immediately on the next ticks and catch up — now reading the corrected state (new price, held
threads). Idempotency holds: thread cursors mean only past-cursor messages are reprocessed, and a held
thread is skipped before the loop reads it.

## Invariants
- **Never leak a secret.** Floors and budgets are written ONLY to `data/floors/` / `data/budgets/` via
  their gates; a correction that names a number updates the file, it never echoes the floor/budget back.
- **State-routed, not prompt-routed.** A correction that targets a thread/want/item MUST land in that
  file; acknowledging it in chat alone does nothing for the transcript-blind background passes.
- **Exactly-once.** Always `mark-applied` after writing state, so a daemon restart mid-resume can't
  re-apply a price change or re-hold a thread.
