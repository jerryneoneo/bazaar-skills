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
import subprocess
import sys
import time

import agent_daemon as ad   # imported lazily by ad.main() → fully-initialized module, no cycle
import channel_control      # is_pause_command (shared /pause matching rule) + the deterministic drain
import channel_outbox       # single-writer control-channel queue (the cap-hit escalation enqueues here)
import harness_run          # CAP_HIT_SIGNAL lives here (run_pass returns it on a turn-cap kill)
import inbox_scan           # SELL peek probes (fail-open wrappers below) for the priority-hint thread
import lease
import proc_tree   # shared kill-the-whole-tree teardown (also used by agent_daemon's default loop)

LEASE_TTL_SEC = lease.AGENT_MARKET_TTL_SEC  # canonical 600s liveness window — sourced from lease.py so
                       # journal_reconcile's in-flight-intent guard uses the IDENTICAL TTL (they can never
                       # drift; a mismatch was the J1 bug that re-opened the in-flight-intent steal).
MAX_WORKER_SEC = 900   # hard wall-clock cap per worker (a stuck/runaway pass is killed by the watchdog)

# Fix C — a buyer worker killed at the turn cap exits with this DISTINCT code (harness_run.run_pass
# maps "rc!=0 + 'Reached max turns'" to it). The supervisor schedules ONE bounded continuation per
# cap-hit, up to CONTINUATION_RETRY_CAP times for a given market, then ESCALATES (never silently
# drops the backlog). The cap stops a perpetually-capping market from hot-looping continuations.
CAP_HIT_SIGNAL = harness_run.CAP_HIT_SIGNAL
CONTINUATION_RETRY_CAP = 2


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


def plan_continuations(capped, busy, free_slots, attempts):
    """The SOLE continuation-budget gate (Bug C3): decide AND record, in one place.

    For each capped market (not already busy):
      • within the retry budget (`attempts[market] < CONTINUATION_RETRY_CAP`) → schedule ONE
        continuation and INCREMENT the counter (so the next cap-hit advances toward the cap).
      • at/over the budget → DROP it for ESCALATION and reset its counter (next cap-hit starts fresh).
    Launches are capped at `free_slots`; markets that don't fit this tick are left UNTOUCHED (not
    counted, not escalated) so they are retried next tick. Deterministic order (sorted) so the planner
    is testable.

    Returns {"launch": [market, ...], "escalate": [market, ...]}. This is the ONLY place the budget
    counter is advanced/spent — _reap merely reports capped markets — so the budget is gated EXACTLY
    once (the old double-gate delivered 1 continuation instead of CONTINUATION_RETRY_CAP).

    IN-PLACE MUTATOR: increments/resets `attempts` (the durable cross-tick counter the run loop
    carries), mirroring the existing _reap/`workers` mutation convention."""
    launch: list = []
    escalate: list = []
    for market in sorted(m for m in capped if m not in busy):
        if attempts.get(market, 0) < CONTINUATION_RETRY_CAP:
            if len(launch) >= max(0, free_slots):
                continue  # no free slot this tick → retry next tick (don't spend the budget)
            attempts[market] = attempts.get(market, 0) + 1
            launch.append(market)
        else:
            escalate.append(market)
            attempts.pop(market, None)  # budget spent → reset (next cap-hit starts fresh)
    return {"launch": launch, "escalate": escalate}


def _escalate_cap_hit(market, env, dry_run):
    """ESCALATE a market that keeps hitting the turn cap over the SAME control-channel mechanism the
    ESCALATE path uses — enqueue a `notify` on channel_outbox (the supervisor's single writer drains
    it in order). Never silently drop a stranded backlog. Fail-open: an enqueue error is logged, not
    raised, so it can't crash the reaper."""
    text = (f"⚠️ buyer:{market} keeps hitting the turn cap after "
            f"{CONTINUATION_RETRY_CAP} continuations; its backlog may be stranded. "
            f"Open {market} and check for unread buyer messages.")
    if dry_run:
        logging.info("[dry-run] would escalate cap-hit for %s", market)
        return
    try:
        path = channel_outbox.data_dir() / "channel_outbox.jsonl"
        from datetime import datetime, timezone
        channel_outbox.run_enqueue("notify", text, datetime.now(timezone.utc), path,
                                   source="cap-hit")
        logging.warning("buyer:%s exhausted continuation budget → escalated over channel", market)
    except (OSError, ValueError) as exc:
        logging.error("cap-hit escalation enqueue failed for %s: %s", market, exc)


def peek_thread_for(market, sell_threads):
    """PURE: the conservative per-market priority-hint thread, or None.

    Returns the single fresh tracked-sell thread for `market` ONLY when there is EXACTLY one — with 0
    fresh threads (a brand-new enquiry, or none) or >1 (ambiguous), return None so the worker keeps
    today's market-scoped behavior. Scoping to one of several threads risks the pass fixating on it
    and missing the others; mis-routing a reply is the worst outcome, so we under-hint."""
    threads = (sell_threads or {}).get(market) or []
    return threads[0] if len(threads) == 1 else None


def sell_threads_from_peek(bp):
    """PURE (C-followup): rebuild {market: [thread_id, ...]} from a buyer_peek RESULT's per-market
    sell_threads. buyer_peek.peek already advanced the SELL memo once and now surfaces the matched
    thread ids per market, so the supervisor's poll path reads the hint from THAT result instead of
    calling _sell_threads_new() (a SECOND advancing probe that would see the advanced memo and null
    the hint). Fail-open to {} on a malformed/old-shape peek."""
    try:
        markets = (bp or {}).get("markets") or {}
        return {m: list((info or {}).get("sell_threads") or []) for m, info in markets.items()}
    except Exception:  # noqa: BLE001 — a pure derivation must never crash the loop
        return {}


def _sell_threads_new():
    """Wrap inbox_scan.sell_threads_new() fail-open so a probe hiccup never crashes the loop —
    a missing hint just means an unscoped (market-only) pass, today's conservative default."""
    try:
        return inbox_scan.sell_threads_new()
    except Exception as exc:  # noqa: BLE001 — a hint probe must never break the loop
        logging.warning("sell_threads_new probe error: %s", exc)
        return {}


def _holder(market, seq):
    return f"sup:buyer:{market}:{seq}"


# Teardown lives in proc_tree (shared with agent_daemon's default loop). Kept as thin module-local
# aliases so the worker call sites and tests read naturally.
def _kill_tree(proc, sig):
    proc_tree.kill_tree(proc, sig)


def _confirm_dead(proc, grace=proc_tree.GRACE_SEC):
    proc_tree.confirm_dead(proc, grace)


def _reap(workers, cont_attempts=None, dry_run=False):
    """Release leases of exited workers; watchdog-kill any past MAX_WORKER_SEC. A NATURAL exit means
    the claude grandchild already finished (harness_run.run_pass blocks on it) — no orphan; only
    forced kills must target the process group.

    Bug C3 (single-gate): a worker that exited with CAP_HIT_SIGNAL was killed at the turn cap with
    work still pending. `_reap` ONLY reports it as `capped` here — it does NOT increment the budget or
    escalate. plan_continuations is the SOLE gate (it increments on launch and escalates a market it
    drops at the cap), so the budget is gated EXACTLY ONCE. (Previously _reap incremented AND appended,
    then run() re-filtered the list through plan_continuations against the now-incremented counter, so
    the budget was gated twice — only 1 continuation fired and the escalation was skipped.)

    A watchdog-killed worker is NOT treated as a cap-hit (it's a stuck/runaway pass, a different
    failure). A clean (non-cap) exit clears any retry history. Returns {"capped": [market, ...]} for
    the caller (run) to feed into plan_continuations.

    IN-PLACE MUTATOR (by design, like the existing `workers` handling): mutates BOTH `workers`
    (removes reaped entries) and `cont_attempts` (clears the counter for a CLEAN exit so a later
    cap-hit starts fresh — the only counter write left in _reap; the increment now lives in
    plan_continuations)."""
    cont_attempts = cont_attempts if cont_attempts is not None else {}
    capped: list = []
    now = time.monotonic()
    for market, w in list(workers.items()):
        if w["proc"].poll() is not None:
            rc = w["proc"].returncode
            logging.info("buyer worker [%s] done rc=%s", market, rc)
            lease.release(_data(), f"market:{market}", w["holder"])
            del workers[market]
            if rc == CAP_HIT_SIGNAL:
                capped.append(market)  # plan_continuations is the SOLE budget gate (no double-gate)
            else:
                cont_attempts.pop(market, None)  # a clean (non-cap) exit clears any retry history
        elif now - w["started"] > MAX_WORKER_SEC:
            logging.error("buyer worker [%s] exceeded %ss — killing process group", market, MAX_WORKER_SEC)
            _confirm_dead(w["proc"])
            lease.release(_data(), f"market:{market}", w["holder"])
            del workers[market]
            # A watchdog kill is a TIMEOUT failure, not a cap-hit — clear any prior cap-hit retry
            # history so a stale counter can't make the NEXT genuine cap-hit escalate a cycle early.
            cont_attempts.pop(market, None)
    return {"capped": capped}


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


def _launch_buyer(market, env, peek, holder, dry_run, hint=None, peek_thread=None):
    """Acquire market:<id> then Popen a scoped buyer pass. Returns the Popen, or None (dry-run/race).
    `hint` overrides the peek-derived snippet (used by the notification-path trigger, which already
    carries the buyer's message text). `peek_thread` (Fix C) seeds $BAZAAR_BUYER_PEEK_THREAD — a
    PRIORITY hint to handle that one thread first; the pass still tours the rest within its budget."""
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
    if peek_thread:
        worker_env["BAZAAR_BUYER_PEEK_THREAD"] = peek_thread
    # start_new_session=True → the worker leads its own process group, so _kill_tree can signal the
    # whole tree (wrapper + claude grandchild) on preempt. Without it, preempt would orphan claude.
    try:
        proc = subprocess.Popen([str(ad.BIN / "run_pass.sh"), "buyer", "--resource", market],
                                env=worker_env, cwd=str(ad.SELLER_DIR), start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        # Popen failed (e.g. fork/ENOMEM/exec error) AFTER we took the lease — release it so the
        # market isn't stranded until the TTL expires, and don't crash the supervisor loop.
        logging.error("buyer worker [%s]: launch failed (%s) — releasing lease", market, exc)
        lease.release(_data(), f"market:{market}", holder)
        return None
    return proc


def run(cfg, channel, env, ns, max_workers, peek_timeout) -> int:
    workers = {}        # market -> {"proc": Popen, "holder": token}
    cont_attempts = {}  # market -> continuation count for Fix C (turn-cap retry budget; resets on success)
    seq = 0
    empty_buyer_cycles = 0
    sweep_idx = 0       # round-robin cursor for the forced one-market safety-net sweep
    enabled = enabled_sell_markets()
    last_buyer = time.monotonic() - cfg["buyer_poll_sec"]
    last_buyer_pass = time.monotonic()  # when an actual buyer worker last launched (time floor)
    last_buy = time.monotonic() - cfg["buy_poll_sec"]
    last_maint = time.monotonic() - cfg["maint_poll_sec"]
    last_eval = time.monotonic()
    last_update = time.monotonic()   # upstream update-check throttle (not immediate)
    last_followup = time.monotonic()  # stale-chat follow-up check throttle (not immediate)
    last_sweep = time.monotonic()    # outbox sweeper throttle (Track A5; not immediate)
    was_paused = ad.control.is_paused()  # edge-trigger for the one-shot pause confirmation (below)
    # Make a corrections-apply pass due IMMEDIATELY if any correction is already pending (e.g. one
    # stranded by a prior resume that never applied it), not CORRECTIONS_RETRY_SEC from now.
    last_corrections = time.monotonic() - channel_control.CORRECTIONS_RETRY_SEC
    logging.info("supervisor up · max_workers=%s · sell markets=%s · (channel/buy/maint exclusive)",
                 max_workers, enabled)
    # Bug C6: sweep per-pass logs leaked by forced kills (preempt/deadline/watchdog skip run_pass's
    # finally). A startup glob-and-unlink of stale pass-*.log is the deterministic backstop.
    swept = harness_run.sweep_stale_pass_logs()
    if swept:
        logging.info("swept %s stale per-pass log(s) leaked by forced kills", len(swept))
    # Heal a catch-up sweep orphaned by the very reload that respawned us (the reload-mid-sweep case):
    # a relaunched supervisor clears the stranded active:true on boot, so the maint lane never stays
    # frozen across a restart waiting for the first maint poll interval.
    if ad.reconcile_catchup_orphan(env, ns.dry_run):
        logging.info("reconciled a stale catch-up sweep orphaned by a prior crash/reload")
    ad._log_wake_mode()  # explicit Instant/Standard banner (did the FDA grant take?)
    src_fp = ad._source_fingerprint()  # exit cleanly when our own code changes → launchd respawns fresh

    while not ad._stop:
        iter_start = time.monotonic()  # Fix D: time each iteration so a wedged one is VISIBLE (WARN)
        # A code change to the daemon's own sources only takes effect on restart (no hot-reload).
        # Bounce at loop top, then _preempt_all (below) tears down live workers cleanly before exit.
        if ad._source_fingerprint() != src_fp:
            logging.info("daemon source changed → exiting to reload (launchd will respawn on fresh code)")
            ad.relaunch_self()  # clean single atomic restart on fresh code (shared with the default loop)
            break
        ad._touch_heartbeat()  # same .daemon.heartbeat the single-flight loop writes — so healthcheck's
                               # staleness check works in concurrent mode too (else it WARNs falsely)
        reaped = _reap(workers, cont_attempts, ns.dry_run)  # cap-hits reported as `capped` (Bug C3)
        _heartbeat(workers)  # lease heartbeats for live workers (distinct from the daemon heartbeat above)
        _drain_outbox(channel, env, ns.dry_run)   # flush queued background notices, in order
        paused = ad.control.is_paused()

        # PAUSE = a hard stop: tear down any live buyer worker NOW, don't just stop launching new
        # ones. The concurrent path's workers are async (Popen, not ad.run_pass), so they have no
        # in-pass interrupt — this is their equivalent of the single-flight loop's mid-flight kill
        # (agent_daemon.py:998). Reuses _preempt_all (kill tree → release lease → drop). Idempotent:
        # `workers` is empty on the next paused tick, so this is a no-op until /resume relaunches.
        if paused and workers:
            logging.info("paused → preempting %s live buyer worker(s)", len(workers))
            _preempt_all(workers)

        # ONE-SHOT pause confirmation on the false→true edge — regardless of HOW the flag flipped
        # (the /pause fast-path below, an LLM seller pass, or the CLI). The drain's catch-all claims
        # the ack exactly once (control.claim_pause_ack), so this never duplicates and never waits
        # for the next inbound message (the late-ack the incident showed). was_paused is seeded from
        # the live flag at startup, so a restart INTO a paused state never re-acks.
        if paused and not was_paused and not ns.dry_run:
            subprocess.run([sys.executable, str(ad.BIN / "channel_control.py"), "drain"],
                           env=env, capture_output=True, timeout=60)
        was_paused = paused

        # APPLY pending corrections (post-/resume, or any stranded one): the drain cleared the pause
        # and acked, but applying a correction is state-routed LLM work — so force ONE channel pass
        # (which runs skills/channel/corrections.md: apply each to durable state, mark-applied, and
        # report what changed). This is what turns "▶️ Resuming — applying your corrections now" into
        # an actual apply + a clear "here's what I did" follow-up, and it runs BEFORE the background
        # gates so they read the corrected state. A channel pass is exclusive → tear down workers first.
        if not ns.dry_run and channel_control.corrections_pass_due(
                paused, len(ad.control.pending_corrections()), time.monotonic() - last_corrections):
            if workers:
                _preempt_all(workers)
            # A /resume should also unwedge a stale catch-up sweep so the background lane comes back with
            # the corrections. Only a TTL-exceeded orphan is cleared — a fresh, live sweep is untouched.
            ad.reconcile_catchup_orphan(env, ns.dry_run, cfg.get("catchup_ttl_sec", ad.CATCHUP_TTL_SEC))
            logging.info("pending correction(s) → channel pass to apply + report")
            ad.run_pass("seller", channel, env, ns.dry_run)
            last_corrections = time.monotonic()

        # Keep notification-path (Meta) tabs hidden between passes so their OS push keeps firing
        # (a focused Meta tab delivers in-app instead). Dedicated warm Chrome → never the user's own.
        if not paused:
            ad.tab_park.park()

        # CONTINUATIONS (Bug C3): a buyer worker killed at the turn cap left work pending.
        # plan_continuations is the SOLE budget gate — it decides which capped markets get ONE more
        # bounded continuation (within the retry budget + free slots, incrementing the counter) and
        # which have exhausted the budget and must ESCALATE. The escalation is emitted HERE (not in
        # _reap), so the budget is gated exactly once. Each continuation re-runs the SAME market that
        # capped, market-scoped (no per-thread hint) so it never advances the SELL memo (Bug C8).
        if not paused and reaped["capped"]:
            free = max_workers - len(workers)
            plan = plan_continuations(set(reaped["capped"]), set(workers), free, cont_attempts)
            for market in plan["escalate"]:
                _escalate_cap_hit(market, None, ns.dry_run)
            # Bug C8: do NOT advance the SELL memo here. The previous code called _sell_threads_new()
            # (a memo-ADVANCING peek) for the per-thread hint, but the poll path below already owns the
            # single memo-advancing peek this iteration (sell_threads_from_peek). A second advance would
            # see the already-advanced memo and null the poll path's sell_peek, so an enumerable market
            # could be skipped that round. A continuation re-runs the SAME market that capped, so it is
            # safe + conservative to under-hint to a market-scoped (no specific thread) continuation —
            # exactly peek_thread_for's own behavior when the fresh-thread set is ambiguous. (The only
            # read-only sell probe, inbox_scan.sell_actionable_now(), yields a per-market boolean, not
            # thread ids, so it cannot pin a single-thread hint anyway; we under-hint instead.)
            for market in plan["launch"]:
                seq += 1
                holder = _holder(market, seq)
                proc = _launch_buyer(market, env, {}, holder, ns.dry_run, peek_thread=None)
                if proc is not None:
                    workers[market] = {"proc": proc, "holder": holder, "started": time.monotonic()}
                    last_buyer_pass = time.monotonic()
                    logging.info("buyer continuation launched [%s] (attempt %s)",
                                 market, cont_attempts.get(market))
                else:
                    # Bug C7: the launch FAILED (lease race / Popen OSError → None). plan_continuations
                    # already spent a budget slot at decision time, but no continuation actually ran —
                    # roll it back so a later genuine cap-hit still gets the full CONTINUATION_RETRY_CAP
                    # continuations (mirror single-flight, which counts only a continuation that runs).
                    cont_attempts[market] = max(0, cont_attempts.get(market, 0) - 1)

        # CONTROL CHANNEL (privileged + exclusive): a user message preempts all market workers.
        peek = ad.channel_peek(channel, env, peek_timeout)
        if peek["pending"]:
            ad._send_typing(channel, env)
            # DETERMINISTIC /pause fast-path: an explicit /pause sets the flag, tears down workers,
            # and lets the drain send the ONE confirmation — with NO LLM seller pass (which the
            # mid-flight interrupt would SIGTERM before it could ack, the self-kill race). Only the
            # explicit command matches (is_pause_command); fuzzy "stop" stays the LLM's job.
            if not paused and channel_control.is_pause_command(peek.get("latest_text", "")):
                logging.info("/pause at channel → set flag + preempt %s worker(s) + drain", len(workers))
                ad.control.pause(source="telegram")
                paused = True
                _preempt_all(workers)
                if not ns.dry_run:
                    subprocess.run([sys.executable, str(ad.BIN / "channel_control.py"), "drain"],
                                   env=env, capture_output=True, timeout=60)
            elif paused:
                if not ns.dry_run:
                    subprocess.run([sys.executable, str(ad.BIN / "channel_control.py"), "drain"],
                                   env=env, capture_output=True, timeout=60)
            else:
                if workers:
                    logging.info("channel work → preempting %s buyer worker(s)", len(workers))
                    _preempt_all(workers)
                # Deterministic, INSTANT handling (settle + sell/buy ask + sell-tap transition) so the
                # seller never waits on a ~2-min channel pass for an ack/transition. 'defer' = fully
                # handled here (no LLM pass needed). Shared with the single-flight loop.
                if ad.channel_instant_ack(channel, env, peek, cfg, ns.dry_run) != "defer":
                    ad.run_pass("seller", channel, env, ns.dry_run)

        # BACKGROUND RESEARCH (Phase B): drive the detached worker. When its result lands, present it
        # DETERMINISTICALLY (no LLM pass). Only a worker TIMEOUT (inline research) or auto-price needs
        # a pass — then preempt market workers first (the seller is waiting).
        if not paused and ad.research_orchestrate(channel, env, ns.dry_run, cfg) == "present":
            if workers:
                logging.info("research fallback → preempting %s worker(s) to present", len(workers))
                _preempt_all(workers)
            logging.info("research fallback → inline present pass")
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
                    # Do NOT reset last_buyer_pass here: it is the AGGREGATE strand-floor clock for
                    # the poll path; a per-market notification (FB) must not delay the floor sweep
                    # that backstops the other markets. The poll gate runs independently as fallback.
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
            # Conservative per-market priority hint (Fix C / C-followup): scope a worker to its single
            # fresh thread when the inbox shows exactly one; 0 or >1 → unset (market-scoped pass).
            # Derive the hint from THIS peek's per-market sell_threads (bp already advanced the SELL
            # memo); a second _sell_threads_new() probe here would advance it again and null the hint.
            # A forced/recheck sweep carries no precise sell_threads in bp, so the hint falls back to
            # None (unscoped) — today's behavior for those branches.
            sell_threads = sell_threads_from_peek(bp) if to_launch else {}
            for market in to_launch:
                seq += 1
                holder = _holder(market, seq)
                proc = _launch_buyer(market, env, bp, holder, ns.dry_run,
                                     peek_thread=peek_thread_for(market, sell_threads))
                if proc is not None:
                    workers[market] = {"proc": proc, "holder": holder, "started": time.monotonic()}
                    last_buyer_pass = time.monotonic()
                    logging.info("launched buyer worker [%s] (%s live)", market, len(workers))
            last_buyer = time.monotonic()

        # OUTBOX SWEEP (Track A5): re-drive any STRANDED never-fired send (status=pending in
        # thread_outbox owned by no live worker) by launching a scoped buyer worker for its market,
        # within free slots. Escalations the sweep enqueues are flushed by _drain_outbox at loop top.
        # The deterministic backstop that makes a silent drop impossible even with no inbound mail.
        if not paused and time.monotonic() - last_sweep >= cfg.get("outbox_sweep_poll_sec", 120):
            redrive = ad.sweep_outbox(env, ns.dry_run, busy_markets=set(workers))
            free = max_workers - len(workers)
            for market in sorted(m for m in redrive if m in enabled and m not in workers)[:max(0, free)]:
                seq += 1
                holder = _holder(market, seq)
                proc = _launch_buyer(market, env, {}, holder, ns.dry_run,
                                     hint="re-driving a reply that never sent")
                if proc is not None:
                    workers[market] = {"proc": proc, "holder": holder, "started": time.monotonic()}
                    last_buyer_pass = time.monotonic()
                    logging.info("outbox sweep → re-drive buyer worker [%s]", market)
            last_sweep = time.monotonic()

        # EXCLUSIVE passes: only when no market worker is live (they'd contend on a shared tab/account).
        if not paused and not workers and time.monotonic() - last_maint >= cfg["maint_poll_sec"]:
            # Heal a stale/abandoned catchup sweep FIRST (reload-killed → stranded active:true would
            # freeze this lane forever). Safe here: maint is exclusive with channel/buy (gated on
            # `not workers`), so the clear can never race a channel pass mid-advancing a live sweep —
            # and a FRESH sweep (within TTL) is left untouched and still defers maint below.
            ad.reconcile_catchup_orphan(env, ns.dry_run, cfg.get("catchup_ttl_sec", ad.CATCHUP_TTL_SEC))
            cat_active = ad._catchup_active()
            dist_active = ad._distribution_active()
            idet_active = ad._inbox_detect_active()
            lh_active = ad._listing_health_session_active()
            scan_due = ad._scan_due(env)
            sweep_due = ad._inbox_sweep_due(env)
            # Stale-listing suggestions are the LOWEST-priority maint step (see agent_daemon.py): only
            # open a new episode when no higher-priority detect/drain work is pending.
            if not (dist_active or idet_active or lh_active or scan_due or sweep_due or cat_active):
                lh_due_item = ad._listing_health_due(env)
                if lh_due_item:
                    ad.run_listing_health_start(env, lh_due_item, ns.dry_run)
                    lh_active = not ns.dry_run
            # A fresh catch-up sweep defers ALL maint this tick (the maint prompt would stand down anyway).
            if (dist_active or idet_active or lh_active or scan_due or sweep_due) and not cat_active:
                logging.info("maint pass (exclusive)%s",
                             " → suggest stale-listing fixes" if lh_active and not (
                                 dist_active or idet_active or scan_due or sweep_due) else "")
                ad.run_pass("maint", channel, env, ns.dry_run)
            elif cat_active:
                logging.info("maint deferred — catch-up sweep in flight (fresh)")
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

        # STALE-CHAT FOLLOW-UPS (exclusive, like maint/buy): nudge quiet counterparts then mark them
        # not interested. Drops are $0 deterministic (the notice rides the outbox _drain_outbox flushes
        # at loop top); nudges reuse an exclusive buyer/buy pass via BAZAAR_FOLLOWUP=1. Gated on
        # `not workers` so a nudge pass never contends with a live market worker for the same tab.
        if not paused and not workers and time.monotonic() - last_followup >= cfg["followup_poll_sec"]:
            ad.run_followup_reconcile(env)
            fu = ad._followup_due(env)
            if fu.get("drops"):
                ad.run_followup_drops(env, ns.dry_run)
            if fu.get("nudges"):
                sides = {d.get("side") for d in fu.get("due_nudges", [])}
                ad.reconcile_orphans(env, ns.dry_run)
                if "sell" in sides:
                    logging.info("followup nudges due (sell) → buyer pass (exclusive)")
                    ad.run_pass("buyer", channel, env, ns.dry_run, extra_env={"BAZAAR_FOLLOWUP": "1"})
                if "buy" in sides:
                    logging.info("followup nudges due (buy) → buy pass (exclusive)")
                    ad.run_pass("buy", channel, env, ns.dry_run, extra_env={"BAZAAR_FOLLOWUP": "1"})
            last_followup = time.monotonic()

        # eval gated on `not paused` too (like the buyer/maint/buy passes above) so /pause is a
        # literal full stop — no work of any kind, deterministic or otherwise, until /resume. The
        # billed LLM judge rides the same nightly run when config.eval_judge_nightly is set (default on).
        if not paused and not workers and time.monotonic() - last_eval >= cfg["eval_poll_sec"]:
            if ad._eval_due(env):
                ad.run_eval(env, ns.dry_run, cfg.get("eval_judge_nightly", True))
            last_eval = time.monotonic()

        # UPSTREAM UPDATE CHECK (read-only, throttled): heads-up if a newer Bazaar is available.
        # ENQUEUE here (the _drain_outbox at loop top is the single writer). Read-only, so it does
        # NOT require an exclusive (no-workers) window. Never auto-applies.
        if not paused and time.monotonic() - last_update >= cfg["update_poll_sec"]:
            ad.check_and_notify_update(channel, env, ns.dry_run, via_outbox=True)
            last_update = time.monotonic()

        # Fix D — per-iteration stall guard (observability), shared with the single-flight loop: a
        # hung worker that froze the loop for minutes (the incident's ~7-min stall) is surfaced as a
        # WARN here. Not a kill — _reap's MAX_WORKER_SEC watchdog handles a runaway worker, and the
        # daemon watchdog restarts a truly wedged loop via the stale heartbeat.
        stall = ad.iteration_stall_warning(time.monotonic() - iter_start)
        if stall:
            logging.warning(stall)

        if ns.once:
            break
        time.sleep(1)

    _preempt_all(workers)
    logging.info("supervisor stopping (clean)")
    return 0
