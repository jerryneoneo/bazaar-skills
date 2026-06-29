#!/usr/bin/env python3
"""agent_daemon.py — the always-on supervisor (no LLM).

The reliable, local "always-on" half of the seller agent. It runs forever under launchd and
keeps idle cost at zero by only invoking the LLM when there is real work:

  • Seller channel: long-`peek` Telegram (non-consuming). On pending events → run the SELLER
    pass (`run_pass.sh seller`), which invokes `claude -p` to actually poll + handle them.
  • Buyer inboxes: every `buyer_poll_sec` → run the BUYER pass (`run_pass.sh buyer`), which
    drives the seller's enabled marketplace inboxes via the warm CDP Chrome.

Reliability properties:
  • single-flight run lock — never two passes at once (protects the ledger/cursors)
  • idempotent — Telegram offset + per-thread cursors mean a retried pass double-does nothing
  • crash-isolated — a failed pass is logged; the loop continues; launchd restarts the daemon
  • SIGTERM-clean — releases the lock and exits so launchd can stop/restart it

Run: agent_daemon.py            (forever, under launchd)
     agent_daemon.py --once --dry-run   (one iteration, log decisions, don't invoke claude)
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SELLER_DIR = Path(__file__).resolve().parent.parent
BIN = SELLER_DIR / "bin"
LOG_DIR = SELLER_DIR / "logs"
RUN_LOCK = SELLER_DIR / ".daemon.runlock"
INSTANCE_LOCK = SELLER_DIR / ".daemon.instancelock"
HEARTBEAT = SELLER_DIR / ".daemon.heartbeat"  # wall-clock last-tick so healthcheck spots a wedged loop
CONFIG_PATH = SELLER_DIR / "data" / "config.json"
SELLER_CONFIG_PATH = SELLER_DIR / "data" / "seller_config.json"

# The launchd LaunchAgent label for this daemon (Fix D: relaunch_self kickstarts it on a code change).
AGENT_LABEL = "com.bazaarskills.agent"
# Fix D — per-iteration stall guard. Each main-loop iteration is timed; an iteration that runs longer
# than this many seconds logs a WARN so a hung iteration is VISIBLE (the ~7-min supervisor stall from a
# hung worker would have surfaced here). It is purely observability — we never kill the process
# mid-iteration (run_pass has its own 900s tree-kill deadline; the watchdog handles a wedged loop).
LOOP_ITER_BUDGET = 120

sys.path.insert(0, str(BIN))
from harnesses import UnknownHarness, get_harness  # noqa: E402  (local bin/harnesses package)
import control  # noqa: E402  the single source of truth for the pause flag (data/control.json)
import instance_lock  # noqa: E402  PID-aware singleton lock (reclaims a stale lock; no respawn storm)
import proc_tree  # noqa: E402  kill the whole pass tree on preempt/timeout (shared with supervisor)
import notify_watch  # noqa: E402  notification-path trigger (macOS Notification Center; fail-open)
import notify_db  # noqa: E402  FDA-gated Notification Center reader (startup wake-mode self-check)
import tab_park  # noqa: E402  keep Meta (notification-path) tabs hidden so their OS push keeps firing

# The runtime dir (beside the agent) or one level up (dev tree) — the harness reads its own secret
# store under whichever holds it, so the daemon never hardcodes settings.local.json.
TOKEN_DIRS = [SELLER_DIR, SELLER_DIR.parent]

# Fix C — turn-budget robustness. A buyer pass killed at the hard turn cap returns this DISTINCT exit
# code (harness_run.run_pass maps "rc!=0 + 'Reached max turns'" to it), so the daemon can tell
# "capped, more work pending" from a generic failure and run exactly one bounded continuation per
# cap-hit, up to CONTINUATION_RETRY_CAP times, before ESCALATING (never silently dropping a backlog).
import harness_run  # noqa: E402  CAP_HIT_SIGNAL single source of truth
import lease  # noqa: E402  per-market lease — the outbox sweeper skips a market a live worker owns
import thread_outbox  # noqa: E402  the send-intent log the outbox sweeper drains stranded sends from
CAP_HIT_SIGNAL = harness_run.CAP_HIT_SIGNAL
REDRIVE_SIGNAL = harness_run.REDRIVE_SIGNAL  # a gated pass ended owning a never-fired send
CONTINUATION_RETRY_CAP = 2

# Track A5 — the outbox sweeper. A still-pending send in thread_outbox that no live worker owns is
# STRANDED. The sweeper re-drives it deterministically (no LLM to decide WHETHER to recover); a
# re-drive is what triggers the recovery pass, which is the ONLY thing that can read the live chat and
# tell "already delivered, just unjournaled" from "genuinely never sent". So the sweeper itself never
# asserts a send failed — it re-drives, and only escalates (humbly, "still verifying") as a last resort.
#   OUTBOX_REDRIVE_AFTER_SEC sits above a normal pacing/in-flight window so a mid-pacing send is never
#     mistaken for stranded (the lease guard is the primary protection; this is the age backstop).
#   OUTBOX_ESCALATE_ATTEMPTS / OUTBOX_ESCALATE_AFTER_SEC gate the user-facing escalation: we only ping
#     once an intent has been re-driven enough times AND stayed stranded long enough that a recovery
#     pass has genuinely had its chance (the vida false alarm fired ~26 min before recovery resolved
#     it — the wall-clock floor would have suppressed it). Escalation is exactly-once via the durable
#     `escalated` marker on the record (not the attempts counter), so re-drives can't race it.
OUTBOX_REDRIVE_AFTER_SEC = 90
OUTBOX_ESCALATE_ATTEMPTS = 3
OUTBOX_ESCALATE_AFTER_SEC = 1800  # 30 min stranded before we ever surface it (recovery runs first)

# Nightly self-eval subprocess timeouts. The deterministic layer is pure file I/O (fast); the LLM
# judge runs up to MAX_JUDGE/BATCH_SIZE sonnet batches (see eval_judge.py), so it needs minutes, not
# seconds — a 120s cap would kill it mid-run.
EVAL_TIMEOUT_SEC = 120
EVAL_JUDGE_TIMEOUT_SEC = 900

_stop = False


def _handle_sigterm(signum, frame):
    global _stop
    _stop = True


def _source_fingerprint() -> int:
    """Newest mtime (ns) across the daemon's own Python sources (bin/*.py + bin/hooks/*.py).

    These modules are imported into this long-running process, which is NOT hot-reloaded — a code
    change here only takes effect on restart. The main loop (and the concurrent supervisor) compare
    this against the value captured at startup and exit cleanly when it changes, so launchd
    (KeepAlive) respawns the daemon on fresh code. This closes the "stale daemon silently runs old
    logic across a code change" failure mode (e.g. a pause fix that never took effect). Passes and
    skills run as fresh subprocesses already, so they need not trigger a restart."""
    newest = 0
    for d in (BIN, BIN / "hooks"):
        try:
            entries = list(d.glob("*.py"))
        except OSError:
            continue
        for f in entries:
            try:
                m = f.stat().st_mtime_ns
            except OSError:
                continue
            if m > newest:
                newest = m
    return newest


def relaunch_self() -> None:
    """Fix D: best-effort single, atomic restart of THIS LaunchAgent on a source change.

    `launchctl kickstart -k gui/<uid>/<label>` stops the running job and starts it again in ONE step
    — no unload+load double-spawn, no window where two instances race the lock. Called right before
    the source-change loop break (in BOTH the single-flight loop and the supervisor), so the bounce
    onto fresh code is a clean single restart instead of relying on KeepAlive to notice a clean exit.

    FAIL-OPEN: a non-zero rc (e.g. the job isn't loaded under launchd — a dev `--once` run) or any
    error is logged and swallowed. It must NEVER raise: the loop is exiting anyway, and KeepAlive
    (SuccessfulExit:false won't fire on this clean exit) plus the watchdog remain the backstops."""
    try:
        uid = os.getuid()
        target = f"gui/{uid}/{AGENT_LABEL}"
        out = subprocess.run(["launchctl", "kickstart", "-k", target],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            logging.info("relaunch_self: kickstarted %s (clean single restart on fresh code)", target)
        else:
            logging.info("relaunch_self: kickstart %s rc=%s (not under launchd? leaving exit to "
                         "KeepAlive/watchdog): %s", target, out.returncode, out.stderr.strip()[:120])
    except (OSError, subprocess.SubprocessError) as exc:
        logging.info("relaunch_self: kickstart failed (%s) — exiting; KeepAlive/watchdog backstop", exc)


def iteration_stall_warning(elapsed: float, budget: float = LOOP_ITER_BUDGET) -> str | None:
    """PURE (Fix D): the WARN message for a loop iteration that overran `budget` seconds, else None.

    Observability only — a too-slow iteration (a hung subprocess) is made VISIBLE in the log; the
    loop is NOT killed mid-iteration (that's run_pass's deadline + the watchdog's job). Returns None
    at or under budget so the common fast iteration is silent."""
    if elapsed > budget:
        return f"loop iteration took {elapsed:.0f}s (> {budget:.0f}s budget) — a pass/probe may be wedged"
    return None


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    # Rotate so an always-on daemon's log can't grow without bound (10MB x 5 = 50MB ceiling).
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "daemon.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[file_handler, logging.StreamHandler()],
    )


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    return {
        "buyer_poll_sec": cfg.get("buyer_poll_sec", cfg.get("watch_poll_sec", 300)),
        "peek_timeout": cfg.get("channel_poll_sec", 25),
        # Safety net: even when the cheap buyer_peek reports nothing new, force a full buyer
        # pass every Nth consecutive empty peek so a missed/flaky unread signal can't strand a
        # buyer. 0 disables the count net (preferred once the time floor below covers strands).
        "force_buyer_pass_every": cfg.get("force_buyer_pass_every", 6),
        # Absolute time floor (hours): force a buyer pass if this long has elapsed since the last
        # actual buyer pass, regardless of the count net. This is the cheap strand backstop that
        # lets force_buyer_pass_every drop to 0 without a flaky-badge strand going unnoticed for a
        # day. 0 disables the floor. See buyer_force_due().
        "force_buyer_sweep_hours": cfg.get("force_buyer_sweep_hours", 2),
        # Buy side (§3) + cross-listing maintenance (§2b): cadence + safety net, mirroring the
        # buyer-inbox knobs. Both are gated by cheap non-LLM probes so idle cost stays ~zero.
        "buy_poll_sec": cfg.get("buy_poll_sec", 600),
        "maint_poll_sec": cfg.get("maint_poll_sec", 600),
        "force_buy_pass_every": cfg.get("force_buy_pass_every", 6),
        # Nightly self-eval: how often to CHECK whether one is due (the actual cadence is
        # config.eval_interval_hours, owned by eval_state.py; 0 there disables it).
        "eval_poll_sec": cfg.get("eval_poll_sec", 3600),
        # Does the nightly run also invoke the billed LLM judge? Default on ("on for everyone");
        # set false for a $0 nightly (deterministic checks only). The deterministic layer always runs.
        "eval_judge_nightly": cfg.get("eval_judge_nightly", True),
        # Upstream update check: how often to CHECK for a newer Bazaar (the actual network throttle
        # is config.update_check_interval_hours, owned by update_check.py; 0 there disables it).
        "update_poll_sec": cfg.get("update_poll_sec", 3600),
        # Stale-chat follow-ups: how often to CHECK whether any thread is due for a nudge or a
        # not-interested drop (the actual 1d/3d/3d cadence + master toggle live in the followup_*
        # config keys, owned by followup_state.py). Cheap, file-only probe — idle cost stays ~zero.
        "followup_poll_sec": cfg.get("followup_poll_sec", 3600),
        # Track A5 — outbox sweeper cadence: how often to check thread_outbox for a STRANDED
        # never-fired send and re-drive it (cheap non-LLM peek; the actual re-drive is a scoped buyer
        # pass). Well below force_buyer_sweep_hours so a stranded reply recovers in minutes, not hours.
        "outbox_sweep_poll_sec": cfg.get("outbox_sweep_poll_sec", 120),
        # Phase 3: >1 opts into the concurrent supervisor (parallel sell-inbox workers across
        # marketplaces). Default 1 keeps the single-flight loop below — byte-identical behavior.
        # BAZAAR_MAX_WORKERS env overrides the file: an ops kill-switch to force single-flight
        # (=1) without editing config, and the seam tests use to pin the path. Coerce defensively:
        # a fat-fingered string/None must never crash the dispatch (falls to 1).
        "max_concurrent_workers": _int_or(
            os.environ.get("BAZAAR_MAX_WORKERS") or cfg.get("max_concurrent_workers", 1), 1),
    }


def _int_or(value, default):
    """int(value) clamped to >= 1, or `default` if it isn't a usable integer."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def buyer_force_due(empty_peeks: int, force_every: int, idle_sec: float,
                    floor_sec: float) -> tuple[bool, str]:
    """PURE: should a forced buyer pass fire now, despite the cheap peek finding nothing new?

    Two independent safety nets, either of which fires:
      • count net  — force after `force_every` consecutive empty peeks (0 disables).
      • time floor — force if `idle_sec` (since the last actual buyer pass) >= `floor_sec`
        (0 disables). The floor is the strand backstop when the count net is OFF: buyer_peek
        advances its memo on every peek regardless of pass outcome, so a memo-advance followed
        by a failed LLM pass could otherwise strand a buyer until the (now-off) count net fired.
        A 1 to 2 hour floor keeps that protection cheap without re-introducing 20-minute empty
        sweeps. Returns (due, human reason)."""
    if force_every and empty_peeks >= force_every:
        return True, f"safety-net after {empty_peeks} empty peeks"
    if floor_sec > 0 and idle_sec >= floor_sec:
        return True, f"absolute floor ({floor_sec / 3600:.1f}h since last buyer pass)"
    return False, ""


def buyer_action(pending: int, forced: bool, floor_due: bool, recheck_unhandled) -> str:
    """PURE: what should a buyer poll do this tick? (Tier 2a — gate the forced sweep on a ~0-token
    recheck so a forced empty sweep no longer pays for a full LLM pass.)

      'pass' — fire the full LLM buyer pass. Reasons: the cheap peek saw real new mail (`pending`);
               the 2h time floor is due (ultimate strand backstop — force an ACTUAL pass even when
               the cheap signals say clear, covering a strand that left count==0); or a count-net
               force fired AND the deterministic recheck found unhandled mail.
      'skip' — a count-net force fired but the recheck says every inbox is clear → spend ~0 tokens
               and skip the LLM pass.
      'idle' — not due to act this tick.

    `recheck_unhandled` is None when no recheck was run (the caller runs it ONLY on a count-net
    force, never wasting it on a floor force or a real pending peek)."""
    if pending:
        return "pass"
    if not forced:
        return "idle"
    if floor_due:
        return "pass"
    return "pass" if recheck_unhandled else "skip"


def _listing_active() -> bool:
    """True when a listing wizard is mid-flow. The ~6s intent line is redundant then — the next
    question IS the response — so the daemon skips it (the native 'typing…' indicator still pulses,
    and the now-fast wizard pass answers quickly). Fail-open: unreadable → not active."""
    try:
        return bool(json.loads((SELLER_DIR / "data" / "listing_session.json").read_text()).get("active"))
    except (OSError, ValueError):
        return False


def _distribution_active() -> bool:
    """True when a cross-listing batch is mid-flow (its queue still has items to drain). Fail-open."""
    try:
        return bool(json.loads((SELLER_DIR / "data" / "distribution_session.json").read_text()).get("active"))
    except (OSError, ValueError):
        return False


def _scan_due(env: dict) -> bool:
    """Cheap, non-LLM: is an enabled marketplace overdue for a cross-listing SCAN? (scan_state.py)."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "scan_state.py"), "due"],
                             capture_output=True, text=True, env=env, timeout=15)
        return out.returncode == 0 and bool(json.loads(out.stdout).get("due_market"))
    except (subprocess.SubprocessError, ValueError):
        return False


def _inbox_detect_active() -> bool:
    """True when an inbox-sweep takeover batch is mid-flow (queue still draining). Fail-open."""
    try:
        return bool(json.loads((SELLER_DIR / "data" / "inbox_detect_session.json").read_text()).get("active"))
    except (OSError, ValueError):
        return False


def _inbox_sweep_due(env: dict) -> bool:
    """Cheap, non-LLM: is a market (sell OR buy) overdue for an inbox sweep? (inbox_detect.py, union
    of enabled markets — so buy-only setups still get an autonomous sweep)."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "inbox_detect.py"), "due"],
                             capture_output=True, text=True, env=env, timeout=15)
        return out.returncode == 0 and bool(json.loads(out.stdout).get("due_market"))
    except (subprocess.SubprocessError, ValueError):
        return False


def _eval_due(env: dict) -> bool:
    """Cheap, non-LLM: is the nightly deterministic self-eval due? (eval_state.py)."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "eval_state.py"), "due"],
                             capture_output=True, text=True, env=env, timeout=15)
        return out.returncode == 0 and bool(json.loads(out.stdout).get("due"))
    except (subprocess.SubprocessError, ValueError):
        return False


def _followup_due(env: dict) -> dict:
    """Cheap, non-LLM: are any chats due for a follow-up nudge or a not-interested drop?
    (followup_state.py). Fail-open to 'no work' on any error (same posture as _eval_due)."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "followup_state.py"), "due"],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode != 0:
            return {"nudges": 0, "drops": 0, "enabled": False, "due_nudges": []}
        data = json.loads(out.stdout)
        counts = data.get("counts", {})
        return {"nudges": counts.get("nudges", 0), "drops": counts.get("drops", 0),
                "enabled": data.get("enabled", False), "due_nudges": data.get("due_nudges", [])}
    except (subprocess.SubprocessError, ValueError):
        return {"nudges": 0, "drops": 0, "enabled": False, "due_nudges": []}


def run_followup_reconcile(env: dict) -> None:
    """Prune ledger entries for threads answered/closed since the last scan, so we never nudge a
    counterpart who just replied. Best-effort + fail-open."""
    try:
        subprocess.run([sys.executable, str(BIN / "followup_state.py"), "reconcile"],
                       capture_output=True, text=True, env=env, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        logging.warning("followup reconcile error (non-fatal): %s", exc)


def run_followup_drops(env: dict, dry_run: bool) -> None:
    """Mark every cold chat not_interested and enqueue ONE batched 'went quiet' notice. $0
    deterministic (no LLM, no browser). Idempotent + fail-open."""
    if dry_run:
        logging.info("[dry-run] would mark cold chats not interested + notify the user")
        return
    try:
        out = subprocess.run([sys.executable, str(BIN / "followup_state.py"), "drops"],
                             capture_output=True, text=True, env=env, timeout=30)
        if out.returncode == 0:
            r = json.loads(out.stdout)
            if r.get("dropped"):
                logging.info("followup drops: %s marked not interested, %s notified",
                             r.get("dropped"), r.get("notified"))
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        logging.warning("followup drops error (non-fatal): %s", exc)


def _listing_health_session_active() -> bool:
    """True when a stale-listing suggestion episode is mid-flow (the MAINT pass owns it). Fail-open."""
    try:
        return bool(json.loads(
            (SELLER_DIR / "data" / "listing_health_session.json").read_text()).get("active"))
    except (OSError, ValueError):
        return False


def _listing_health_due(env: dict):
    """Cheap, non-LLM: which (if any) stale LIVE listing is due for an improvement-suggestion
    episode? (listing_health.py). Returns the item_id or None. Fail-open to None."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "listing_health.py"), "due"],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout).get("due_item")
    except (subprocess.SubprocessError, ValueError):
        return None


def run_listing_health_start(env: dict, item_id: str, dry_run: bool) -> None:
    """Open a stale-listing episode: write the MAINT session baton + stamp the rate-limit cursor, so
    the next maint pass composes + sends suggestions for this one item. Best-effort + fail-open."""
    if dry_run:
        logging.info("[dry-run] would start stale-listing episode for %s", item_id)
        return
    try:
        subprocess.run([sys.executable, str(BIN / "listing_health.py"), "start", "--item", item_id],
                       capture_output=True, text=True, env=env, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        logging.warning("listing_health start error (non-fatal): %s", exc)


def run_eval(env: dict, dry_run: bool, use_judge: bool = False) -> None:
    """Run the nightly self-eval and stamp it. The deterministic layer always runs ($0, no LLM, no
    browser, no channel send). When `use_judge` is set (config.eval_judge_nightly), the same run also
    invokes the billed MCP-less sonnet judge for nuance, so the subprocess gets the larger judge
    timeout. Findings land in data/eval/; the daemon only logs a one-line summary. Fail-open."""
    argv = [sys.executable, str(BIN / "eval_run.py"), "run"]
    if not use_judge:
        argv.append("--no-llm")  # deterministic-only: never imports eval_judge, makes zero LLM calls
    timeout = EVAL_JUDGE_TIMEOUT_SEC if use_judge else EVAL_TIMEOUT_SEC
    label = "deterministic + LLM judge" if use_judge else "deterministic ($0)"
    if dry_run:
        logging.info("[dry-run] would run %s self-eval (%s)", label, " ".join(argv[2:]))
        return
    try:
        out = subprocess.run(argv, capture_output=True, text=True, env=env,
                             cwd=str(SELLER_DIR), timeout=timeout)
        summary = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else f"rc={out.returncode}"
        logging.info("self-eval (%s): %s", label, summary[:200])
        subprocess.run([sys.executable, str(BIN / "eval_state.py"), "mark"],
                       capture_output=True, text=True, env=env, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        logging.warning("self-eval failed: %s", exc)


# Daemon update notice dedupes per VERSION via a long snooze (a NEWER upstream version still breaks
# through — see update_check.should_prompt). So the seller is pinged about a given release ~once, not
# every cadence, but is always told when a fresh release lands.
UPDATE_NOTICE_SNOOZE_DAYS = 30


def check_and_notify_update(channel: dict, env: dict, dry_run: bool, *, via_outbox: bool) -> None:
    """Throttled, read-only upstream-update check -> ONE channel heads-up if a newer Bazaar exists.
    NEVER auto-applies (account safety); the seller runs /bazaar-upgrade. via_outbox: the supervisor
    ENQUEUEs (its single writer drains the outbox); the single-flight loop sends directly (it has no
    outbox drain). Fail-open throughout — a broken check must never disturb the loop."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "update_check.py"), "check"],
                             capture_output=True, text=True, env=env, timeout=20)
        info = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}
    except (subprocess.SubprocessError, ValueError):
        return
    if not info.get("should_prompt"):
        return
    summary = info.get("summary") or f"v{info.get('current', '?')} -> v{info.get('latest', '?')}"
    text = (f"🆙 Bazaar update available: {summary}. "
            f"Run /bazaar-upgrade when convenient (I won't auto-update).")
    if dry_run:
        logging.info("[dry-run] would notify update: %s", summary)
        return
    if channel.get("adapter") != "telegram":
        logging.info("update available (%s) but adapter=%s notice not wired; skipping",
                     summary, channel.get("adapter"))
        return  # don't snooze: a future telegram-capable run should still surface it
    try:
        if via_outbox:
            subprocess.run([sys.executable, str(BIN / "channel_outbox.py"), "enqueue", "--kind",
                            "notify", "--text", text, "--source", "update-check"],
                           capture_output=True, text=True, env=env, timeout=15)
        else:
            subprocess.run([sys.executable, str(BIN / "telegram.py"), "send", "--text", text,
                            "--kind", "notify"], capture_output=True, text=True, env=env, timeout=20)
        logging.info("update notice %s: %s", "queued" if via_outbox else "sent", summary)
    except subprocess.SubprocessError as exc:
        logging.warning("update notice failed: %s", exc)
        return  # don't snooze on send failure -> retry next interval
    try:  # dedupe this version (a newer release still breaks through update_check.should_prompt)
        subprocess.run([sys.executable, str(BIN / "update_check.py"), "snooze", "--days",
                        str(UPDATE_NOTICE_SNOOZE_DAYS)], capture_output=True, text=True,
                       env=env, timeout=15)
    except subprocess.SubprocessError:
        pass


def buy_peek(env: dict) -> dict:
    """Cheap, non-LLM probe (bin/buy_peek.py): is there a want needing a buy step (search/liaise)?
    {pending:0} on any error — fail-open; the force_buy_pass_every safety net covers a missed signal."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "buy_peek.py")],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode != 0:
            return {"pending": 0, "latest_text": "", "want_id": None}
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        logging.warning("buy_peek error: %s", exc)
        return {"pending": 0, "latest_text": "", "want_id": None}


def load_channel() -> dict:
    """The bound seller-channel adapter and its non-secret detail (handle/phone_id).
    Defaults to telegram so existing deployments are unchanged."""
    if not SELLER_CONFIG_PATH.exists():
        return {"adapter": "telegram", "detail": {}}
    ch = json.loads(SELLER_CONFIG_PATH.read_text()).get("channel", {})
    return {"adapter": ch.get("adapter", "telegram"), "detail": ch.get("detail", {})}


def _peek_cmd(channel: dict, timeout: int) -> list[str] | None:
    """Build the non-consuming peek command for the bound adapter, or None if it has no daemon."""
    adapter = channel["adapter"]
    if adapter == "telegram":
        return [sys.executable, str(BIN / "telegram.py"), "peek", "--timeout", str(timeout)]
    if adapter == "imessage":
        handle = channel["detail"].get("handle", "")
        return [sys.executable, str(BIN / "imessage.py"), "peek", "--handle", handle]
    if adapter == "whatsapp":
        return [sys.executable, str(BIN / "whatsapp.py"), "peek"]
    return None  # console has no daemon


def ensure_token(env: dict) -> dict:
    """telegram.py needs TELEGRAM_BOT_TOKEN; if not in env, ask the active harness to read it back
    from wherever it stored secrets (claude: settings.local.json `env`; codex: .codex/.env)."""
    if env.get("TELEGRAM_BOT_TOKEN"):
        return env
    try:
        harness = get_harness(os.environ.get("BAZAAR_HARNESS") or None)
    except UnknownHarness:
        return env
    for base in TOKEN_DIRS:
        tok = harness.load_env(base).get("TELEGRAM_BOT_TOKEN")
        if tok:
            return {**env, "TELEGRAM_BOT_TOKEN": tok}
    return env


def channel_peek(channel: dict, env: dict, timeout: int) -> dict:
    """Non-consuming peek on the bound adapter — {pending, latest_text} ({pending:0} on any error
    or for adapters with no daemon, e.g. console)."""
    cmd = _peek_cmd(channel, timeout)
    if cmd is None:
        return {"pending": 0, "latest_text": ""}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout + 15)
        if out.returncode != 0:
            logging.warning("peek failed rc=%s: %s", out.returncode, out.stderr.strip())
            return {"pending": 0, "latest_text": ""}
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        logging.warning("peek error: %s", exc)
        return {"pending": 0, "latest_text": ""}


def buyer_peek(env: dict) -> dict:
    """Cheap, non-LLM probe (bin/buyer_peek.py): is there a NEW buyer message on any enabled
    marketplace? Reads the warm CDP Chrome's unread badges (~0 tokens). {pending:0} on any error
    — fail-open, the safety-net pass below covers a missed signal. The buyer pass is the
    expensive part; this gate is what keeps it from firing every cycle on an empty inbox."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "buyer_peek.py")],
                             capture_output=True, text=True, env=env, timeout=30)
        if out.returncode != 0:
            logging.warning("buyer_peek failed rc=%s: %s", out.returncode, out.stderr.strip())
            return {"pending": 0, "latest_text": ""}
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        logging.warning("buyer_peek error: %s", exc)
        return {"pending": 0, "latest_text": ""}


def wake_mode() -> str:
    """'instant' if the macOS Notification Center is READABLE (Full Disk Access granted to this
    daemon's process), else 'standard'. The gating capability for the notification path; per-market
    activation is still empirical at runtime (trigger_resolver). Never raises."""
    try:
        return "instant" if notify_db.available() else "standard"
    except Exception:  # noqa: BLE001 — a self-check must never block startup
        return "standard"


def _log_wake_mode() -> None:
    """Log a clear one-line wake-mode banner at startup — explicit confirmation of whether the Full
    Disk Access grant took (so a restart tells the operator plainly: Instant vs Standard)."""
    if wake_mode() == "instant":
        logging.info("⚡ wake mode: INSTANT — Notification Center readable (FDA granted); "
                     "push-capable markets (FB/IG) wake on notifications, others poll")
    else:
        logging.info("\U0001f6e1️ wake mode: STANDARD polling — Full Disk Access not granted "
                     "(grant it for Instant: /bazaar -> speed). All markets use the cheap poll path")


def _register_bot_commands(env: dict) -> None:
    """Best-effort: register the Telegram "/" autocomplete menu (telegram.py setcommands).
    Idempotent (the shim hashes the command set and skips when unchanged), so this is cheap on
    every boot. Non-fatal — the daemon must never fail to start because the menu didn't register."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "telegram.py"), "setcommands"],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode == 0:
            logging.info("telegram command menu: %s", out.stdout.strip() or "ok")
        else:
            logging.warning("telegram setcommands failed (rc=%s): %s",
                            out.returncode, out.stderr.strip())
    except (subprocess.SubprocessError, OSError) as exc:
        logging.warning("telegram setcommands error: %s", exc)


def notify_trigger(env: dict) -> dict:
    """Notification-path trigger: which notification-path markets (trigger_resolver) have a NEW OS
    notification right now? Checked every loop iteration (cheap, ~0 tokens) so a push wakes the agent
    within one channel-poll cycle instead of waiting for the buyer poll. Fail-open: no Full Disk
    Access / no notifications → {pending:0}. Markets on the POLL path are untouched (buyer_peek owns
    them). Wraps notify_watch.watch; never raises."""
    try:
        return notify_watch.watch()
    except Exception as exc:  # noqa: BLE001 — never crash the loop on a notification-reader hiccup
        logging.warning("notify_trigger error: %s", exc)
        return {"pending": 0, "latest_text": "", "markets": {}}


def buyer_recheck(env: dict) -> dict:
    """Deterministic re-probe (bin/buyer_recheck.py): which enabled markets still show unread? Used
    to gate the FORCED buyer sweep so it costs ~0 tokens when inboxes are clear (Tier 2a).

    Fail-open CONSERVATIVELY (the opposite of buyer_peek): on any error report unhandled=1 so the
    caller STILL fires the LLM pass. A safety-net probe that fails open to 'clear' could skip a real
    buyer; failing open to 'unhandled' only costs one idempotent pass that finds nothing."""
    try:
        out = subprocess.run([sys.executable, str(BIN / "buyer_recheck.py")],
                             capture_output=True, text=True, env=env, timeout=30)
        if out.returncode != 0:
            logging.warning("buyer_recheck failed rc=%s: %s", out.returncode, out.stderr.strip())
            return {"unhandled": 1, "latest_text": "", "markets": {}}
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        logging.warning("buyer_recheck error: %s", exc)
        return {"unhandled": 1, "latest_text": "", "markets": {}}


def send_intent(channel: dict, env: dict, text: str, dry_run: bool) -> None:
    """Fast, contextual 'what I'll do next' line via a MCP-less haiku pass (no API key).
    Telegram-only (the intent-line plumbing sends via telegram.py); other adapters skip it and
    rely on the full pass, which messages via their own shim. Graceful: any failure skips the line."""
    if channel["adapter"] != "telegram":
        return
    if dry_run:
        logging.info("[dry-run] would send intent line for: %s", (text or "")[:40])
        return
    try:
        out = subprocess.run([str(BIN / "intent.sh"), text or "[message]"],
                             capture_output=True, text=True, env=env, timeout=25)
        line = out.stdout.strip()
        if out.returncode == 0 and line:
            # Tag the pre-ack as kind=intent so the next pass can tell it's a system ack, not a
            # considered answer it should treat as a prior [out] turn worth resolving against.
            subprocess.run([sys.executable, str(BIN / "telegram.py"), "send", "--text", line,
                            "--kind", "intent"], env=env, capture_output=True, timeout=15)
            logging.info("intent line: %s", line[:60])
        else:
            logging.info("intent skipped (rc=%s)", out.returncode)
    except subprocess.SubprocessError as exc:
        logging.warning("intent failed: %s", exc)


def _send_typing(channel: dict, env: dict) -> None:
    """Fire the native 'typing…' indicator (no LLM, instant). Telegram-only — iMessage/WhatsApp
    have no programmatic typing indicator, so this is a no-op there."""
    if channel["adapter"] != "telegram":
        return
    try:
        subprocess.run([sys.executable, str(BIN / "telegram.py"), "typing"],
                       env=env, capture_output=True, timeout=15)
    except subprocess.SubprocessError as exc:
        logging.warning("typing send failed: %s", exc)


def _touch_heartbeat() -> None:
    """Record a wall-clock tick so healthcheck can tell a loaded-but-WEDGED daemon from a live one.
    Written at the loop top and every ~4s while a pass runs, so only a truly stuck loop goes stale.
    Heartbeat IO must never crash the loop."""
    try:
        HEARTBEAT.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}))
    except OSError as exc:
        logging.debug("heartbeat write failed: %s", exc)


def reconcile_orphans(env: dict, dry_run: bool) -> None:
    """Heal crash orphans (Fix A) before a buyer/buy pass: a reply that landed on the marketplace but
    was never journaled (the Olaf split-brain). A cheap non-LLM subprocess that NEVER re-sends — it
    folds the orphan as unconfirmed, advances the cursor so the pass won't auto-resend, and asks the
    user to verify. Best-effort + fail-open: any error is swallowed so reconcile can never break the
    pass (the BUYER/BUY prompts also run it as their first step, the primary path)."""
    if dry_run:
        return
    try:
        subprocess.run([sys.executable, str(BIN / "journal_reconcile.py")],
                       env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("journal_reconcile error (non-fatal): %s", exc)


def _reply_committed(thread: dict, rec: dict) -> bool:
    """PURE: True if the thread ledger already reflects this intent's reply — so the queued intent is
    a STALE duplicate, not a lost send. Two deterministic signals:
      • the committed outbound row `out|<intent_id>` is in the transcript (commit folds it with that
        exact msg_id — see journal_send.py), or
      • the cursor has reached/passed the inbound this intent answers (last_handled_msg_id == in_msg_id,
        or last_handled_ts >= the intent's ts), meaning some reply handled it and the chat moved on.
    Either way the reply is delivered+journaled; escalating it would be the vida false alarm."""
    transcript = thread.get("transcript") or []
    marker = f"out|{rec.get('id')}"
    if any(isinstance(r, dict) and r.get("msg_id") == marker for r in transcript):
        return True
    cursor = thread.get("cursor") or {}
    if rec.get("in_msg_id") and cursor.get("last_handled_msg_id") == rec.get("in_msg_id"):
        return True
    handled_ts = thread_outbox.parse_iso(cursor.get("last_handled_ts"))
    intent_ts = thread_outbox.parse_iso(rec.get("ts"))
    return handled_ts is not None and intent_ts is not None and handled_ts >= intent_ts


def _intent_already_resolved(rec: dict) -> bool:
    """IO + fail-open: read the intent's thread file and ask _reply_committed. ANY uncertainty
    (missing file, unreadable, bad json) → False, so we never wrongly suppress a genuinely-stranded
    send — only a ledger we can positively read as delivered suppresses the alarm."""
    thread_id = rec.get("thread_id") or ""
    if not thread_id:
        return False
    try:
        path = thread_outbox.data_dir() / "threads" / f"{thread_id}.json"
        if not path.exists():
            return False
        thread = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return _reply_committed(thread, rec)


def plan_outbox_sweep(pending: list, now, leased_markets: set, redrive_after_sec: float,
                      escalate_attempts: int, escalate_after_sec: float) -> dict:
    """PURE (Track A5): decide what to do with each STRANDED still-pending send.

    For a pending intent whose market holds NO live lease (a live worker = in-flight, skip), which has
    aged past `redrive_after_sec` (so a normal pacing/in-flight wait is never mistaken for stranded),
    and which has NOT already been escalated to the user:
      • re-driven enough (attempts >= escalate_attempts) AND stranded long enough
        (age >= escalate_after_sec) → ESCALATE once (humbly — recovery has had its chance), else
      • RE-DRIVE its market (a re-drive triggers the recovery pass that can verify against the live chat).
    An intent already carrying the durable `escalated` marker is left visible and never re-driven or
    re-alarmed. Exactly-once lives on that marker, NOT the attempts counter, so re-drives (which bump
    attempts) can't race the escalate decision. Returns
    {"redrive": {market: [intent_id, ...]}, "escalate": [{thread_id, id, text, attempts}, ...]}.
    Deterministic order (input order) so the planner is unit-testable."""
    redrive: dict = {}
    escalate: list = []
    for rec in pending:
        market = rec.get("market")
        if market and market in leased_markets:
            continue  # a live worker owns this market — the intent is in-flight, not stranded
        if rec.get("escalated"):
            continue  # already surfaced once — never re-alarm or hot-loop
        ts = thread_outbox.parse_iso(rec.get("ts"))
        age = (now - ts).total_seconds() if ts is not None else float("inf")
        if age < redrive_after_sec:
            continue  # still within a normal pacing/in-flight window
        attempts = int(rec.get("attempts", 0) or 0)
        if attempts >= escalate_attempts and age >= escalate_after_sec:
            escalate.append({"thread_id": rec.get("thread_id", ""), "id": rec.get("id"),
                             "text": rec.get("text", ""), "attempts": attempts})
        else:
            redrive.setdefault(market, []).append(rec.get("id"))  # keep re-driving until both gates pass
    return {"redrive": redrive, "escalate": escalate}


def _live_market_leases(markets) -> set:
    """The subset of `markets` that currently hold a live (non-stale) lease — a worker is actively
    driving them. Uses the supervisor's canonical 600s TTL so the window matches reconcile/supervisor
    exactly. Fail-open: an unreadable lease is treated as not-live (the age backstop still guards)."""
    live = set()
    base = thread_outbox.data_dir()
    for m in {x for x in markets if x}:
        try:
            if lease.status(base, f"market:{m}", ttl=lease.AGENT_MARKET_TTL_SEC).get("held"):
                live.add(m)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return live


def _enqueue_outbox_escalation(esc: dict, env: dict) -> None:
    """Surface a long-stranded queued reply to the user (never silent) — HONESTLY. The sweeper cannot
    read the live chat, so it must NOT assert the send failed: the reply may well already be sitting in
    the chat (sent by a pass that died before journaling), and the recovery pass usually confirms that.
    So the wording states uncertainty, not failure. Enqueues a control-channel notify on channel_outbox
    (drained by the supervisor's _drain_outbox / the single-flight drain). Fail-open."""
    thread_id = esc.get("thread_id") or "a chat"
    text = (f"A queued reply on {thread_id} hasn't been confirmed delivered yet — it may already have "
            f"sent (I'm still verifying it in the chat). I'll keep checking; please glance at that "
            f"chat or re-send manually if it's urgent.")
    try:
        import channel_outbox
        path = channel_outbox.data_dir() / "channel_outbox.jsonl"
        channel_outbox.run_enqueue("notify", text, datetime.now(timezone.utc), path,
                                   source="outbox-sweep")
    except (OSError, ValueError) as exc:
        logging.warning("outbox escalation enqueue failed (non-fatal): %s", exc)


def sweep_outbox(env: dict, dry_run: bool, busy_markets: set | None = None, now=None) -> set:
    """Deterministic, non-LLM backstop (Track A5): re-drive STRANDED never-fired sends and escalate
    ones stuck past the attempt ceiling. Returns the set of markets to launch a scoped re-drive for
    (the caller uses its own buyer-launch path). Bumps each handled intent's attempts (thread_outbox
    .fail) so the loop is BOUNDED — a perpetually-failing send escalates once and stops hot-looping.
    Fail-open everywhere: any error returns an empty set so the sweep never breaks the daemon loop."""
    if dry_run:
        return set()
    try:
        pending = thread_outbox.peek(statuses=(thread_outbox.STATUS_PENDING,))["pending"]
    except (OSError, ValueError, KeyError):
        return set()
    if not pending:
        return set()
    now = now or datetime.now(timezone.utc)
    # Ledger-resolution prefilter: drop intents the thread file already shows delivered+journaled
    # (recovery committed it). Acking the stale record here is what stops the vida false alarm — the
    # planner never sees a resolved intent, so it can't re-drive or escalate it.
    survivors: list = []
    resolved = 0
    for rec in pending:
        if _intent_already_resolved(rec):
            try:
                thread_outbox.ack(rec.get("id"))
            except (OSError, ValueError):
                pass
            resolved += 1
            continue
        survivors.append(rec)
    if not survivors:
        if resolved:
            logging.info("outbox sweep: %s stranded intent(s) already delivered — acked, no alarm",
                         resolved)
        return set()
    leased = set(busy_markets or set()) | _live_market_leases({r.get("market") for r in survivors})
    plan = plan_outbox_sweep(survivors, now, leased, OUTBOX_REDRIVE_AFTER_SEC,
                             OUTBOX_ESCALATE_ATTEMPTS, OUTBOX_ESCALATE_AFTER_SEC)
    for esc in plan["escalate"]:
        _enqueue_outbox_escalation(esc, env)
        try:
            thread_outbox.mark_escalated(esc["id"])  # durable exactly-once marker (not the attempts race)
        except (OSError, ValueError):
            pass
    for _market, ids in plan["redrive"].items():
        for iid in ids:
            try:
                thread_outbox.fail(iid)  # bump attempts → bounded re-drive count toward the escalate gate
            except (OSError, ValueError):
                pass
    redrive_markets = set(plan["redrive"])
    if redrive_markets or plan["escalate"] or resolved:
        logging.info("outbox sweep: re-drive markets=%s, escalate=%s, already-delivered=%s",
                     sorted(redrive_markets), len(plan["escalate"]), resolved)
    return redrive_markets


def drain_channel_outbox(channel: dict, env: dict, dry_run: bool) -> None:
    """Flush queued control-channel notices (channel_outbox) to the bound adapter, in order. The
    single-flight loop has no background workers but components (e.g. the outbox sweeper) still
    enqueue notices, so it must drain too. Telegram only (other adapters' notices wait). Fail-open."""
    if dry_run or channel.get("adapter") != "telegram":
        return
    try:
        out = subprocess.run([sys.executable, str(BIN / "channel_outbox.py"), "peek"],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode != 0:
            return
        pending = json.loads(out.stdout).get("pending", [])
    except (subprocess.SubprocessError, ValueError):
        return
    for rec in pending:
        cmd = [sys.executable, str(BIN / "telegram.py"), "send", "--text", rec.get("text", ""),
               "--kind", rec.get("kind", "notify")]
        if rec.get("ref"):
            cmd += ["--ref", str(rec["ref"])]
        try:
            sent = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=25)
        except subprocess.SubprocessError:
            continue  # a poison notice is retried next drain; the rest still flush
        if sent.returncode == 0:
            try:
                subprocess.run([sys.executable, str(BIN / "channel_outbox.py"), "ack", "--id", rec["id"]],
                               capture_output=True, text=True, env=env, timeout=15)
            except subprocess.SubprocessError:
                pass


def run_pass(mode: str, channel: dict, env: dict, dry_run: bool,
             extra_env: dict | None = None) -> int:
    """Invoke the LLM pass (run_pass.sh seller|buyer) under the single-flight lock.
    For SELLER passes, pulse the native 'typing…' indicator every ~4s while it runs — instant,
    no LLM, covers the claude -p cold start + working time. The assistant authors all actual text.
    extra_env seeds per-pass hints (e.g. the buyer_peek snippet) into the pass's environment.

    Returns the pass exit code (0 on dry-run / a held lock — nothing ran, so nothing to continue).
    A buyer pass killed at the turn cap returns CAP_HIT_SIGNAL (harness_run maps it), which the buyer
    call site turns into ONE bounded continuation (Fix C)."""
    pulse = mode in ("seller", "channel")  # the channel pass is the one the user is waiting on
    pass_env = {**env, **(extra_env or {})}
    if dry_run:
        logging.info("[dry-run] would run %s pass (typing pulse=%s, hints=%s)",
                     mode, pulse, list((extra_env or {}).keys()))
        return 0
    if RUN_LOCK.exists():
        logging.info("run lock held — skipping %s pass this tick", mode)
        return 0
    # Check-then-write is not atomic, but it is never raced: a single daemon instance is guaranteed
    # by INSTANCE_LOCK (fcntl), and run_pass is only ever called sequentially from that one loop.
    RUN_LOCK.write_text(str(os.getpid()))
    try:
        logging.info("running %s pass…", mode)
        # start_new_session=True → the pass leads its own process group, so proc_tree.confirm_dead
        # can signal the WHOLE tree (run_pass.sh → harness_run → the `claude` grandchild that drives
        # the tab). Without it, a preempt/timeout would SIGTERM only the wrapper and orphan claude,
        # leaving it acting on the live account after RUN_LOCK is released (the supervisor's CRITICAL
        # bug, now fixed here too). Every forced exit confirms the tree dead BEFORE the finally unlink.
        proc = subprocess.Popen([str(BIN / "run_pass.sh"), mode], env=pass_env, cwd=str(SELLER_DIR),
                                start_new_session=True)
        deadline = time.monotonic() + 900
        while proc.poll() is None:
            _touch_heartbeat()  # keep the heartbeat fresh during a long pass (proves the loop lives)
            # MID-FLIGHT INTERRUPT: a pause set via the CLI/slash command or a prior tick stops the
            # running pass within ~one poll cadence (idempotent — cursors/pacing make the killed step
            # safe to re-run; it won't, until /resume). This covers ALL modes, incl. the seller pass.
            if control.is_paused():
                logging.info("paused mid-%s pass → stop tree (idempotent; resumes after /resume)", mode)
                proc_tree.confirm_dead(proc)
                break
            if pulse:
                _send_typing(channel, env)
            elif mode in ("buyer", "buy", "maint"):
                peeked = channel_peek(channel, env, 2)
                if peeked["pending"] > 0:
                    if peeked.get("latest_text", "").strip().startswith("/pause"):
                        # Telegram /pause arriving DURING a long background pass → set the flag and
                        # stop the tree now (the next loop iteration's drain consumes + acks the command).
                        logging.info("/pause during %s pass → set flag + stop tree", mode)
                        control.pause(source="telegram")
                        _send_typing(channel, env)
                        proc_tree.confirm_dead(proc)
                        break
                    # seller is waiting → fire typing now and PREEMPT this background pass (idempotent;
                    # it resumes next cycle). The channel pass runs on the next loop iteration.
                    logging.info("seller message during %s pass → typing + preempt", mode)
                    _send_typing(channel, env)
                    proc_tree.confirm_dead(proc)
                    break
            try:
                proc.wait(timeout=4)          # ~4s cadence; returns early when the pass finishes
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                logging.error("%s pass exceeded 900s — killing tree", mode)
                proc_tree.confirm_dead(proc)
                break
        logging.info("%s pass done rc=%s", mode, proc.returncode)
        return proc.returncode
    finally:
        RUN_LOCK.unlink(missing_ok=True)  # safe: every forced break above already confirmed the tree dead


def peek_thread_from(bp: dict) -> str | None:
    """PURE (C-followup): the conservative single-thread hint derived from a buyer_peek RESULT — the
    single fresh tracked-sell thread when there is EXACTLY one across all markets, else None (0 → a
    fresh enquiry or nothing; >1 → ambiguous). Reading the hint from the SAME peek that drove the
    freshness gate means the SELL memo is advanced ONCE per cycle (buyer_peek.peek already advanced
    it); a second probe here would see the advanced memo and null the hint. Fail-open to None on any
    malformed/old-shape peek so a missing hint just yields an unscoped (market-only) pass."""
    try:
        markets = (bp or {}).get("markets") or {}
        threads = [t for info in markets.values() for t in ((info or {}).get("sell_threads") or [])]
        return threads[0] if len(threads) == 1 else None
    except Exception:  # noqa: BLE001 — a pure hint derivation must never crash the loop
        return None


def buyer_peek_thread(env: dict) -> str | None:
    """Conservative priority-hint thread for the UNSCOPED single-flight buyer pass: the single fresh
    tracked-sell thread when there is EXACTLY one across all enumerable markets; otherwise None (0 →
    a fresh enquiry or nothing; >1 → ambiguous). Under-hinting is deliberate — scoping to one of
    several threads risks the pass fixating on it and missing the rest; mis-routing is the worst
    outcome. Fail-open to None: a probe hiccup just yields an unscoped (market-only) pass.

    NOTE: this advances the SELL memo (a fresh inbox_scan.sell_threads_new peek). The POLL path must
    NOT call this AFTER buyer_peek() already advanced the memo (that double-advance nulls the hint —
    C-followup); it derives the hint from the peek result via peek_thread_from() instead. This helper
    remains for the NOTIFICATION path, where no prior SELL _peek ran this cycle."""
    try:
        import inbox_scan  # lazy: keeps the daemon import-side-effect-free + avoids any cycle
        threads = [t for ids in (inbox_scan.sell_threads_new() or {}).values() for t in (ids or [])]
        return threads[0] if len(threads) == 1 else None
    except Exception as exc:  # noqa: BLE001 — a hint probe must never crash the loop
        logging.warning("buyer_peek_thread probe error: %s", exc)
        return None


def buyer_continuation_action(rc: int, attempts: int, retry_cap: int) -> str:
    """PURE: what to do after a buyer pass exits with code `rc`, given `attempts` continuations so far.

      'continue' — the pass was killed at the turn cap (rc == CAP_HIT_SIGNAL) and the retry budget is
                   not yet spent → run ONE more bounded continuation.
      'escalate' — capped, but the retry budget is exhausted → surface it over the channel.
      'none'     — a clean exit (rc 0) or a GENERIC failure (any other rc) → NOT our loop; do nothing
                   (a generic failure is handled by the normal idempotent next-pass retry, not here)."""
    if rc != CAP_HIT_SIGNAL:
        return "none"
    return "continue" if attempts < retry_cap else "escalate"


def escalate_cap_hit(channel: dict, env: dict, resource: str, dry_run: bool,
                     *, via_outbox: bool) -> None:
    """ESCALATE a market that keeps hitting the turn cap, over the control channel. Never silently
    drop a stranded backlog. Fail-open — an error is logged, not raised.

    Bug D1 — two delivery modes, mirroring check_and_notify_update:
      • via_outbox=True  → ENQUEUE a `notify` on channel_outbox (the concurrent supervisor's single
        writer drains it). Used only where a drainer exists.
      • via_outbox=False → DIRECT-send via telegram.py (adapter-gated). The SINGLE-FLIGHT loop has no
        outbox drainer, so an enqueue there would be written and NEVER sent — the escalation must
        direct-send instead (exactly the via_outbox=False branch the update notice uses)."""
    text = (f"⚠️ buyer:{resource} keeps hitting the turn cap after {CONTINUATION_RETRY_CAP} "
            f"continuations; its backlog may be stranded. Open {resource} and check for unread "
            f"buyer messages.")
    if dry_run:
        logging.info("[dry-run] would escalate cap-hit for %s (via_outbox=%s)", resource, via_outbox)
        return
    if via_outbox:
        try:
            # In-process enqueue — channel_outbox.data_dir() reads BAZAAR_DATA_DIR, so it lands in the
            # same outbox the supervisor's _drain_outbox flushes.
            import channel_outbox
            from datetime import datetime, timezone
            path = channel_outbox.data_dir() / "channel_outbox.jsonl"
            channel_outbox.run_enqueue("notify", text, datetime.now(timezone.utc), path,
                                       source="cap-hit")
            logging.warning("buyer:%s exhausted continuation budget → escalation queued", resource)
        except (OSError, ValueError) as exc:
            logging.error("cap-hit escalation enqueue failed for %s: %s", resource, exc)
        return
    # DIRECT send (single-flight). Adapter-gated exactly like the notify/update path: only telegram
    # has one-shot send wired here; other adapters surface it via their own pass, not here.
    if channel.get("adapter") != "telegram":
        logging.info("cap-hit for %s but adapter=%s direct send not wired; skipping",
                     resource, channel.get("adapter"))
        return
    try:
        subprocess.run([sys.executable, str(BIN / "telegram.py"), "send", "--text", text,
                        "--kind", "notify"], capture_output=True, text=True, env=env, timeout=20)
        logging.warning("buyer:%s exhausted continuation budget → escalated over channel (direct)",
                        resource)
    except subprocess.SubprocessError as exc:
        logging.error("cap-hit escalation direct send failed for %s: %s", resource, exc)


def run_buyer_with_continuation(resource: str, channel: dict, env: dict, dry_run: bool,
                                extra_env: dict | None = None, peek_thread: str | None = None) -> int:
    """Run a buyer pass, and if it was killed at the turn cap, run exactly ONE bounded continuation
    per cap-hit (up to CONTINUATION_RETRY_CAP) before ESCALATING. The retry guard makes this loop
    finite — a perpetually-capping market escalates ONCE and stops, never hot-loops. Each continuation
    re-runs `buyer` for the same `resource`; the priority-hint thread (peek_thread) rides along if set.

    `resource` "" means an unscoped buyer pass (single-flight default) — the continuation re-runs
    unscoped too. Returns the LAST pass's exit code."""
    base_env = dict(extra_env or {})
    if peek_thread:
        base_env["BAZAAR_BUYER_PEEK_THREAD"] = peek_thread
    attempts = 0
    rc = run_pass("buyer", channel, env, dry_run, extra_env={**base_env, "BAZAAR_RESOURCE": resource}
                  if resource else base_env)
    while True:
        action = buyer_continuation_action(rc, attempts, CONTINUATION_RETRY_CAP)
        if action == "escalate":
            # Bug D1: single-flight has no outbox drainer → DIRECT-send (via_outbox=False), mirroring
            # the update-notice path. An enqueue here would never be delivered.
            escalate_cap_hit(channel, env, resource or "buyer", dry_run, via_outbox=False)
            return rc
        if action != "continue":
            return rc
        attempts += 1
        logging.info("buyer pass hit the turn cap → bounded continuation #%s (%s)",
                     attempts, resource or "all markets")
        reconcile_orphans(env, dry_run)  # heal any crash orphan before re-touring (best-effort, no resend)
        rc = run_pass("buyer", channel, env, dry_run,
                      extra_env={**base_env, "BAZAAR_RESOURCE": resource} if resource else base_env)


def _acquire_instance_lock() -> dict:
    """Process-lifetime SINGLETON (Fix D: thin wrapper over instance_lock.acquire).

    Only one agent_daemon may run: the concurrent supervisor's heartbeat-TTL lease liveness assumes a
    single heartbeater, and a second consumer also fights the Telegram offset. Returns the full
    instance_lock.acquire dict — {acquired, holder_pid, holder_alive, reclaimed, fd}. The caller keeps
    res["fd"] referenced for the process lifetime (the OS frees the flock on crash/exit). A lock left
    by a DEAD pid is RECLAIMED, not refused (no respawn storm); a LIVE duplicate is refused with
    truthful holder info so main() can log INFO + exit 0."""
    return instance_lock.acquire(INSTANCE_LOCK, HEARTBEAT)


def _clear_instance_lock() -> None:
    """Bug D3: on a CLEAN shutdown, clear the instance-lock body so the watchdog can't be fooled.

    The watchdog reads the lock-body PID to decide liveness. After a clean exit (which KeepAlive
    won't restart) the body still names our now-dead PID; if the OS recycles that PID to an unrelated
    live process, the watchdog thinks the daemon is alive and never restarts it (a silent stay-down).
    instance_lock.clear_holder empties the body ONLY if WE are the recorded holder, so the watchdog
    then sees no holder (correctly treated as not-alive → restart). Best-effort + fail-open — a
    failure to clear must never turn a clean shutdown into an error."""
    try:
        instance_lock.clear_holder(INSTANCE_LOCK)
    except Exception as exc:  # noqa: BLE001 — clearing is non-critical; never break a clean exit
        logging.debug("instance-lock clear on exit failed: %s", exc)


def main(argv) -> int:
    p = argparse.ArgumentParser(prog="agent_daemon.py")
    p.add_argument("--once", action="store_true", help="run one iteration then exit")
    p.add_argument("--dry-run", action="store_true", help="log decisions; don't invoke claude")
    p.add_argument("--peek-timeout", type=int, default=None)
    ns = p.parse_args(argv[1:])

    setup_logging()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    _lock = _acquire_instance_lock()
    if not _lock["acquired"]:
        # A LIVE duplicate already holds the lock. Log INFO (not ERROR) + exit 0 (a CLEAN exit) so
        # KeepAlive={SuccessfulExit:false} does NOT respawn us — this is what stops the ~2-minute
        # ERROR storm in logs/daemon.log where colliding respawns spammed ERROR + exited rc=3.
        logging.info("agent_daemon already running (pid=%s, alive=%s) — exiting",
                     _lock["holder_pid"], _lock["holder_alive"])
        return 0
    _instance_lock = _lock["fd"]  # noqa: F841 — held for the process lifetime (OS frees on exit)
    if _lock["reclaimed"]:
        logging.info("reclaimed a stale instance lock (previous holder pid was dead) — starting fresh")
    RUN_LOCK.unlink(missing_ok=True)  # clear any stale lock from a hard crash

    # Bug D3: once WE hold the lock, guarantee a clean-exit clear of the lock body so a recycled
    # PID can never fool the watchdog into a silent stay-down. The finally covers every exit path
    # below (the rc=3 early returns, the supervisor handoff, and the loop's own clean exit).
    try:
        cfg = load_config()
        channel = load_channel()
        env = ensure_token(dict(os.environ))
        peek_timeout = ns.peek_timeout if ns.peek_timeout is not None else cfg["peek_timeout"]
        if channel["adapter"] == "telegram" and not env.get("TELEGRAM_BOT_TOKEN"):
            logging.error("TELEGRAM_BOT_TOKEN not found (env or settings.local.json) — exiting")
            return 3
        if channel["adapter"] == "console":
            logging.error("channel.adapter=console has no daemon — use the interactive /sell session")
            return 3

        # Phase 3 (opt-in): with max_concurrent_workers > 1, hand off to the concurrent supervisor
        # (parallel sell-inbox workers across marketplaces). Default (1) falls through to the proven
        # single-flight loop below — completely unchanged.
        if cfg.get("max_concurrent_workers", 1) > 1:
            import supervisor  # lazy: avoids an import cycle (supervisor imports this module)
            return supervisor.run(cfg, channel, env, ns, cfg["max_concurrent_workers"], peek_timeout)

        logging.info("daemon up · adapter=%s · buyer_poll=%ss · buy_poll=%ss · maint_poll=%ss · "
                     "peek_timeout=%ss · dry_run=%s · paused=%s", channel["adapter"], cfg["buyer_poll_sec"],
                     cfg["buy_poll_sec"], cfg["maint_poll_sec"], peek_timeout, ns.dry_run,
                     control.is_paused())  # a file-based pause survives a daemon restart
        _log_wake_mode()  # explicit Instant/Standard banner so the operator knows if the FDA grant took
        # Bug C6: sweep per-pass logs leaked by forced kills (the pause/seller-message force-break, the
        # 900s deadline — all skip run_pass's finally in the pass tree). Startup glob-and-unlink backstop.
        _swept = harness_run.sweep_stale_pass_logs()
        if _swept:
            logging.info("swept %s stale per-pass log(s) leaked by forced kills", len(_swept))
        if channel["adapter"] == "telegram":
            _register_bot_commands(env)  # populate the "/" autocomplete menu (idempotent, non-fatal)
        last_buyer = time.monotonic() - cfg["buyer_poll_sec"]  # make a buyer pass due immediately
        last_buyer_pass = time.monotonic()                     # when an actual buyer PASS last ran (time floor)
        last_buy = time.monotonic() - cfg["buy_poll_sec"]      # and a buy pass
        last_maint = time.monotonic() - cfg["maint_poll_sec"]  # and a maintenance pass
        last_eval = time.monotonic()                           # self-eval check throttle (not immediate)
        last_update = time.monotonic()                          # upstream update-check throttle (not immediate)
        last_followup = time.monotonic()                        # stale-chat follow-up check throttle (not immediate)
        last_sweep = time.monotonic()                           # outbox sweeper throttle (Track A5; not immediate)
        empty_peeks = 0  # consecutive buyer peeks that found nothing new (drives the safety net)
        empty_buys = 0   # consecutive buy peeks that found nothing actionable (buy safety net)
        _was_paused = False  # edge-triggered PAUSED/RESUMED logging (the loop runs ~1/sec; don't spam)
        src_fp = _source_fingerprint()  # exit cleanly when our own code changes → launchd respawns fresh
        while not _stop:
            iter_start = time.monotonic()  # Fix D: time each iteration so a wedged one is VISIBLE (WARN)
            # A code change to the daemon's own sources only takes effect on restart (no hot-reload), so
            # bounce here at a between-pass boundary (no pass runs at loop top — run_pass is synchronous).
            if _source_fingerprint() != src_fp:
                logging.info("daemon source changed → exiting to reload (launchd will respawn on fresh code)")
                relaunch_self()  # clean single atomic restart on fresh code (no unload+load double-spawn)
                break
            _touch_heartbeat()  # one tick per loop iteration; healthcheck WARNs if this goes stale
            # Read the pause flag ONCE per iteration. While paused, the daemon still peeks the channel
            # (so /resume and corrections get received) but takes NO marketplace action: the channel
            # path runs the deterministic, no-LLM drain instead of a seller pass, and the three
            # background gates are skipped. A file-based flag → any interface can set it.
            paused = control.is_paused()
            if paused != _was_paused:
                if paused:
                    logging.info("daemon PAUSED — channel peek continues; action passes held until /resume")
                else:
                    logging.info("daemon RESUMED — %s correction(s) queued for the next pass",
                                 len(control.pending_corrections()))
                _was_paused = paused
            # Keep notification-path (Meta) tabs hidden during the inter-pass wait so their OS push
            # keeps firing — a focused Meta tab delivers in-app and suppresses the readable push. The
            # warm Chrome is a dedicated instance, so this never touches the user's own browser.
            if not paused:
                tab_park.park()
            peek = channel_peek(channel, env, peek_timeout)
            if peek["pending"]:
                _send_typing(channel, env)                                  # instant native 'typing…'
                if paused:
                    # Deterministic, ~$0: consume /resume + capture corrections + ack. No claude -p,
                    # no browser. Leaving the offset un-advanced would deadlock /resume, so we MUST drain.
                    logging.info("paused: %s pending → deterministic control drain (no LLM)", peek["pending"])
                    if not ns.dry_run:
                        subprocess.run([sys.executable, str(BIN / "channel_control.py"), "drain"],
                                       env=env, capture_output=True, timeout=60)
                else:
                    mid_listing = _listing_active()  # skip the redundant intent line during a listing wizard
                    logging.info("%s: %s pending → typing%s + seller pass",
                                 channel["adapter"], peek["pending"],
                                 "" if mid_listing else " + intent")
                    if not mid_listing:
                        send_intent(channel, env, peek.get("latest_text", ""), ns.dry_run)  # ~6s (TG only)
                    run_pass("seller", channel, env, ns.dry_run)            # full pass: work + report
            # NOTIFICATION-PATH trigger (checked every iteration, ~0 tokens): a market whose path
            # resolves to "notification" (e.g. FB web push) wakes the agent the moment its OS
            # notification lands, instead of waiting for the buyer poll. Poll-path markets (e.g.
            # Carousell) are untouched and fall through to the buyer gate below. Idempotent: notify_watch
            # advances its per-market cursor, and the per-thread cursors dedupe within the pass.
            if not paused:
                nt = notify_trigger(env)
                if nt.get("pending"):
                    logging.info("notification trigger → buyer pass: %s", nt.get("latest_text", "")[:70])
                    reconcile_orphans(env, ns.dry_run)  # heal crash orphans first (best-effort, no resend)
                    run_buyer_with_continuation(
                        "", channel, env, ns.dry_run,
                        extra_env={"BAZAAR_BUYER_PEEK_TEXT": nt.get("latest_text", "")},
                        peek_thread=buyer_peek_thread(env))
                    # Deliberately do NOT touch last_buyer / last_buyer_pass here. Those drive the
                    # AGGREGATE poll gate + strand-floor for ALL markets; resetting them on a per-market
                    # notification (FB) would starve the poll path that backstops the OTHER markets
                    # (Carousell) and any FB message a notification misses. The poll runs independently
                    # on its own cadence and remains the fallback. (notify_watch's own cursor dedupes.)
            if not paused and time.monotonic() - last_buyer >= cfg["buyer_poll_sec"]:
                # GATE the expensive buyer pass behind a cheap, non-LLM unread probe. Only spend a
                # full LLM browser pass when a buyer actually wrote — or, as a safety net, every Nth
                # empty peek so a flaky unread signal can't strand a buyer.
                bp = buyer_peek(env)
                floor_sec = cfg["force_buyer_sweep_hours"] * 3600
                forced, force_reason = buyer_force_due(
                    empty_peeks, cfg["force_buyer_pass_every"],
                    time.monotonic() - last_buyer_pass, floor_sec)
                floor_due = floor_sec > 0 and (time.monotonic() - last_buyer_pass) >= floor_sec
                # Spend the ~0-token recheck ONLY on a count-net force (not on a real peek hit, where we
                # already know there is mail, nor on a floor force, which deliberately fires an actual
                # pass as the strand backstop). This is what turns the old forced empty LLM sweep free.
                recheck_unhandled, recheck_text = None, ""
                if forced and not floor_due and not bp.get("pending"):
                    rc = buyer_recheck(env)
                    recheck_unhandled, recheck_text = rc.get("unhandled", 0), rc.get("latest_text", "")
                action = buyer_action(bp.get("pending", 0), forced, floor_due, recheck_unhandled)
                if action == "pass":
                    hint = bp.get("latest_text", "") or recheck_text
                    reason = (f"{bp['pending']} new" if bp.get("pending")
                              else force_reason if floor_due
                              else f"recheck: {recheck_unhandled} unhandled")
                    logging.info("buyer pass → %s", reason)
                    reconcile_orphans(env, ns.dry_run)  # heal crash orphans first (best-effort, no resend)
                    # C-followup: derive the peek-thread hint from THIS peek's result (bp already advanced
                    # the SELL memo). Calling buyer_peek_thread(env) here would advance it a SECOND time
                    # and the now-stale memo would null the hint. A forced/recheck sweep carries no precise
                    # sell_threads, so peek_thread_from falls back to None (unscoped) — today's behavior.
                    run_buyer_with_continuation(
                        "", channel, env, ns.dry_run,
                        extra_env={
                            "BAZAAR_BUYER_PEEK_TEXT": hint,
                            "BAZAAR_BUYER_PEEK_FORCED": "1" if not bp.get("pending") else "",
                        },
                        peek_thread=peek_thread_from(bp))
                    last_buyer_pass = time.monotonic()
                elif action == "skip":
                    logging.info("buyer recheck: all inboxes clear → skip forced pass (~0 tokens)")
                else:  # idle
                    empty_peeks += 1
                    logging.info("buyer peek: nothing new (%s consecutive) → skip pass", empty_peeks)
                if action != "idle":
                    empty_peeks = 0
                last_buyer = time.monotonic()

            # OUTBOX SWEEP (Track A5): re-drive any STRANDED never-fired send (status=pending in
            # thread_outbox that no live worker owns) — the deterministic backstop that makes a silent
            # drop impossible even if no inbound mail triggers a buyer pass. Escalates ones stuck past
            # the attempt ceiling (enqueued to channel_outbox; drained just below).
            if not paused and time.monotonic() - last_sweep >= cfg["outbox_sweep_poll_sec"]:
                for market in sorted(sweep_outbox(env, ns.dry_run, busy_markets=set())):
                    reconcile_orphans(env, ns.dry_run)  # surfaces needs_resend; the pass then re-sends
                    logging.info("outbox sweep → re-drive buyer pass [%s]", market)
                    run_buyer_with_continuation(
                        market, channel, env, ns.dry_run,
                        extra_env={"BAZAAR_BUYER_PEEK_TEXT": "re-driving a reply that never sent"})
                drain_channel_outbox(channel, env, ns.dry_run)  # deliver any sweep escalation
                last_sweep = time.monotonic()

            # MAINTENANCE (§2b detect): drain an active distribution/inbox-takeover batch one step per
            # pass, or start a cadence-due my-listings SCAN and/or inbox SWEEP. Gated by cheap non-LLM
            # probes so the LLM only runs when there's work (never interrupts an active listing wizard).
            if not paused and time.monotonic() - last_maint >= cfg["maint_poll_sec"]:
                dist_active = _distribution_active()
                idet_active = _inbox_detect_active()
                lh_active = _listing_health_session_active()
                scan_due = _scan_due(env)
                sweep_due = _inbox_sweep_due(env)
                # Stale-listing suggestions are the LOWEST-priority maint step: only start a new episode
                # when no higher-priority detect/drain work is pending, so it never preempts a live wizard
                # or an in-flight batch (one item per pass; rate-limited by listing_health_interval_hours).
                if not (dist_active or idet_active or lh_active or scan_due or sweep_due):
                    lh_due_item = _listing_health_due(env)
                    if lh_due_item:
                        run_listing_health_start(env, lh_due_item, ns.dry_run)
                        lh_active = not ns.dry_run  # dry-run writes no session, so it stays inactive
                if dist_active or idet_active or lh_active or scan_due or sweep_due:
                    reason = ("drain distribution batch" if dist_active else
                              "drain inbox-takeover batch" if idet_active else
                              "suggest stale-listing fixes" if lh_active else "detect due")
                    logging.info("maint pass → %s", reason)
                    run_pass("maint", channel, env, ns.dry_run)
                last_maint = time.monotonic()

            # BUY SIDE (§3): pursue active wants — search/shortlist a `searching` want, or liaise a
            # `liaising`/`agreed` one. Gated by buy_peek (file-state only), with a periodic safety net.
            if not paused and time.monotonic() - last_buy >= cfg["buy_poll_sec"]:
                bpk = buy_peek(env)
                force_buy = cfg["force_buy_pass_every"]
                forced_buy = bool(force_buy) and empty_buys >= force_buy
                if bpk.get("pending") or forced_buy:
                    reason = bpk.get("latest_text") or f"safety-net after {empty_buys} empty peeks"
                    logging.info("buy pass → %s", reason)
                    reconcile_orphans(env, ns.dry_run)  # heal crash orphans first (best-effort, no resend)
                    run_pass("buy", channel, env, ns.dry_run, extra_env={
                        "BAZAAR_BUY_PEEK_WANT": bpk.get("want_id") or "",
                        "BAZAAR_BUY_PEEK_TEXT": bpk.get("latest_text", ""),
                    })
                    empty_buys = 0
                else:
                    empty_buys += 1
                    logging.info("buy peek: nothing actionable (%s consecutive) → skip pass", empty_buys)
                last_buy = time.monotonic()

            # STALE-CHAT FOLLOW-UPS: nudge counterparts who went quiet, then mark them not interested.
            # Detection is a cheap non-LLM probe; a DROP is $0 deterministic (mark + ONE channel notice);
            # a NUDGE reuses the buyer/buy pass via BAZAAR_FOLLOWUP=1 (same compose+send+journal bracket).
            # Reconcile first so a counterpart who just replied is dropped before we decide.
            if not paused and time.monotonic() - last_followup >= cfg["followup_poll_sec"]:
                run_followup_reconcile(env)
                fu = _followup_due(env)
                if fu.get("drops"):
                    run_followup_drops(env, ns.dry_run)
                if fu.get("nudges"):
                    sides = {d.get("side") for d in fu.get("due_nudges", [])}
                    reconcile_orphans(env, ns.dry_run)  # heal crash orphans before any send
                    if "sell" in sides:
                        logging.info("followup nudges due (sell) → buyer pass")
                        run_pass("buyer", channel, env, ns.dry_run, extra_env={"BAZAAR_FOLLOWUP": "1"})
                    if "buy" in sides:
                        logging.info("followup nudges due (buy) → buy pass")
                        run_pass("buy", channel, env, ns.dry_run, extra_env={"BAZAAR_FOLLOWUP": "1"})
                last_followup = time.monotonic()

            # NIGHTLY SELF-EVAL: on a slow throttle, check the cadence gate and run the eval if due. The
            # deterministic layer is always $0 (pure file reads/writes, no browser, no channel send); when
            # config.eval_judge_nightly is set (default on) the same run also fires the billed LLM judge.
            # Gated on `not paused` too so /pause means a literal full stop (no work of any kind).
            if not paused and time.monotonic() - last_eval >= cfg["eval_poll_sec"]:
                if _eval_due(env):
                    use_judge = cfg.get("eval_judge_nightly", True)
                    logging.info("self-eval due → running %s",
                                 "deterministic + LLM judge" if use_judge else "deterministic checks")
                    run_eval(env, ns.dry_run, use_judge)
                last_eval = time.monotonic()

            # UPSTREAM UPDATE CHECK (read-only, throttled): heads-up over the channel if a newer Bazaar
            # is available. Single-flight loop sends directly (no outbox drain here). Never auto-applies.
            if not paused and time.monotonic() - last_update >= cfg["update_poll_sec"]:
                check_and_notify_update(channel, env, ns.dry_run, via_outbox=False)
                last_update = time.monotonic()

            # Fix D — per-iteration stall guard (observability): a hung subprocess that froze the loop
            # for minutes (the incident's ~7-min stall) is surfaced as a WARN. Not a kill: run_pass owns
            # the 900s tree-kill, and the watchdog restarts a truly wedged loop via the stale heartbeat.
            stall = iteration_stall_warning(time.monotonic() - iter_start)
            if stall:
                logging.warning(stall)

            if ns.once:
                break
            time.sleep(1)  # peek already long-polled; brief yield
        logging.info("daemon stopping (clean)")
        RUN_LOCK.unlink(missing_ok=True)
        return 0
    finally:
        _clear_instance_lock()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
