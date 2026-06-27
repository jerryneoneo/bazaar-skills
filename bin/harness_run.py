#!/usr/bin/env python3
"""harness_run.py — invoke ONE headless pass through the harness seam (no hardcoded CLI).

This is the agnostic replacement for the old Claude-only run_pass.sh/intent.sh bodies. It builds a
harness-agnostic PassSpec per mode, asks the active harness (`$BAZAAR_HARNESS`, default claude-code)
to translate it into argv + env via `Harness.pass_argv`, then runs it.

  harness_run.py channel         → drain the control channel, BOTH sides (bazaar-run §1). Alias: `seller`.
  harness_run.py buyer           → one SELL-inbox watch pass (buyers messaging the seller; §2)
  harness_run.py buy             → one BUY-side step: search/liaise a want like an iPhone (§3)
  harness_run.py maint           → one cross-listing step: drain distribution / cadence-scan (§2b)
  harness_run.py intent "<msg>"  → ONE short MCP-less "what I'll do next" line (printed to stdout)

These mirror the four phases of .claude/commands/bazaar-run.md so the always-on daemon drives the
whole agent, not just the channel + sell-inbox. (`buyer` = the seller's inbox; `buy` = acquiring for
the user — distinct, despite the close names.)

Runtime scope: only `claude-code` is wired + verified today. For any other harness the seam exists
(see bin/harnesses/) but the runner refuses, rather than launching a half-supported pass. Add a
harness by implementing + verifying its `pass_argv` and dropping the guard below.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harnesses import UnknownHarness, get_harness  # noqa: E402
from harnesses.base import PassSpec  # noqa: E402

try:
    import channel_log  # noqa: E402  short-term memory: tail injected into the channel pass
except ImportError:
    channel_log = None

SELLER_DIR = Path(__file__).resolve().parent.parent
LOG = SELLER_DIR / "logs" / "pass.log"

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

CHANNEL_PROMPT = """You are the Bazaar agent on the control channel, headless and unattended. Be
RESPONSIVE — send a short progress message at each step; never go silent on a long task. The system
ALREADY sent a one-line intent ack (e.g. 'Let me check your listings…') and a native 'typing…'
indicator is showing — so do NOT repeat a 'let me check' line; go straight to the work and send
PROGRESS + RESULTS as you go. Fire 'telegram.py typing' right before each message you send.

Do ONE turn of the CONTROL CHANNEL phase (§1 of .claude/commands/bazaar-run.md) — BOTH sell and
buy — then stop:
0a. PAUSE CHECK — run `python3 bin/control.py status`. If paused: take NO marketplace action this
   pass (the daemon's deterministic drain normally handles the paused channel; if you are running,
   just acknowledge + stop). If NOT paused but there are pending corrections (applied:false), run
   skills/channel/corrections.md FIRST to apply them to durable state, then continue normal work.
0. THEN read data/listing_session.json, data/distribution_session.json AND data/buy_session.json.
   If ANY is active you are MID-FLOW: apply the user's latest reply to THAT session's current step.
   NEVER re-ask which item/want it is — the session knows. Only treat a message as NEW when no
   session is active. If a RECENT CONTROL-CHANNEL CONVERSATION block is present below, READ it: it
   is what you and the user just said. A no-session message like 'do all', 'do all tasks', 'take
   over all', 'both', 'yes', 'go ahead', 'auto', 'the first one'/'#2' is a FOLLOW-UP to your last
   [out] turn (especially an 'enumerated-tasks' turn) — resolve it against that turn and ACT; do
   NOT fall back to 'let me check what needs doing'.
1. run `python3 bin/telegram.py poll` (download photos with `bin/telegram.py getfile`).
2. Route each event in order:
   - mid-flow reply → feed it to the active session (listing/distribution → listing.md /
     distribution.md; buy → skills/buying/search.md or the buying answer handlers).
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

BUYER_PROMPT = """You are the Bazaar seller agent, running headless and unattended.
Run ONE buyer-inbox watch pass per .claude/commands/sell-watch.md over your enabled
seller_config.marketplaces: open the relevant inbox(es) (see COST DISCIPLINE for which) in the
(already-running) Chrome and handle each new buyer message past its cursor via
skills/reply-pipeline.md — auto-negotiate price offers through
`python3 bin/negotiate.py` (anti-probing + discreet cross-buyer), quote delivery via
bin/shipping.py, answer from qa_bank, escalate unknowns to the seller over Telegram
(skills/channel/notifications.md). Respect pacing/caps and per-thread cursors (idempotent).

COST DISCIPLINE (a cheap non-LLM probe already detected new activity before launching you):
$BAZAAR_BUYER_PEEK_TEXT hints which marketplace/snippet is new — go STRAIGHT to that
marketplace's inbox and open only thread(s) with messages past their cursor. Handle ONLY the
marketplace the peek points to; do NOT tour every marketplace in one pass (another pass picks up
the rest). Do NOT browser_snapshot whole inbox pages when a targeted thread read suffices;
snapshots are the single biggest cost per pass. If $BAZAAR_BUYER_PEEK_FORCED=1 this is a periodic
safety-net sweep with NO specific signal — open only the single most-recently-active inbox,
confirm nothing sits unread past its cursor, and stop; do NOT sweep all marketplaces.

TURN BUDGET (hard rule — your turn cap is finite and being killed mid-pass loses ALL progress and
your summary): you MUST end with a one-line summary, so reserve your final turn for it and STOP.
Do not open another inbox or thread once you are running low on turns — partial progress is fine
because every reply marks its thread read, so the next pass resumes where you left off. NEVER loop
on a stuck step: if an inbox or thread won't open after ONE retry, escalate it over Telegram
(skills/channel/notifications.md) and move on. Stop as soon as the pass is complete."""

MAINT_PROMPT = """You are the Bazaar agent doing cross-listing maintenance, headless and unattended.
This is a BACKGROUND pass — do NOT poll the control channel or buyer inboxes for INBOUND messages
(reading replies is the channel/buyer passes' job). You DO, however, send OUTBOUND completion
notifications for work you finish this pass — a `say` is a one-way push (telegram.py send) that needs
no polling, so 'quiet background pass' must NOT mean 'silently drop the success notice'. Do ONE step
of bazaar-run.md §2b, then stop:
0. If data/listing_session.json is active → do nothing (never interrupt an active listing); end.
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
     (i)  skills/channel/distribution.md SCAN — find listings made OUTSIDE Bazaar (unmanaged) → queue
          to manage + cross-list under the distribution gate.
     (ii) skills/inbox-detect.md SWEEP (scope=both) — review m's chat list for threads the user started
          on their OWN that are NOT yet tracked (absent from data/threads/ and data/buyer_threads/) and
          surface a takeover offer under the takeover gate, persisting data/inbox_detect_session.json
          for TAKEOVER on a later pass. This is how a chat you started yourself reaches your channel.
   If nothing is due → end (no work)."""

BUY_PROMPT = """You are the Bazaar BUYER agent (acquiring for the user), headless and unattended.
Do ONE buy-side step of bazaar-run.md §3 + .claude/commands/buy-run.md, then stop. $BAZAAR_BUY_PEEK_WANT
names the actionable want; $BAZAAR_BUY_PEEK_TEXT is a hint. Load data/buyer_config.json.

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

# Skills folded into the cached prefix per mode (byte-stable, no volatile data) — fail-open.
# `skills/style.md` is the stable voice/persona rulebook (the volatile prefs live in data/style.json,
# read at compose time); it rides the prefix anywhere a message is composed — buyer/buy replies and
# control-channel say/ask. Not in `maint` (a background pass composes only a fixed completion notice).
CORE_SKILLS = {
    "channel": ("skills/channel/notifications.md", "skills/channel/channel.md",
                "skills/channel/corrections.md", "skills/style.md"),
    "buyer": ("skills/reply-pipeline.md", "skills/channel/notifications.md", "skills/style.md"),
    "buy": ("skills/buying/liaison-pipeline.md", "skills/channel/notifications.md",
            "skills/style.md"),
    "maint": ("skills/channel/notifications.md",),
}

# Courtesy pause line for the background passes: the PreToolUse hook (bin/hooks/pause_guard.py) is
# the real, deterministic enforcement (it denies the reserve + browser mutations while paused), and
# the daemon interrupts a running pass — this just lets a compliant pass narrate the pause cleanly
# instead of being SIGTERM'd mid-sentence. Byte-stable → folded into the cached prefix.
PAUSE_LINE = ("\n\nPAUSE: before any `bin/pacing_gate.py reserve` (i.e. before any marketplace send),"
              " run `python3 bin/control.py is-paused`; if it exits 0 (paused), send a brief 'Paused"
              " — holding here' line and STOP this pass. (The harness also blocks sends while paused.)")


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


def _seller_fast_step() -> str:
    """awaiting_price/awaiting_floor are pure wizard turns → fast model + small turn cap so the next
    question lands quickly. START/publish/anomaly stay on the strong model. Fail-open → ''."""
    try:
        s = json.loads((SELLER_DIR / "data" / "listing_session.json").read_text())
    except (OSError, ValueError):
        return ""
    step = s.get("step", "") if s.get("active") else ""
    return step if step in ("awaiting_price", "awaiting_floor") else ""


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
        model, max_turns = (None, None)
        tail_turns = channel_log.DEFAULT_MAX_TURNS if channel_log else 0
        if _seller_fast_step():
            model, max_turns = ("sonnet", 8)  # cheap wizard turn → fast + small cap
            tail_turns = 6  # mid-wizard rarely needs deep history; spare the small turn cap
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
        # is plenty and far cheaper. 40 is a BACKSTOP, not a workload bound: eval found the buyer
        # pass exhausting whatever cap it was given (14→28→40 all failed ~82% of the time) by
        # touring every inbox and looping on stuck navigation — so progress comes from BUYER_PROMPT's
        # TURN BUDGET governor (bound discovery to the peek-hinted market, reserve the last turn to
        # summarise, one-retry-then-escalate), NOT from raising this number. Don't bump it chasing
        # rc=1; replies mark threads read, so partial progress carries across passes.
        return PassSpec(
            prompt=_scope_prefix(resource) + BUYER_PROMPT + PAUSE_LINE, model="sonnet", max_turns=40,
            allowed_tools=BASE_TOOLS + _browser_tools(BUYER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("buyer"),
            prompt_cache_1h=True,
        )
    if mode == "buy":
        # Buy-side liaison/search — same mechanical profile as the sell inbox (Sonnet + turn cap);
        # money lives in buyer_negotiate.py/budget_gate.py. Reuses the smaller buyer browser set.
        # Same backlog rationale as the buyer pass: 14 was too low and stranded liaison work (rc=1).
        return PassSpec(
            prompt=_scope_prefix(resource) + BUY_PROMPT + PAUSE_LINE, model="sonnet", max_turns=28,
            allowed_tools=BASE_TOOLS + _browser_tools(BUYER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("buy"),
            prompt_cache_1h=True,
        )
    if mode == "maint":
        # Cross-listing is MECHANICAL (read listing URLs, drain the queue), so right-size it to
        # sonnet instead of the strong DEFAULT — a large saving on a background pass that does not
        # need the deepest model. Gated behind BAZAAR_MAINT_MODEL so it reverts instantly if the
        # publish/verify path regresses: set it to "" (empty) to restore the strong DEFAULT (model
        # None → no --model flag), or to any model name to pin that. Full seller browser set stays.
        maint_model = os.environ.get("BAZAAR_MAINT_MODEL", "sonnet") or None
        return PassSpec(
            prompt=_scope_prefix(resource) + MAINT_PROMPT + PAUSE_LINE, model=maint_model, max_turns=None,
            allowed_tools=BASE_TOOLS + _browser_tools(SELLER_BROWSER),
            permission_mode="acceptEdits", system_prompt_append=_core_skills_block("maint"),
            prompt_cache_1h=True,
        )
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
        # carries no browser tools and can't act on the world; sonnet for nuance. Deliberately NOT
        # in PASS_MODES — the daemon must never run the judge; it's on-demand via /bazaar-eval only.
        return PassSpec(prompt=msg, model="sonnet", max_turns=1, strict_mcp=True, mcp_servers={})
    raise ValueError(f"unknown mode: {mode}")


def _resolve_harness():
    """Active harness from $BAZAAR_HARNESS (default: autodetect, prefers signed-in). Refuse a
    harness whose runtime isn't verified yet — honest scope beats a broken daemon."""
    name = os.environ.get("BAZAAR_HARNESS") or None
    harness = get_harness(name)
    if harness.name not in SUPPORTED_RUNTIME:
        sys.stderr.write(
            f"bazaar: runtime not yet supported for harness '{harness.name}'. Only "
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
    env = {**inv.env, **os.environ}
    return argv, env


def run_pass(mode: str, resource: str = "") -> int:
    harness = _resolve_harness()
    argv, env = _invocation(harness, build_spec(mode, resource=resource))
    if resource:
        # Pass the scoped marketplace to the skills (they read $BAZAAR_RESOURCE to know which
        # marketplace + which tab is theirs). Empty resource → unscoped, env unchanged (legacy path).
        env = {**env, "BAZAAR_RESOURCE": resource}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    label = f"{mode}:{resource}" if resource else mode
    with LOG.open("a") as log:
        log.write(f"=== {_utcnow()} {label} pass ({harness.name}) ===\n")
        log.flush()
        rc = subprocess.run(argv, cwd=str(SELLER_DIR), env=env, stdout=log, stderr=log).returncode
        log.write(f"=== {_utcnow()} {label} pass done rc={rc} ===\n")
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


PASS_MODES = ("channel", "seller", "buyer", "buy", "maint")


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
        sys.stderr.write(f"bazaar: {exc}\n")
        return 3
    sys.stderr.write(f"unknown mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
