#!/usr/bin/env python3
"""harness_run.py — invoke ONE headless pass through the harness seam (no hardcoded CLI).

This is the agnostic replacement for the old Claude-only run_pass.sh/intent.sh bodies. It builds a
harness-agnostic PassSpec per mode, asks the active harness (`$SELLY_HARNESS`, default claude-code)
to translate it into argv + env via `Harness.pass_argv`, then runs it.

  harness_run.py channel         → drain the control channel, BOTH sides (selly-run §1). Alias: `seller`.
  harness_run.py buyer           → one SELL-inbox watch pass (buyers messaging the seller; §2)
  harness_run.py buy             → one BUY-side step: search/liaise a want like an iPhone (§3)
  harness_run.py maint           → one cross-listing step: drain distribution / cadence-scan (§2b)
  harness_run.py intent "<msg>"  → ONE short MCP-less "what I'll do next" line (printed to stdout)

These mirror the four phases of .claude/commands/selly-run.md so the always-on daemon drives the
whole agent, not just the channel + sell-inbox. (`buyer` = the seller's inbox; `buy` = acquiring for
the user — distinct, despite the close names.)

Runtime scope: only `claude-code` is wired + verified today. For any other harness the seam exists
(see bin/harnesses/) but the runner refuses, rather than launching a half-supported pass. Add a
harness by implementing + verifying its `pass_argv` and dropping the guard below.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  crash-safe (tmp + os.replace) write for the cap-hit breadcrumb
from harnesses import UnknownHarness, get_harness  # noqa: E402
from harnesses.base import PassSpec  # noqa: E402

try:
    import channel_log  # noqa: E402  short-term memory: tail injected into the channel pass
except ImportError:
    channel_log = None

try:
    import thread_outbox  # noqa: E402  the intent log — the completion gate peeks it after a pass
    import lease  # noqa: E402  per-market lease — guards a concurrent worker's in-flight intent
except ImportError:  # pragma: no cover - both ship in bin/; guard keeps argv-only tests importable
    thread_outbox = None
    lease = None

SELLER_DIR = Path(__file__).resolve().parent.parent
LOG = SELLER_DIR / "logs" / "pass.log"

# Fix C — turn-budget robustness.
# The buyer pass cap is a two-tier design: a SOFT budget the prompt self-governs against (stop
# opening new threads near it) and a HARD backstop the harness enforces ABOVE it. Raising the cap
# ALONE failed ~82% (eval): the pass just toured more inboxes. The soft budget is the real fix; the
# raised backstop only gives a clean stop more headroom before the kill.
BUYER_SOFT_TURNS_DEFAULT = 30      # $SELLY_BUYER_SOFT_TURNS — the self-governed soft budget
BUYER_BACKSTOP_TURNS = 50          # the hard --max-turns the harness passes (was 40)
# When a pass is killed at the hard cap (rc!=0 AND the marker below in its log tail), run_pass returns
# this DISTINCT code so callers can tell "capped, more work pending" from a generic failure (rc=1).
CAP_HIT_SIGNAL = 42
# Track A3 — completion gate. A gated pass (one that can send a marketplace message) must not END
# owning a never-fired send. After the pass exits, run_pass peeks the thread_outbox for PENDING
# (status=pending) intents in this pass's scope; if any remain un-fired it returns this DISTINCT code
# so the daemon schedules a prioritized re-drive (the send is journaled but the browser send never
# fired — the vida silent drop). sent_unverified intents are NOT flagged here: the send fired, so the
# journal_reconcile verify path owns them, not the re-drive path.
REDRIVE_SIGNAL = 43
# Passes that can send to a marketplace (so a stranded never-fired send is possible). `maint` is a
# background pass that sends only one-way completion notices (no inbound replies), so it is not gated.
GATED_MODES = ("channel", "seller", "buyer", "buy")
# Match the kill marker the harness writes to the log; keep this in lock-step with eval_checks'
# MAX_TURNS_RE so a CLI-wording drift is a single-place fix.
MAX_TURNS_MARKER = re.compile(r"reached max turns", re.IGNORECASE)
# How much of THIS pass's per-pass output file to scan for the kill marker (C1/C5). The marker is
# always at the tail (it is the last thing the harness writes before exiting), so we read only the
# final window rather than loading a runaway pass's whole transcript into memory. Used by _read_tail.
LOG_TAIL_BYTES = 8192

# Bug C6 — stale per-pass-log sweep. run_pass writes the claude subprocess output to a per-pass file
# (logs/pass-<mode>-<resource>-<pid>.log) and unlinks it in its `finally`. But a FORCED kill of the
# pass tree (SIGTERM/SIGKILL from supervisor._preempt_all, the agent_daemon force-break on a
# pause/seller-message interrupt, the 900s deadline, or the _reap MAX_WORKER_SEC watchdog) skips
# Python's finally entirely, orphaning the file. Forced kills are COMMON, so without a sweep logs/
# grows unbounded. sweep_stale_pass_logs() is the deterministic backstop: glob the per-pass files and
# unlink any whose mtime is older than this many minutes (a live pass owns a FRESH file, so it is
# spared). Run at daemon/supervisor startup. The cutoff sits well above any single pass's worst-case
# wall-clock (the 900s deadline) so an in-flight pass's file is never swept out from under it.
STALE_PASS_LOG_AGE_MIN = 60

# Runtime harnesses verified end-to-end today. The install/config layer is broader; the *runner* is
# deliberately conservative so an unverified harness can't strand the daemon (ARCHITECTURE.md §2).
SUPPORTED_RUNTIME = {"claude-code"}

# Non-browser tools every pass needs: deterministic money/shipping scripts + web research + reads.
BASE_TOOLS = ("Bash(python3:*)", "WebSearch", "WebFetch", "Read", "Glob", "Grep")

# Browser tool surface is PER MODE — the buyer pass ships a smaller set than the seller pass, since
# every tool schema is re-sent each turn (cost). Suffixes map to mcp__playwright__browser_<suffix>.
SELLER_BROWSER = ("navigate", "navigate_back", "click", "type", "fill_form", "select_option",
                  "press_key", "hover", "file_upload", "snapshot", "take_screenshot", "wait_for",
                  "tabs", "handle_dialog", "evaluate", "run_code_unsafe")
# `tabs` is included: the buyer pass drives the warm Chrome across several marketplace inbox tabs
# (FB, Carousell, …) and uses tab listing/switching to avoid re-snapshotting whole pages. Without
# it the headless pass stalls asking for approval and burns its turn cap.
# `evaluate` is included: FB Marketplace's **Selling** inbox rows resist plain snapshot+click (they
# carry no stable ref/href — see skills/listing-flows/fb.md), so the pass needs JS to click a row and
# to read message text from aria-labels. Without it the buyer pass detects FB unread but replies to
# none (verified). Carousell/eBay read fine without it; it's here for the FB Selling path.
BUYER_BROWSER = ("navigate", "navigate_back", "click", "type", "fill_form", "select_option",
                 "press_key", "wait_for", "snapshot", "tabs", "handle_dialog", "evaluate")

CHANNEL_PROMPT = """You are the SELLY agent on the control channel, headless and unattended. Be
RESPONSIVE — send a short progress message at each step; never go silent on a long task. The system
may have already sent a GENERIC one-line intent ack (e.g. 'Let me check your listings…') and a
native 'typing…' indicator is showing. That generic line does NOT replace a flow's own ack: still
OPEN each command/flow with its short, contextual acknowledgement BEFORE any slow step (e.g. the
listing photo ack), then send PROGRESS + RESULTS as you go. 'Don't repeat' means don't send a
SECOND generic 'let me check' one-liner — it never means do the work in silence; always respond to
the user before going off to work. Fire 'telegram.py typing' right before each message you send.

Do ONE turn of the CONTROL CHANNEL phase (§1 of .claude/commands/selly-run.md) — BOTH sell and
buy — then stop:
0a. PAUSE CHECK — run `python3 bin/control.py status`. If paused: take NO marketplace action this
   pass (the daemon's deterministic drain normally handles the paused channel; if you are running,
   just acknowledge + stop). If NOT paused but there are pending corrections (applied:false), run
   skills/channel/corrections.md FIRST to apply them to durable state, then continue normal work.
0. THEN read data/listing_session.json, data/distribution_session.json, data/buy_session.json AND
   data/catchup_session.json. If ANY is active you are MID-FLOW: apply the user's latest reply to
   THAT session's current step.
   NEVER re-ask which item/want it is — the session knows. Only treat a message as NEW when no
   session is active. If a RECENT CONTROL-CHANNEL CONVERSATION block is present below, READ it: it
   is what you and the user just said. A no-session message like 'do all', 'do all tasks', 'take
   over all', 'both', 'yes', 'go ahead', 'auto', 'the first one'/'#2' is a FOLLOW-UP to your last
   [out] turn (especially an 'enumerated-tasks' turn) — resolve it against that turn and ACT; do
   NOT fall back to 'let me check what needs doing'.
1. run `python3 bin/telegram.py poll` (download photos with `bin/telegram.py getfile`).
2. Route each event in order:
   - mid-flow reply → feed it to the active session (listing/distribution → listing.md /
     distribution.md; buy → skills/buying/search.md or the buying answer handlers; catchup →
     skills/selly-catchup.md, applying the reply to catchup_session.json's current step — NEVER
     re-sweep or re-ask what the digest already found).
   - FRESH message (no active session) → run the §1 FRESH-MESSAGE INTENT GATE, which now resolves a
     no-signal FOLLOW-UP (like 'do all'/'yes'/'the first one'/'take over all') against the RECENT
     CONTROL-CHANNEL CONVERSATION block instead of bouncing to 'let me check'. Otherwise classify
     sell vs buy from the words (photo + buy-intent → BUY), and start AT MOST ONE flow on an
     AVAILABLE side (seller_config present → sell; buyer_config present → buy). Never start both; if
     the chosen side isn't set up, say so and start nothing.
   - commands: /list → skills/channel/listing.md ; /detect → skills/channel/distribution.md ;
     /delist → skills/channel/delist.md (seller-initiated take-down of a LIVE listing; also matches
       free-text "delete/remove/take down my <item> listing" — resolve the item by id, run each
       platform's take-down recipe, then `python3 bin/delist_item.py <item_id>` writes the durable
       data/items/<id>.json to removed_by_seller. NEVER write the deletion to listing_session.json.) ;
     /search → skills/buying/search.md ; /onboard → onboarding.md ;
     /status → a summary (live items + open buyer threads; active wants + shortlists + open seller
     threads; escalations); if `bin/control.py status` shows paused, LEAD with "⏸ PAUSED since
     <since> (via <source>) — <N> correction(s) queued. Send /resume to continue." ;
     /catchup (also /selly-catchup) → skills/selly-catchup.md, START at HEALTH with scope:"both":
       a deep, read-only sweep of every listing + inbox + setup surface that reports ONE grouped
       digest and proposes the work (acts on nothing during the sweep). It is turn-based/resumable in
       data/catchup_session.json (one market or one question per pass), so ack the slow sweep first,
       do ONE step, and let later passes continue it — the seller's reply routes back via step 0 ;
     /pause → `python3 bin/control.py pause --source telegram`, ack "⏸ Paused", take no further
       action this pass ; /resume → `python3 bin/control.py resume --source telegram`, then run
       skills/channel/corrections.md to apply pending corrections BEFORE resuming normal work.
   - action / answer to a pending notify → skills/channel/notifications.md. This covers SELL
     escalations/bids/sale (incl. confirm-sold → bin/negotiate.py confirm-sold → take down other
     listings) AND BUY escalations/deals AND a BUY answer: when the user gives a max budget + which
     listings to pursue, write data/budgets/<want_id>.json (the max lives ONLY there), seed
     data/buyer_threads/, and set the want status=liaising.
listing_autonomy=auto_anomaly (publish without confirm; pause only on a real anomaly). Keep
account-safety pacing. ONE step per pass so the bot stays responsive."""

BUYER_PROMPT = """You are the SELLY seller agent, running headless and unattended.

FIRST STEP (before anything else): run `python3 bin/journal_reconcile.py` to heal any reply a prior
pass was INTERRUPTED on. It is a cheap non-LLM call that never re-sends; it returns JSON with TWO
lists you must resolve THIS pass (both are one-shot — reconcile won't re-surface them), BEFORE your
normal sweep:
  • `needs_verify` — the send FIRED but the commit was lost; the thread's last outbound row is marked
    `unconfirmed`. For EACH: open the live chat and check whether that reply is actually the last
    message there. If present, it sent — do nothing. If MISSING, resend that exact text via the §5
    bracket.
  • `needs_resend` — the send NEVER fired (no false row was folded, the cursor was left at the
    inbound). For EACH: open the live chat, RE-READ it, and send the reply via the §5 bracket. The
    intent is deduped by thread+inbound, so re-driving cannot strand a duplicate.
Always re-read the chat before resending so you never post a duplicate.

JOURNAL DISCIPLINE (hard rule): every reply is bracketed `journal_send.py intent` (before the send) →
`pacing_gate.py reserve --block` → `type`+`send()` → `journal_send.py mark-sent` (the instant send()
returns) → `journal_send.py commit` (immediately after), per skills/reply-pipeline.md §5. The
`mark-sent` step is REQUIRED — it is what lets recovery tell a sent-but-unjournaled reply from one
that never fired. Never advance to the next message or thread with an un-committed send, and never
hand-edit data/threads/<id>.json.

Run ONE buyer-inbox watch pass per .claude/commands/sell-watch.md over your enabled
seller_config.marketplaces: open the relevant inbox(es) (see COST DISCIPLINE for which) in the
(already-running) Chrome and handle each new buyer message past its cursor via
skills/reply-pipeline.md — auto-negotiate price offers through
`python3 bin/negotiate.py` (anti-probing + discreet cross-buyer), quote delivery via
bin/shipping.py, answer from qa_bank, escalate unknowns to the seller over Telegram
(skills/channel/notifications.md). Respect pacing/caps and per-thread cursors (idempotent).

COST DISCIPLINE (a cheap non-LLM probe already detected new activity before launching you):
$SELLY_BUYER_PEEK_TEXT hints which marketplace/snippet is new — go STRAIGHT to that
marketplace's inbox and open only thread(s) with messages past their cursor. Handle ONLY the
marketplace the peek points to; do NOT tour every marketplace in one pass (another pass picks up
the rest). Do NOT browser_snapshot whole inbox pages when a targeted thread read suffices;
snapshots are the single biggest cost per pass. If $SELLY_BUYER_PEEK_FORCED=1 this is a periodic
safety-net sweep with NO specific signal — open only the single most-recently-active inbox,
confirm nothing sits unread past its cursor, and stop; do NOT sweep all marketplaces.

SCOPE (priority hint, not a hard restriction): if $SELLY_BUYER_PEEK_THREAD is set,
PRIORITISE that thread first (read its new messages past the cursor, reply via
skills/reply-pipeline.md, commit) BEFORE touring any other thread. This is a PRIORITY HINT to put
the thread that actually has new mail first; it is not a hard 'only that thread' rule. If other
threads on this marketplace also have unread mail past their cursor, handle them too within your
budget. Never let the hint stop you replying to a real message, and never reply to a thread the hint
does not name unless that thread genuinely has new mail (mis-routing a reply onto the wrong thread is
the worst outcome).

TURN BUDGET (hard rule — your turn cap is finite and being killed mid-pass loses ALL progress and
your summary): you have a SOFT budget of about $SELLY_BUYER_SOFT_TURNS turns. As you approach
$SELLY_BUYER_SOFT_TURNS (leave yourself ~5 turns of headroom) stop opening NEW threads: journal
everything you have already sent via `python3 bin/journal_send.py commit` (per the JOURNAL DISCIPLINE
rule above — never end a pass with an un-committed send), write your one-line summary, and STOP. The
soft budget sits well below the hard cap so a clean stop almost always beats the cap. Partial
progress is fine — every reply marks
its thread read, so the next pass resumes where you left off. NEVER loop on a stuck step. To OPEN a
marketplace inbox, ALWAYS `navigate` to its inbox URL first — you drive a dedicated Chrome, so a
marketplace tab not already being open is NORMAL: `navigate` opens it. A missing tab is NEVER an
escalation and NEVER "inbox unreadable" — just navigate. Escalate over Telegram
(skills/channel/notifications.md) ONLY when, AFTER navigating, the marketplace is logged-out /
checkpoint / captcha (escalate "re-auth your <market>") or the inbox still won't render after
ONE retry — then move on. Reserve your final turn for the summary and STOP as soon as the pass is
complete."""

MAINT_PROMPT = """You are the SELLY agent doing cross-listing maintenance, headless and unattended.
This is a BACKGROUND pass — do NOT poll the control channel or buyer inboxes for INBOUND messages
(reading replies is the channel/buyer passes' job). You DO, however, send OUTBOUND completion
notifications for work you finish this pass — a `say` is a one-way push (telegram.py send) that needs
no polling, so 'quiet background pass' must NOT mean 'silently drop the success notice'. Do ONE step
of selly-run.md §2b, then stop:
0. If data/listing_session.json is active, OR data/catchup_session.json is active AND recently updated
   (a live, advancing sweep) → do nothing (never interrupt an active listing or an in-flight catch-up
   sweep — the catchup invariant is single-active-session); end. A catchup session that is active but
   STALE is a crash orphan: the daemon's deterministic reconciler clears it before this pass runs, so
   an active sweep reaching you here is genuinely live — stand down.
1. Else if data/distribution_session.json is active → CONTINUE it per skills/channel/distribution.md:
   cross-list the session's current_item_id to its target market (ONE item this pass; honor
   max_actions_per_hour pacing + quiet_hours). Record a listing URL ONLY after bin/verify_listing_url.py
   passes (read from the live page, never composed), update the item's listing_urls, advance
   current_item_id. DISTRIBUTION GATE (approvals.steps.distribution): if this batch has NOT been
   confirmed by the seller yet, send the confirm ask via skills/channel/notifications.md and STOP;
   once confirmed, auto-drain one item per pass without re-asking. Stay QUIET per item while the
   queue still has items left to cross-list (no per-item ping during a blanket-confirmed drain). When
   THIS cross-list drains the queue (no `decision=="manage"` item left to list → set session
   active=false), send the ONE end-of-batch completion summary per skills/channel/distribution.md
   "Done" (the outbound `say` described above) so the seller learns the batch finished.
2. Else if data/inbox_detect_session.json is active → CONTINUE it per skills/inbox-detect.md TAKEOVER:
   offer ONE untracked-chat group this pass under the takeover gate (hard floor = confirm) and STOP,
   or apply the user's just-arrived accept/skip to the current group. Never re-ask which chats those
   were. One group per pass.
3. Else `python3 bin/inbox_detect.py due` (most-overdue market across the UNION of enabled sell+buy
   markets; cadence config.scan_interval_hours). If a market m is due, run BOTH detectors for m ONLY,
   then `python3 bin/scan_state.py mark --market m` (one stamp covers both detectors):
     (i)  skills/channel/distribution.md SCAN — find listings made OUTSIDE SELLY (unmanaged) → queue
          to manage + cross-list under the distribution gate.
     (ii) skills/inbox-detect.md SWEEP (scope=both) — review m's chat list for threads the user started
          on their OWN that are NOT yet tracked (absent from data/threads/ and data/buyer_threads/) and
          surface a takeover offer under the takeover gate, persisting data/inbox_detect_session.json
          for TAKEOVER on a later pass. This is how a chat you started yourself reaches your channel.
   If nothing is due → fall through to step 4.
4. Else if data/listing_health_session.json is active → CONTINUE it per skills/channel/listing-health.md
   (the LOWEST-priority maint step — a stale LIVE listing with no buyer interest for 7+ days that needs
   improvement suggestions). Read the session (item_id + stale_row: silent_days, basis, last_inbound_ts,
   list_price) and load data/items/<item_id>.json. FIRST re-check status=="live": if the item is now
   sold/removed/cancelled, send NOTHING, set the session active=false, and STOP — do NOT run
   listing_health.py mark (the episode is void). Otherwise research current comps for THIS ONE item
   (WebSearch/WebFetch, at most ~2 parallel queries, no browser comps), compose CONCRETE improvement
   suggestions (price vs comps, photos, title/description, reach/distribution, bump/relist — include
   only the ones that genuinely apply; do NOT suggest a price drop if already at/below comps), send ONE
   control-channel message (skills/channel/notifications.md notify, ref=item_id, voice per
   skills/style.md, NO em-dashes, framed as suggestions the seller can approve — never auto-applied),
   then run `python3 bin/listing_health.py mark --item <item_id>`, set the session active=false, STOP.
   If nothing is active or due in any step → end (no work)."""

BUY_PROMPT = """You are the SELLY BUYER agent (acquiring for the user), headless and unattended.

FIRST STEP (before anything else): run `python3 bin/journal_reconcile.py` to heal any message a prior
pass was interrupted on. Cheap, non-LLM, never re-sends. Resolve its TWO one-shot lists THIS pass:
`needs_verify` (the send fired but wasn't journaled — open the live chat and resend only if the
`unconfirmed` reply is genuinely missing) and `needs_resend` (the send NEVER fired — re-read the chat
and send it via the §6 bracket; the intent is deduped so you can't strand a duplicate).

JOURNAL DISCIPLINE (hard rule): every message is bracketed `journal_send.py intent --side buy`
(before the send) → `pacing_gate.py reserve --block` → `type`+`send()` → `journal_send.py mark-sent`
(the instant send() returns) → `journal_send.py commit --side buy` (immediately after), per
skills/buying/liaison-pipeline.md §6. The `mark-sent` step is REQUIRED. Never advance to the next
message or thread with an un-committed send, and never hand-edit data/buyer_threads/<id>.json.

Do ONE buy-side step of selly-run.md §3 + .claude/commands/buy-run.md, then stop. $SELLY_BUY_PEEK_WANT
names the actionable want; $SELLY_BUY_PEEK_TEXT is a hint. Load data/buyer_config.json.

For the actionable want (data/wants/<id>.json):
- status 'searching' → run skills/buying/search.md: search each enabled buy market
  (buyer_config.marketplaces) via skills/search-flows/*, de-dupe + verify URLs + rank, then SEND the
  ranked shortlist and ASK the user over the channel for (a) max budget and (b) which to pursue.
  AUTO-SEARCH: do NOT wait for any 'search now' confirmation (ignore a stale awaiting_search_confirm).
  Persist progress in data/buy_session.json and STOP after asking — the user's answer arrives on the
  control channel (handled by the channel pass).
- status 'liaising'/'agreed' → for each thread in the want: open data/buyer_threads/<thread>.json
  (skip closed/escalated); if no outbound message yet → skills/buying/liaison-pipeline.md INITIATE
  (opening offer via bin/buyer_negotiate.py, capped under the secret max); else handle each new seller
  message past the cursor → liaison-pipeline.md (classify → bin/buyer_negotiate.py → compose → pace →
  persist). Struck deal → skills/buying/handover.md → channel.notify(buy_deal); scam/unanswerable →
  ESCALATE to the user.
The max budget lives ONLY in data/budgets/<want_id>.json (read by bin/budget_gate.py /
bin/buyer_negotiate.py) — NEVER put a number in a walk-away ('a bit more than I can do', no figure).
Respect pacing + per-thread cursors (idempotent). ONE step per pass."""

# FOLLOW-UP BRANCH — appended to the buyer/buy prompts (byte-stable, so the 1h prompt cache is
# unaffected). It only ACTS when $SELLY_FOLLOWUP=1 (the daemon sets it when followup_state.py reports
# due nudges); otherwise the model ignores it and runs the normal inbound body. A nudge is the SAME
# action the pass already performs (open a tracked thread, compose, pace, journal bracket) — only the
# trigger differs, so no new pass mode / tool surface is needed. The follow-up COUNT is derived from
# the transcript tail by followup_state.py, so no special commit tagging is required here.
FOLLOWUP_BRANCH_SELL = """

FOLLOW-UP MODE (only when $SELLY_FOLLOWUP=1; otherwise IGNORE this whole section): some buyers went
quiet after our last message and are due a gentle nudge. FIRST run `python3 bin/followup_state.py due`
and read `due_nudges` (rows: thread_id, marketplace, side, nudges_sent). Handle ONLY rows on THIS
pass's marketplace. For each such thread: OPEN it and RE-READ its tail. If the last transcript row is
now INBOUND (they replied since the scan), do NOT nudge — handle their reply normally via
skills/reply-pipeline.md instead. Otherwise compose ONE short, friendly nudge (no em-dashes per
skills/style.md; do NOT re-introduce yourself; nudge #1 lighter than #2, e.g. a soft 'just checking
in' first, then a final 'still keen? no worries either way'). Send it bracketed EXACTLY like any reply
(reply-pipeline.md §5): journal_send.py intent -> pacing_gate.py reserve --block -> type+send ->
journal_send.py mark-sent -> journal_send.py commit. AFTER a successful commit run `python3
bin/followup_state.py mark-nudge --thread <thread_id>
--side sell`. If pacing returns wait/quiet, do NOT send and do NOT mark (it retries next interval).
One nudge per thread per pass; never nudge a thread whose tail is inbound."""

FOLLOWUP_BRANCH_BUY = """

FOLLOW-UP MODE (only when $SELLY_FOLLOWUP=1; otherwise IGNORE this whole section): some sellers went
quiet after our last message and are due a gentle nudge. FIRST run `python3 bin/followup_state.py due`
and read `due_nudges` (rows: thread_id, marketplace, side, nudges_sent). Handle ONLY rows on THIS
pass's marketplace. For each such thread: OPEN it and RE-READ its tail. If the last transcript row is
now INBOUND (they replied since the scan), do NOT nudge — handle their reply normally via
skills/buying/liaison-pipeline.md instead. Otherwise compose ONE short, friendly nudge (no em-dashes
per skills/style.md; do NOT re-introduce yourself; nudge #1 lighter than #2). Send it bracketed
EXACTLY like any message (liaison-pipeline.md §6): journal_send.py intent --side buy -> pacing_gate.py
reserve --block -> type+send -> journal_send.py mark-sent -> journal_send.py commit --side buy. AFTER
a successful commit run `python3
bin/followup_state.py mark-nudge --thread <thread_id> --side buy`. If pacing returns wait/quiet, do
NOT send and do NOT mark (it retries next interval). One nudge per thread per pass; never nudge a
thread whose tail is inbound."""

RESEARCH_PROMPT = """You are the SELLY RESEARCH worker, running headless and DETACHED. You have NO
browser and NO channel: you cannot message the seller or touch any marketplace. Your ONLY output is a
single result file, written with the one Bash command you are allowed. Be fast and quiet.

GOAL: identify the item in a listing's photos and find its used market price, so the main agent can
present it to the seller instantly.

STEPS:
1. Read `data/listing_session.json`. Take `batch_id` and `fields.photos` (the downloaded photo paths).
   If it is missing/unreadable or has no photos, exit without writing anything (nothing to do).
2. Read the seller's region from `data/seller_config.json` (e.g. region/currency) for the price query.
3. Read EACH photo path (the Read tool shows you the image). Identify the item:
     - title (concise, what a buyer would search), category, condition (be honest),
     - attributes (brand/model/size/colour as visible),
     - category_tag — EXACTLY ONE of the fixed taxonomy in skills/marketplaces.md (drives the publish
       filter); pick the closest,
     - size_bucket — the delivery size from the item type: book/clothing=small, monitor/lamp=medium,
       large appliance=large, desk/sofa=bulky; default medium when unsure.
4. WebSearch `"<title> used price <region>"` for comps -> comp_low / comp_med / comp_high (integers in
   the seller's currency). If you run a second query (e.g. a "<title> sold price <region>" lookup for
   a tighter range), issue BOTH in ONE turn (parallel), never back-to-back.
5. Write the result with EXACTLY this command (your only allowed Bash):
     python3 bin/research_result.py --batch <batch_id> --result '<json>'
   where <json> is a JSON object:
     {"title","category","category_tag","condition","attributes","size_bucket",
      "comp_low","comp_med","comp_high","currency"}
   Then STOP. Do not message anyone (you have no way to). Do not retry after a successful write."""


# Skills folded into the cached prefix per mode (byte-stable, no volatile data) — fail-open.
# `skills/style.md` is the stable voice/persona rulebook (the volatile prefs live in data/style.json,
# read at compose time); it rides the prefix anywhere a message is composed — buyer/buy replies,
# control-channel say/ask, AND the maint stale-listing suggestions (free-form copy, so voice applies).
CORE_SKILLS = {
    "channel": ("skills/channel/notifications.md", "skills/channel/channel.md",
                "skills/channel/corrections.md", "skills/style.md"),
    "buyer": ("skills/reply-pipeline.md", "skills/channel/notifications.md", "skills/style.md"),
    "buy": ("skills/buying/liaison-pipeline.md", "skills/channel/notifications.md",
            "skills/style.md"),
    # maint now composes free-form stale-listing suggestions (not just a fixed completion notice), so
    # style.md (voice, NO em-dashes) + the listing-health skill ride the prefix too.
    "maint": ("skills/channel/notifications.md", "skills/channel/listing-health.md", "skills/style.md"),
    # research is a quiet background worker: it only needs the marketplaces taxonomy (for category_tag).
    # No channel/voice skills — it never messages.
    "research": ("skills/marketplaces.md",),
}

# NOTE: the old PAUSE_LINE (a per-pass "Paused — holding here" narration) was REMOVED — it was the
# source of unbounded duplicate pause acks (every concurrent worker / poll cycle emitted its own).
# Pause is now enforced deterministically without it: the PreToolUse hook (bin/hooks/pause_guard.py)
# denies the pacing reserve + browser mutations while paused, the supervisor preempts live workers
# and the single-flight loop interrupts a running pass, and the ONE confirmation is sent by the
# non-LLM drain (bin/channel_control.py, gated by control.claim_pause_ack — exactly once per episode).


def _browser_tools(suffixes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"mcp__playwright__browser_{s}" for s in suffixes)


def _core_skills_block(mode: str) -> str | None:
    """Concatenate the per-mode core skills into one block. Fail-open: a missing file just shrinks
    the block (None if nothing readable) rather than aborting the pass."""
    parts = []
    for rel in CORE_SKILLS.get(mode, ()):
        try:
            parts.append((SELLER_DIR / rel).read_text())
        except OSError:
            continue
    return "\n".join(parts) if parts else None


def _scope_prefix(resource: str) -> str:
    """Phase-3 per-marketplace scoping. When the supervisor runs concurrent workers it scopes each
    to ONE marketplace; this prefix (in the uncached `-p` prompt, so the cached skills prefix stays
    byte-stable) narrows the pass and pins it to its own browser tab. Empty resource → no prefix, so
    an UNSCOPED pass is byte-identical to the pre-Phase-3 behavior (single worker handles all markets)."""
    if not resource:
        return ""
    return (
        f"SCOPE — this pass handles ONLY the marketplace '{resource}'. Another worker owns each other "
        f"marketplace, so IGNORE every marketplace except '{resource}' (this OVERRIDES any 'for each "
        f"marketplace' instruction below). Drive ONLY this marketplace's tab in the warm Chrome: run "
        f"`python3 bin/tab_registry.py resolve --market {resource}` for its host/url_prefix, find the "
        f"open tab whose URL starts with that (browser_tabs) and select it, and act ONLY on that tab "
        f"— never open or close tabs, never touch another marketplace's tab.\n\n"
    )


def build_spec(mode: str, msg: str = "", resource: str = "") -> PassSpec:
    if mode in ("channel", "seller"):  # `seller` kept as the daemon-facing alias
        # The combined awaiting_listing_inputs step writes the floor + item and runs the publish
        # loop, so the channel pass always uses the strong default model + full turn budget. The old
        # per-step fast/8-turn cap was REMOVED: it silently crashed when a seller over-answered a
        # wizard step (e.g. giving price + floor together) — the reply landed with no response.
        model, max_turns = (None, None)
        tail_turns = channel_log.DEFAULT_MAX_TURNS if channel_log else 0
        # The recent-conversation tail goes in the (uncached) `-p` message ONLY — never in
        # system_prompt_append, which is the 1h-cached, byte-stable prefix. So a follow-up like
        # "do all tasks" resolves against what was just said, with ~zero cache impact.
        prompt = CHANNEL_PROMPT
        if channel_log:
            tail = channel_log.render_tail(max_turns=tail_turns)
            if tail:
                prompt = f"{CHANNEL_PROMPT}\n\n{tail}"
        return PassSpec(
            prompt=prompt, model=model, max_turns=max_turns,
            allowed_tools=BASE_TOOLS + _browser_tools(SELLER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("channel"),
            prompt_cache_1h=True,
        )
    if mode == "buyer":
        # Sell-inbox work is mechanical (money decisions live in negotiate.py/shipping.py) → Sonnet
        # is plenty and far cheaper. BUYER_BACKSTOP_TURNS (50) is a BACKSTOP, not a workload bound:
        # eval found the buyer pass exhausting whatever cap it was given (14→28→40 all failed ~82% of
        # the time) by touring every inbox and looping on stuck navigation — so progress comes from
        # BUYER_PROMPT's TURN BUDGET governor (a SOFT budget of $SELLY_BUYER_SOFT_TURNS at which it
        # stops opening new threads, journals, and summarises; the peek-thread priority hint; one
        # retry then escalate), NOT from this number. The backstop is just above the soft budget so a
        # clean stop has headroom; a cap-hit is now RARE and NON-FATAL (run_pass detects it → Fix C
        # continuation). Don't bump it chasing rc=1; replies mark threads read, so partial progress
        # carries across passes.
        return PassSpec(
            prompt=_scope_prefix(resource) + BUYER_PROMPT + FOLLOWUP_BRANCH_SELL,
            model="sonnet", max_turns=BUYER_BACKSTOP_TURNS,
            allowed_tools=BASE_TOOLS + _browser_tools(BUYER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("buyer"),
            prompt_cache_1h=True,
        )
    if mode == "buy":
        # Buy-side liaison/search — same mechanical profile as the sell inbox (Sonnet + turn cap);
        # money lives in buyer_negotiate.py/budget_gate.py. Reuses the smaller buyer browser set.
        # Same backlog rationale as the buyer pass: 14 was too low and stranded liaison work (rc=1).
        return PassSpec(
            prompt=_scope_prefix(resource) + BUY_PROMPT + FOLLOWUP_BRANCH_BUY,
            model="sonnet", max_turns=28,
            allowed_tools=BASE_TOOLS + _browser_tools(BUYER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("buy"),
            prompt_cache_1h=True,
        )
    if mode == "maint":
        # Cross-listing is MECHANICAL (read listing URLs, drain the queue), so right-size it to
        # sonnet instead of the strong DEFAULT — a large saving on a background pass that does not
        # need the deepest model. Gated behind SELLY_MAINT_MODEL so it reverts instantly if the
        # publish/verify path regresses: set it to "" (empty) to restore the strong DEFAULT (model
        # None → no --model flag), or to any model name to pin that. Full seller browser set stays.
        maint_model = os.environ.get("SELLY_MAINT_MODEL", "sonnet") or None
        return PassSpec(
            prompt=_scope_prefix(resource) + MAINT_PROMPT, model=maint_model, max_turns=None,
            allowed_tools=BASE_TOOLS + _browser_tools(SELLER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("maint"),
            prompt_cache_1h=True,
        )
    if mode == "research":
        # Detached BACKGROUND worker: identify the item from the listing's photos + find comps, then
        # write data/research_results/<batch_id>.json. Browser-free and channel-free by construction —
        # the ONLY Bash it can run is the result writer, so it cannot message the seller or drive the
        # live marketplace (it can only deposit findings). strict_mcp drops the browser MCP entirely.
        # Sonnet (vision quality matters for identification); bounded turns; reads photos via Read.
        return PassSpec(
            prompt=RESEARCH_PROMPT, model="sonnet", max_turns=8,
            allowed_tools=("WebSearch", "WebFetch", "Read", "Bash(python3 bin/research_result.py:*)"),
            permission_mode="acceptEdits", strict_mcp=True, mcp_servers={},
            system_prompt_append=_core_skills_block("research"))
    if mode == "intent":
        prompt = (
            'You are a friendly marketplace seller assistant. The seller just messaged you:\n'
            f'"{msg}"\n'
            'Reply with ONE short line (≤10 words) saying what you\'ll do NEXT — an intent, not an '
            'answer.\nExamples: "Let me check your listings…" / "Let me get that listed for you…" /\n'
            '"Let me pull up the prices…" / "Let me take a look at those photos…".\n'
            'Output ONLY that line, no quotes, no extra text.'
        )
        # MCP-less, single-turn, fast model → ~5-8s.
        return PassSpec(prompt=prompt, model="haiku", max_turns=1, strict_mcp=True, mcp_servers={})
    if mode == "eval":
        # Offline LLM-as-judge over eval records (bin/eval_judge.py). MCP-less + single-turn so it
        # carries no browser tools and can't act on the world; sonnet for nuance. Invoked as a library
        # by eval_judge (not via run_pass), so it stays out of PASS_MODES; the daemon runs it on the
        # nightly eval when config.eval_judge_nightly is set, and /selly-eval always runs it.
        return PassSpec(prompt=msg, model="sonnet", max_turns=1, strict_mcp=True, mcp_servers={})
    raise ValueError(f"unknown mode: {mode}")


def _resolve_harness():
    """Active harness from $SELLY_HARNESS (default: autodetect, prefers signed-in). Refuse a
    harness whose runtime isn't verified yet — honest scope beats a broken daemon."""
    name = os.environ.get("SELLY_HARNESS") or None
    harness = get_harness(name)
    if harness.name not in SUPPORTED_RUNTIME:
        sys.stderr.write(
            f"selly: runtime not yet supported for harness '{harness.name}'. Only "
            f"{sorted(SUPPORTED_RUNTIME)} is wired today (see ARCHITECTURE.md §2).\n")
        sys.exit(3)
    return harness


def _invocation(harness, spec: PassSpec):
    inv = harness.pass_argv(spec)
    argv = list(inv.argv)
    # Honor a CLAUDE_BIN override for the claude-code binary (tests / non-standard installs).
    override = os.environ.get("CLAUDE_BIN")
    if override and harness.name == "claude-code" and argv:
        argv[0] = override
    # Let an explicit env value win over the harness default (e.g. ENABLE_PROMPT_CACHING_1H=0).
    # Mark every headless pass so the SessionStart update-notice hook NO-OPs here — the daemon can't
    # act on an interactive prompt, and it has its own channel update notice (agent_daemon.py).
    env = {**inv.env, **os.environ, "SELLY_DAEMON_PASS": "1"}
    # Fix C: make $SELLY_BUYER_SOFT_TURNS resolve in the BUYER_PROMPT. Default it ONLY when no
    # explicit value is set, so an operator/caller value (already merged via os.environ above) wins.
    env.setdefault("SELLY_BUYER_SOFT_TURNS", str(BUYER_SOFT_TURNS_DEFAULT))
    return argv, env


def sweep_stale_pass_logs(max_age_min: int = STALE_PASS_LOG_AGE_MIN) -> list[str]:
    """Bug C6 — remove leaked per-pass log files (logs/pass-*.log) older than `max_age_min` minutes.

    run_pass's `finally` unlinks the per-pass file on the happy path, but a FORCED kill of the pass
    tree skips Python finally, so a stale file is left behind on every preempt/deadline/watchdog kill.
    This sweep is the deterministic backstop — call it at daemon/supervisor startup (and optionally
    once per loop). A FRESH per-pass file (a live pass still writing to it) is younger than the cutoff
    and is spared; the human-readable logs/pass.log never matches the `pass-*.log` glob, so it is never
    touched. Fail-open throughout: a missing logs dir or an unlink race must never crash the caller.

    Returns the basenames it removed (empty on nothing-to-do / any error)."""
    removed: list[str] = []
    cutoff = time.time() - max_age_min * 60
    try:
        candidates = list(LOG.parent.glob("pass-*.log"))
    except OSError:
        return removed
    for f in candidates:
        if f == LOG:  # never the shared human-readable log (defensive; the glob already excludes it)
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed.append(f.name)
        except OSError:
            continue  # a concurrent unlink / stat race is fine — best-effort cleanup
    return removed


def _read_tail(path: Path) -> str:
    """Read a per-pass output file, bounded to the last LOG_TAIL_BYTES (the kill marker is always at
    the tail — a runaway pass can produce a large transcript, so we never load it whole just to find
    one line). Fail-open to '' — a read error never reclassifies rc."""
    try:
        with path.open("rb") as handle:
            try:
                size = os.fstat(handle.fileno()).st_size
            except OSError:
                size = 0
            if size > LOG_TAIL_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _is_cap_hit(rc: int, pass_output: str) -> bool:
    """A cap-hit = the pass exited non-zero AND THIS pass's OWN output carries the 'Reached max turns'
    marker. Keyed on BOTH so it is robust to CLI-wording drift (the marker) and never misfires on a
    success that happens to echo the phrase (the rc).

    Bug C1: `pass_output` is THIS pass's PER-PASS file content, NOT a byte slice of the shared
    logs/pass.log. With the concurrent supervisor running fb ∥ carousell passes into the same shared
    file, an offset slice [since..EOF] of the shared log would capture a CONCURRENT worker's marker
    and misclassify this pass — so each pass writes its own output to an isolated file and scans only
    that."""
    if rc == 0:
        return False
    return bool(MAX_TURNS_MARKER.search(pass_output))


def _undriven_intents(resource: str) -> list[dict]:
    """The PENDING (never-fired) intents this pass's scope still owns after it exited.

    A pending intent here means the send was journaled but the browser send never fired (the vida
    silent drop) — the owning pass has exited, so it is stranded. Scoped to the marketplace `resource`
    when set (each concurrent worker owns one market). LEASE-GUARDED: an intent whose market holds a
    live (non-stale) lease is skipped — a concurrent worker is actively driving that market, so the
    intent is in-flight (mid-pacing), not stranded. sent_unverified intents are excluded (the send
    fired; the verify path owns them). Fail-open: any error returns [] so the gate never breaks a pass."""
    if thread_outbox is None:
        return []
    try:
        pending = thread_outbox.peek(statuses=(thread_outbox.STATUS_PENDING,))["pending"]
    except (OSError, ValueError, KeyError):
        return []
    base = thread_outbox.data_dir()
    out: list[dict] = []
    for rec in pending:
        market = rec.get("market")
        if resource and market != resource:
            continue
        if market and lease is not None:
            try:
                if lease.status(base, f"market:{market}", ttl=lease.AGENT_MARKET_TTL_SEC).get("held"):
                    continue  # a live worker owns this market — in-flight, not stranded
            except (OSError, ValueError, KeyError, TypeError):
                pass  # fail-open: an unreadable lease must not hide a real stranded send
        out.append(rec)
    return out


def _record_redrive(mode: str, resource: str) -> None:
    """Drop a per-resource re-drive breadcrumb so the daemon can see a gated pass ended owning a
    never-fired send and schedule a prioritized re-drive. Atomic + fail-open (rc=43 is primary)."""
    label = f"{mode}:{resource}" if resource else mode
    try:
        atomic_io.write_json(SELLER_DIR / "data" / "pass_state" / f"{label}.redrive.json",
                             {"redrive": True, "ts": _utcnow(), "resource": resource})
    except OSError:
        pass


def _record_cap_hit(mode: str, resource: str) -> None:
    """Drop a per-resource cap-hit breadcrumb so a caller (daemon/supervisor) can see a pass was
    killed at the cap and schedule a bounded continuation. Atomic + fail-open."""
    label = f"{mode}:{resource}" if resource else mode
    # Derive from SELLER_DIR (not the frozen PASS_STATE_DIR) so tests that relocate the tree see it.
    try:
        atomic_io.write_json(SELLER_DIR / "data" / "pass_state" / f"{label}.json",
                             {"capped": True, "ts": _utcnow(), "resource": resource})
    except OSError:
        pass  # a breadcrumb is best-effort; the rc=42 signal is the primary channel


def run_pass(mode: str, resource: str = "") -> int:
    harness = _resolve_harness()
    argv, env = _invocation(harness, build_spec(mode, resource=resource))
    if resource:
        # Pass the scoped marketplace to the skills (they read $SELLY_RESOURCE to know which
        # marketplace + which tab is theirs). Empty resource → unscoped, env unchanged (legacy path).
        env = {**env, "SELLY_RESOURCE": resource}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    label = f"{mode}:{resource}" if resource else mode
    # Bug C1 — PER-PASS output isolation. The claude subprocess writes to its OWN file (keyed by
    # resource + pid so two concurrent workers never collide), so cap-detection scans ONLY this
    # pass's output. The shared logs/pass.log is still maintained for human tailing (header + the
    # pass output folded back in + footer), but it is NEVER the source for the kill marker — a
    # concurrent worker's marker in the shared file must not reclassify this pass.
    safe_resource = re.sub(r"[^A-Za-z0-9_.-]", "_", resource) if resource else "all"
    pass_log = LOG.parent / f"pass-{mode}-{safe_resource}-{os.getpid()}.log"
    rc = 1
    pass_output = ""  # default so a subprocess-spawn error can't NameError the cap-hit check below
    try:
        with pass_log.open("w+") as out:
            rc = subprocess.run(argv, cwd=str(SELLER_DIR), env=env,
                                stdout=out, stderr=out).returncode
        pass_output = _read_tail(pass_log)
    finally:
        # Fold this pass's output into the shared human log (header + body + footer), then drop the
        # per-pass file. Best-effort: a logging hiccup must never change the rc the daemon acts on.
        try:
            with LOG.open("a") as shared:
                shared.write(f"=== {_utcnow()} {label} pass ({harness.name}) ===\n")
                try:
                    shared.write(pass_log.read_text(errors="replace"))
                except OSError:
                    pass
                shared.write(f"=== {_utcnow()} {label} pass done rc={rc} ===\n")
        except OSError:
            pass
        try:
            pass_log.unlink(missing_ok=True)
        except OSError:
            pass
    # Fix C: distinguish a turn-cap kill ("more work pending") from a generic failure. Only the buyer
    # pass tours a backlog, so only it can strand work at the cap — scope the detection to it.
    if mode == "buyer" and _is_cap_hit(rc, pass_output):
        _record_cap_hit(mode, resource)
        return CAP_HIT_SIGNAL
    # Track A3 — completion gate. A gated pass must not end owning a never-fired send: if it does,
    # signal a prioritized re-drive (independent of rc — a stranded send is more urgent than the exit
    # code). The lease guard inside _undriven_intents avoids racing a concurrent worker's in-flight send.
    if mode in GATED_MODES and _undriven_intents(resource):
        _record_redrive(mode, resource)
        return REDRIVE_SIGNAL
    return rc


def run_intent(msg: str) -> int:
    harness = _resolve_harness()
    argv, env = _invocation(harness, build_spec("intent", msg))
    try:
        out = subprocess.run(argv, cwd=str(SELLER_DIR), env=env, capture_output=True,
                             text=True, timeout=25)
    except subprocess.SubprocessError:
        return 1
    sys.stdout.write(out.stdout[:200])
    return out.returncode


def _utcnow() -> str:
    # Date.now-equivalent; isolated so the rest stays pure/testable.
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


PASS_MODES = ("channel", "seller", "buyer", "buy", "maint", "research")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: harness_run.py <channel|seller|buyer|buy|maint|intent> "
                         "[message] [--resource <market>]\n")
        return 2
    mode = argv[1]
    rest = argv[2:]
    resource = ""
    if "--resource" in rest:
        i = rest.index("--resource")
        resource = rest[i + 1] if i + 1 < len(rest) else ""
    try:
        if mode == "intent":
            return run_intent(rest[0] if rest and not rest[0].startswith("--") else "[message]")
        if mode in PASS_MODES:
            return run_pass(mode, resource)
    except UnknownHarness as exc:
        sys.stderr.write(f"selly: {exc}\n")
        return 3
    sys.stderr.write(f"unknown mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
