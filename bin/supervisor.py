#!/usr/bin/env python3
"""supervisor.py — the concurrent daemon loop (opt-in, max_concurrent_workers > 1).

agent_daemon.py runs its proven single-flight loop by DEFAULT and dispatches here only when
`config.max_concurrent_workers > 1`. So this is purely additive: the default path is untouched.

What it parallelizes (conservatively): SELL-INBOX (buyer) work across DIFFERENT marketplaces —
one worker per marketplace, each holding its own `market:<id>` lease and (via harness_run's
--resource scoping) driving only its own Chrome tab. That is the "FB inbox ∥ Carousell inbox" win.

Everything else stays EXCLUSIVE for the conservative posture (never two automated actions on one
account, and these passes are unscoped or may publish to any market):
  • channel/seller — privileged; PREEMPTS market workers when the user messages (today's behavior).
  • buy / maint / eval — run only when NO market worker is live.

It reuses agent_daemon's probes + blocking `run_pass` (with its typing pulse + preempt) for the
exclusive passes; only the parallel buyer fan-out and the per-resource leases are new.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time

import agent_daemon as ad   # imported lazily by ad.main() → fully-initialized module, no cycle
import lease

LEASE_TTL_SEC = 600    # crash-recovery TTL — generously above any single loop iteration's blocking
                       # subprocess timeouts, so a live worker's lease is never wrongly reclaimed.
MAX_WORKER_SEC = 900   # hard wall-clock cap per worker (a stuck/runaway pass is killed by the watchdog)


def _data():
    return lease.data_dir()


def enabled_sell_markets(seller_config_path=None):
    """Enabled sell marketplaces from seller_config (object {id:{enabled}} or legacy array)."""
    path = seller_config_path or (_data() / "seller_config.json")
    try:
        mk = json.loads(path.read_text()).get("marketplaces", {})
    except (OSError, ValueError):
        return []
    if isinstance(mk, dict):
        return [m for m, sel in mk.items() if sel.get("enabled")]
    if isinstance(mk, list):
        return list(mk)
    return []


def plan_buyer_launches(peek, enabled, busy, free_slots):
    """PURE: which sell markets get a scoped buyer worker this tick.

    A market qualifies when buyer_peek flagged it `new`, it's enabled, and no worker is already on
    it. Capped at `free_slots` (= max_concurrent_workers − live workers). Deterministic order =
    `enabled` order, so the planner is testable."""
    if free_slots <= 0:
        return []
    markets = (peek or {}).get("markets", {})
    ready = [m for m in enabled if m not in busy and (markets.get(m) or {}).get("new")]
    return ready[:free_slots]


def plan_recheck_launches(recheck, enabled, busy, free_slots):
    """PURE: a count-net forced sweep launches ONLY markets the deterministic recheck (buyer_recheck)
    flagged as unhandled (unread or unreadable), skipping busy ones, capped at free_slots. This is
    the supervisor mirror of the daemon's recheck gate: a forced sweep no longer fans out to every
    market to 'confirm nothing', it spends a ~0-token recheck and launches only real work.
    Deterministic order = `enabled` order, so the planner is testable."""
    if free_slots <= 0:
        return []
    flagged = (recheck or {}).get("markets", {})
    ready = [m for m in enabled if m not in busy and (flagged.get(m) or {}).get("unhandled")]
    return ready[:free_slots]


def plan_buyer_sweep(enabled, busy, sweep_idx):
    """PURE: the forced safety-net sweep launches ONE market, round-robin by `sweep_idx`.

    Adaptive concurrency: genuine fan-out (FB ∥ Carousell) happens only for markets the peek
    flagged `new` (plan_buyer_launches). A forced sweep fires when nothing is flagged new — there
    is no signal that two markets are hot, so blanket-launching every free market just doubled idle
    cost. Launch one instead, rotating across enabled markets so no market is starved across
    successive forced sweeps. Returns [market] or []."""
    eligible = [m for m in enabled if m not in busy]
    if not eligible:
        return []
    return [eligible[sweep_idx % len(eligible)]]


def _holder(market, seq):
    return f"sup:buyer:{market}:{seq}"


def _kill_tree(proc, sig):
    """Signal the worker's whole process GROUP, not just the wrapper. Workers are launched with
    start_new_session=True, so the pgid == the wrapper pid — this reaches run_pass.sh → harness_run
    → the `claude` grandchild that actually drives the tab. (Signalling only proc.pid would orphan
    claude, leaving it driving the account after the lease is released — the CRITICAL bug.)"""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass  # already gone


def _confirm_dead(proc, grace=10):
    """SIGTERM the group, wait, then SIGKILL — return ONLY once the whole tree is gone."""
    _kill_tree(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_tree(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logging.error("worker pid %s survived SIGKILL", proc.pid)


def _reap(workers):
    """Release leases of exited workers; watchdog-kill any past MAX_WORKER_SEC. A NATURAL exit means
    the claude grandchild already finished (harness_run.run_pass blocks on it) — no orphan; only
    forced kills must target the process group."""
    now = time.monotonic()
    for market, w in list(workers.items()):
        if w["proc"].poll() is not None:
            logging.info("buyer worker [%s] done rc=%s", market, w["proc"].returncode)
            lease.release(_data(), f"market:{market}", w["holder"])
            del workers[market]
        elif now - w["started"] > MAX_WORKER_SEC:
            logging.error("buyer worker [%s] exceeded %ss — killing process group", market, MAX_WORKER_SEC)
            _confirm_dead(w["proc"])
            lease.release(_data(), f"market:{market}", w["holder"])
            del workers[market]


def _heartbeat(workers):
    for market, w in workers.items():
        lease.heartbeat(_data(), f"market:{market}", w["holder"])


def _preempt_all(workers):
    """Kill each live worker's PROCESS GROUP and release its lease ONLY after the tree is confirmed
    dead — so an exclusive channel/seller pass never runs while an orphaned worker still drives the
    same account (the conservative same-account guard, preserved through teardown)."""
    for market, w in list(workers.items()):
        _confirm_dead(w["proc"])
        lease.release(_data(), f"market:{market}", w["holder"])
        del workers[market]


def _drain_outbox(channel, env, dry_run):
    """Single-writer FIFO drain: send queued background notices to the control channel, in order.

    Concurrent buyer workers ENQUEUE notices (channel_outbox.py) instead of writing the channel
    directly; the supervisor (single-threaded) is the one writer, so messages never interleave.
    Telegram only in v1 (the other adapters have no one-shot send wired here); their notices wait."""
    if channel.get("adapter") != "telegram":
        return
    try:
        out = subprocess.run([sys.executable, str(ad.BIN / "channel_outbox.py"), "peek"],
                             capture_output=True, text=True, env=env, timeout=15)
        if out.returncode != 0:
            return
        pending = json.loads(out.stdout).get("pending", [])
    except (subprocess.SubprocessError, ValueError):
        return
    for rec in pending:
        if dry_run:
            logging.info("[dry-run] would send queued notice %s", rec.get("id"))
            continue
        cmd = [sys.executable, str(ad.BIN / "telegram.py"), "send",
               "--text", rec.get("text", ""), "--kind", rec.get("kind", "notify")]
        if rec.get("ref"):
            cmd += ["--ref", str(rec["ref"])]
        # A failed/slow send must neither CRASH the supervisor (try/except) nor HEAD-OF-LINE-BLOCK
        # the queue forever (continue, not break) — a poison notice is retried next tick, the rest
        # still drain. ack failure → at-least-once (may re-send): acceptable for notices.
        try:
            sent = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=25)
            failed = sent.returncode != 0
        except subprocess.SubprocessError as exc:
            logging.warning("outbox send errored for %s (%s)", rec.get("id"), exc)
            failed = True
        if failed:
            # bounded retry: increment attempts; channel_outbox dead-letters after MAX so one poison
            # notice can't block the queue forever. Other notices still drain (continue, not break).
            try:
                subprocess.run([sys.executable, str(ad.BIN / "channel_outbox.py"), "fail",
                                "--id", rec["id"]], capture_output=True, text=True, env=env, timeout=15)
            except subprocess.SubprocessError:
                pass
            continue
        try:
            ack = subprocess.run([sys.executable, str(ad.BIN / "channel_outbox.py"), "ack", "--id", rec["id"]],
                                 capture_output=True, text=True, env=env, timeout=15)
            if ack.returncode != 0:
                logging.warning("outbox ack failed for %s — may re-send next tick", rec.get("id"))
        except subprocess.SubprocessError as exc:
            logging.warning("outbox ack errored for %s (%s) — may re-send next tick", rec.get("id"), exc)


def _launch_buyer(market, env, peek, holder, dry_run, hint=None):
    """Acquire market:<id> then Popen a scoped buyer pass. Returns the Popen, or None (dry-run/race).
    `hint` overrides the peek-derived snippet (used by the notification-path trigger, which already
    carries the buyer's message text)."""
    acq = lease.acquire(_data(), f"market:{market}", holder, "buyer", LEASE_TTL_SEC)
    if not acq["acquired"]:
        logging.info("buyer worker [%s]: lease busy — skip", market)
        return None
    if dry_run:
        logging.info("[dry-run] would launch buyer worker for %s", market)
        lease.release(_data(), f"market:{market}", holder)
        return None
    if hint is not None:
        text = hint
    else:
        snippet = ((peek.get("markets") or {}).get(market) or {}).get("snippet", "")
        text = f"[{market}] {snippet}".strip()
    worker_env = {**env, "BAZAAR_BUYER_PEEK_TEXT": text}
    # start_new_session=True → the worker leads its own process group, so _kill_tree can signal the
    # whole tree (wrapper + claude grandchild) on preempt. Without it, preempt would orphan claude.
    proc = subprocess.Popen([str(ad.BIN / "run_pass.sh"), "buyer", "--resource", market],
                            env=worker_env, cwd=str(ad.SELLER_DIR), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def run(cfg, channel, env, ns, max_workers, peek_timeout) -> int:
    workers = {}        # market -> {"proc": Popen, "holder": token}
    seq = 0
    empty_buyer_cycles = 0
    sweep_idx = 0       # round-robin cursor for the forced one-market safety-net sweep
    enabled = enabled_sell_markets()
    last_buyer = time.monotonic() - cfg["buyer_poll_sec"]
    last_buyer_pass = time.monotonic()  # when an actual buyer worker last launched (time floor)
    last_buy = time.monotonic() - cfg["buy_poll_sec"]
    last_maint = time.monotonic() - cfg["maint_poll_sec"]
    last_eval = time.monotonic()
    logging.info("supervisor up · max_workers=%s · sell markets=%s · (channel/buy/maint exclusive)",
                 max_workers, enabled)
    src_fp = ad._source_fingerprint()  # exit cleanly when our own code changes → launchd respawns fresh

    while not ad._stop:
        # A code change to the daemon's own sources only takes effect on restart (no hot-reload).
        # Bounce at loop top, then _preempt_all (below) tears down live workers cleanly before exit.
        if ad._source_fingerprint() != src_fp:
            logging.info("daemon source changed → exiting to reload (launchd will respawn on fresh code)")
            break
        _reap(workers)
        _heartbeat(workers)
        _drain_outbox(channel, env, ns.dry_run)   # flush queued background notices, in order
        paused = ad.control.is_paused()

        # CONTROL CHANNEL (privileged + exclusive): a user message preempts all market workers.
        peek = ad.channel_peek(channel, env, peek_timeout)
        if peek["pending"]:
            ad._send_typing(channel, env)
            if paused:
                if not ns.dry_run:
                    subprocess.run([sys.executable, str(ad.BIN / "channel_control.py"), "drain"],
                                   env=env, capture_output=True, timeout=60)
            else:
                if workers:
                    logging.info("channel work → preempting %s buyer worker(s)", len(workers))
                    _preempt_all(workers)
                if not ad._listing_active():
                    ad.send_intent(channel, env, peek.get("latest_text", ""), ns.dry_run)
                ad.run_pass("seller", channel, env, ns.dry_run)

        # NOTIFICATION-PATH trigger (checked every iteration, ~0 tokens): launch a scoped buyer
        # worker for any notification-path market (trigger_resolver) with new OS-notification mail,
        # within free slots. Instant wake; poll-path markets fall through to the buyer gate below.
        if not paused:
            nt = ad.notify_trigger(env)
            for market, info in (nt.get("markets") or {}).items():
                if market in workers or (max_workers - len(workers)) <= 0:
                    continue
                seq += 1
                holder = _holder(market, seq)
                proc = _launch_buyer(market, env, {}, holder, ns.dry_run,
                                     hint=info.get("latest_text", ""))
                if proc is not None:
                    workers[market] = {"proc": proc, "holder": holder, "started": time.monotonic()}
                    last_buyer_pass = time.monotonic()
                    logging.info("notification trigger → buyer worker [%s]: %s",
                                 market, info.get("latest_text", "")[:60])

        # SELL INBOX (the parallel part): one scoped buyer worker per market with new activity.
        if not paused and time.monotonic() - last_buyer >= cfg["buyer_poll_sec"]:
            bp = ad.buyer_peek(env)
            free = max_workers - len(workers)
            to_launch = plan_buyer_launches(bp, enabled, set(workers), free)
            if to_launch:
                empty_buyer_cycles = 0
            elif free > 0:
                # Nothing flagged new. Two safety nets (ad.buyer_force_due): the count net AND the
                # absolute time floor.
                empty_buyer_cycles += 1
                floor_sec = cfg.get("force_buyer_sweep_hours", 0) * 3600
                forced, reason = ad.buyer_force_due(
                    empty_buyer_cycles, cfg["force_buyer_pass_every"],
                    time.monotonic() - last_buyer_pass, floor_sec)
                floor_due = floor_sec > 0 and (time.monotonic() - last_buyer_pass) >= floor_sec
                if forced and floor_due:
                    # Ultimate strand backstop: force an ACTUAL pass for ONE market (round-robin),
                    # even when the cheap signals say clear (covers a strand that left count==0).
                    to_launch = plan_buyer_sweep(enabled, set(workers), sweep_idx)
                    sweep_idx += 1
                    empty_buyer_cycles = 0
                    if to_launch:
                        logging.info("buyer floor sweep (%s) → %s", reason, to_launch)
                elif forced:
                    # Count-net force: spend a ~0-token recheck and launch ONLY markets with real
                    # unread (adaptive concurrency — no blanket fan-out to 'confirm nothing').
                    rc = ad.buyer_recheck(env)
                    to_launch = plan_recheck_launches(rc, enabled, set(workers), free)
                    empty_buyer_cycles = 0
                    if to_launch:
                        logging.info("buyer recheck sweep → %s", to_launch)
                    else:
                        logging.info("buyer recheck: all inboxes clear → skip forced sweep (~0 tokens)")
            for market in to_launch:
                seq += 1
                holder = _holder(market, seq)
                proc = _launch_buyer(market, env, bp, holder, ns.dry_run)
                if proc is not None:
                    workers[market] = {"proc": proc, "holder": holder, "started": time.monotonic()}
                    last_buyer_pass = time.monotonic()
                    logging.info("launched buyer worker [%s] (%s live)", market, len(workers))
            last_buyer = time.monotonic()

        # EXCLUSIVE passes: only when no market worker is live (they'd contend on a shared tab/account).
        if not paused and not workers and time.monotonic() - last_maint >= cfg["maint_poll_sec"]:
            if (ad._distribution_active() or ad._inbox_detect_active()
                    or ad._scan_due(env) or ad._inbox_sweep_due(env)):
                logging.info("maint pass (exclusive)")
                ad.run_pass("maint", channel, env, ns.dry_run)
            last_maint = time.monotonic()

        if not paused and not workers and time.monotonic() - last_buy >= cfg["buy_poll_sec"]:
            bpk = ad.buy_peek(env)
            if bpk.get("pending"):
                logging.info("buy pass (exclusive) → %s", bpk.get("latest_text", "")[:60])
                ad.run_pass("buy", channel, env, ns.dry_run, extra_env={
                    "BAZAAR_BUY_PEEK_WANT": bpk.get("want_id") or "",
                    "BAZAAR_BUY_PEEK_TEXT": bpk.get("latest_text", ""),
                })
            last_buy = time.monotonic()

        # eval gated on `not paused` too (like the buyer/maint/buy passes above) so /pause is a
        # literal full stop — no work of any kind, deterministic or otherwise, until /resume.
        if not paused and not workers and time.monotonic() - last_eval >= cfg["eval_poll_sec"]:
            if ad._eval_due(env):
                ad.run_eval(env, ns.dry_run)
            last_eval = time.monotonic()

        if ns.once:
            break
        time.sleep(1)

    _preempt_all(workers)
    logging.info("supervisor stopping (clean)")
    return 0
