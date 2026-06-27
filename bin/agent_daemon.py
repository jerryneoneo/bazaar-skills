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
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SELLER_DIR = Path(__file__).resolve().parent.parent
BIN = SELLER_DIR / "bin"
LOG_DIR = SELLER_DIR / "logs"
RUN_LOCK = SELLER_DIR / ".daemon.runlock"
INSTANCE_LOCK = SELLER_DIR / ".daemon.instancelock"
CONFIG_PATH = SELLER_DIR / "data" / "config.json"
SELLER_CONFIG_PATH = SELLER_DIR / "data" / "seller_config.json"

sys.path.insert(0, str(BIN))
from harnesses import UnknownHarness, get_harness  # noqa: E402  (local bin/harnesses package)
import control  # noqa: E402  the single source of truth for the pause flag (data/control.json)
import notify_watch  # noqa: E402  notification-path trigger (macOS Notification Center; fail-open)
import notify_db  # noqa: E402  FDA-gated Notification Center reader (startup wake-mode self-check)
import tab_park  # noqa: E402  keep Meta (notification-path) tabs hidden so their OS push keeps firing

# The runtime dir (beside the agent) or one level up (dev tree) — the harness reads its own secret
# store under whichever holds it, so the daemon never hardcodes settings.local.json.
TOKEN_DIRS = [SELLER_DIR, SELLER_DIR.parent]

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


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "daemon.log"), logging.StreamHandler()],
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
        # Nightly $0 deterministic self-eval: how often to CHECK whether one is due (the actual
        # cadence is config.eval_interval_hours, owned by eval_state.py; 0 there disables it).
        "eval_poll_sec": cfg.get("eval_poll_sec", 3600),
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


def run_eval(env: dict, dry_run: bool) -> None:
    """Run the $0 deterministic self-eval (no LLM, no browser, no channel send) and stamp it.
    Findings land in data/eval/; the daemon only logs a one-line summary. Fail-open."""
    if dry_run:
        logging.info("[dry-run] would run deterministic self-eval (eval_run.py run --no-llm)")
        return
    try:
        out = subprocess.run([sys.executable, str(BIN / "eval_run.py"), "run", "--no-llm"],
                             capture_output=True, text=True, env=env, cwd=str(SELLER_DIR), timeout=120)
        summary = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else f"rc={out.returncode}"
        logging.info("self-eval: %s", summary[:200])
        subprocess.run([sys.executable, str(BIN / "eval_state.py"), "mark"],
                       capture_output=True, text=True, env=env, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        logging.warning("self-eval failed: %s", exc)


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


def run_pass(mode: str, channel: dict, env: dict, dry_run: bool,
             extra_env: dict | None = None) -> None:
    """Invoke the LLM pass (run_pass.sh seller|buyer) under the single-flight lock.
    For SELLER passes, pulse the native 'typing…' indicator every ~4s while it runs — instant,
    no LLM, covers the claude -p cold start + working time. The assistant authors all actual text.
    extra_env seeds per-pass hints (e.g. the buyer_peek snippet) into the pass's environment."""
    pulse = mode in ("seller", "channel")  # the channel pass is the one the user is waiting on
    pass_env = {**env, **(extra_env or {})}
    if dry_run:
        logging.info("[dry-run] would run %s pass (typing pulse=%s, hints=%s)",
                     mode, pulse, list((extra_env or {}).keys()))
        return
    if RUN_LOCK.exists():
        logging.info("run lock held — skipping %s pass this tick", mode)
        return
    RUN_LOCK.write_text(str(os.getpid()))
    try:
        logging.info("running %s pass…", mode)
        proc = subprocess.Popen([str(BIN / "run_pass.sh"), mode], env=pass_env, cwd=str(SELLER_DIR))
        deadline = time.monotonic() + 900
        while proc.poll() is None:
            # MID-FLIGHT INTERRUPT: a pause set via the CLI/slash command or a prior tick stops the
            # running pass within ~one poll cadence (idempotent — cursors/pacing make the killed step
            # safe to re-run; it won't, until /resume). This covers ALL modes, incl. the seller pass.
            if control.is_paused():
                logging.info("paused mid-%s pass → terminate (idempotent; resumes after /resume)", mode)
                proc.terminate()
                break
            if pulse:
                _send_typing(channel, env)
            elif mode in ("buyer", "buy", "maint"):
                peeked = channel_peek(channel, env, 2)
                if peeked["pending"] > 0:
                    if peeked.get("latest_text", "").strip().startswith("/pause"):
                        # Telegram /pause arriving DURING a long background pass → set the flag and
                        # terminate now (the next loop iteration's drain consumes + acks the command).
                        logging.info("/pause during %s pass → set flag + terminate", mode)
                        control.pause(source="telegram")
                        _send_typing(channel, env)
                        proc.terminate()
                        break
                    # seller is waiting → fire typing now and PREEMPT this background pass (idempotent;
                    # it resumes next cycle). The channel pass runs on the next loop iteration.
                    logging.info("seller message during %s pass → typing + preempt", mode)
                    _send_typing(channel, env)
                    proc.terminate()
                    break
            try:
                proc.wait(timeout=4)          # ~4s cadence; returns early when the pass finishes
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                logging.error("%s pass exceeded 900s — killing", mode)
                proc.kill()
                break
        logging.info("%s pass done rc=%s", mode, proc.returncode)
    finally:
        RUN_LOCK.unlink(missing_ok=True)


def _acquire_instance_lock():
    """Process-lifetime SINGLETON. Only one agent_daemon may run: the concurrent supervisor's
    heartbeat-TTL lease liveness assumes a single heartbeater, and a second consumer also fights the
    Telegram offset. Returns the held fd (keep it referenced for the process lifetime) or None if
    another instance already holds it. The OS frees the flock automatically on crash/exit."""
    fd = os.open(str(INSTANCE_LOCK), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def main(argv) -> int:
    p = argparse.ArgumentParser(prog="agent_daemon.py")
    p.add_argument("--once", action="store_true", help="run one iteration then exit")
    p.add_argument("--dry-run", action="store_true", help="log decisions; don't invoke claude")
    p.add_argument("--peek-timeout", type=int, default=None)
    ns = p.parse_args(argv[1:])

    setup_logging()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    _instance_lock = _acquire_instance_lock()  # noqa: F841 — held for the process lifetime
    if _instance_lock is None:
        logging.error("another agent_daemon is already running (instance lock held) — exiting")
        return 3
    RUN_LOCK.unlink(missing_ok=True)  # clear any stale lock from a hard crash

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
    if channel["adapter"] == "telegram":
        _register_bot_commands(env)  # populate the "/" autocomplete menu (idempotent, non-fatal)
    last_buyer = time.monotonic() - cfg["buyer_poll_sec"]  # make a buyer pass due immediately
    last_buyer_pass = time.monotonic()                     # when an actual buyer PASS last ran (time floor)
    last_buy = time.monotonic() - cfg["buy_poll_sec"]      # and a buy pass
    last_maint = time.monotonic() - cfg["maint_poll_sec"]  # and a maintenance pass
    last_eval = time.monotonic()                           # self-eval check throttle (not immediate)
    empty_peeks = 0  # consecutive buyer peeks that found nothing new (drives the safety net)
    empty_buys = 0   # consecutive buy peeks that found nothing actionable (buy safety net)
    _was_paused = False  # edge-triggered PAUSED/RESUMED logging (the loop runs ~1/sec; don't spam)
    src_fp = _source_fingerprint()  # exit cleanly when our own code changes → launchd respawns fresh
    while not _stop:
        # A code change to the daemon's own sources only takes effect on restart (no hot-reload), so
        # bounce here at a between-pass boundary (no pass runs at loop top — run_pass is synchronous).
        if _source_fingerprint() != src_fp:
            logging.info("daemon source changed → exiting to reload (launchd will respawn on fresh code)")
            break
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
                run_pass("buyer", channel, env, ns.dry_run, extra_env={
                    "BAZAAR_BUYER_PEEK_TEXT": nt.get("latest_text", "")})
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
                run_pass("buyer", channel, env, ns.dry_run, extra_env={
                    "BAZAAR_BUYER_PEEK_TEXT": hint,
                    "BAZAAR_BUYER_PEEK_FORCED": "1" if not bp.get("pending") else "",
                })
                last_buyer_pass = time.monotonic()
            elif action == "skip":
                logging.info("buyer recheck: all inboxes clear → skip forced pass (~0 tokens)")
            else:  # idle
                empty_peeks += 1
                logging.info("buyer peek: nothing new (%s consecutive) → skip pass", empty_peeks)
            if action != "idle":
                empty_peeks = 0
            last_buyer = time.monotonic()

        # MAINTENANCE (§2b detect): drain an active distribution/inbox-takeover batch one step per
        # pass, or start a cadence-due my-listings SCAN and/or inbox SWEEP. Gated by cheap non-LLM
        # probes so the LLM only runs when there's work (never interrupts an active listing wizard).
        if not paused and time.monotonic() - last_maint >= cfg["maint_poll_sec"]:
            dist_active = _distribution_active()
            idet_active = _inbox_detect_active()
            if dist_active or idet_active or _scan_due(env) or _inbox_sweep_due(env):
                reason = ("drain distribution batch" if dist_active else
                          "drain inbox-takeover batch" if idet_active else "detect due")
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
                run_pass("buy", channel, env, ns.dry_run, extra_env={
                    "BAZAAR_BUY_PEEK_WANT": bpk.get("want_id") or "",
                    "BAZAAR_BUY_PEEK_TEXT": bpk.get("latest_text", ""),
                })
                empty_buys = 0
            else:
                empty_buys += 1
                logging.info("buy peek: nothing actionable (%s consecutive) → skip pass", empty_buys)
            last_buy = time.monotonic()

        # NIGHTLY SELF-EVAL (deterministic, $0): on a slow throttle, check the cadence gate and run
        # the no-LLM eval if due. Pure file reads/writes — no LLM, no browser, no channel send — so
        # it honors the daemon's "idle cost ≈ zero" contract. The LLM judge stays manual. Gated on
        # `not paused` too so /pause means a literal full stop (no work of any kind until /resume).
        if not paused and time.monotonic() - last_eval >= cfg["eval_poll_sec"]:
            if _eval_due(env):
                logging.info("self-eval due → running deterministic checks")
                run_eval(env, ns.dry_run)
            last_eval = time.monotonic()

        if ns.once:
            break
        time.sleep(1)  # peek already long-polled; brief yield
    logging.info("daemon stopping (clean)")
    RUN_LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
