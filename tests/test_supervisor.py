#!/usr/bin/env python3
"""Tests for supervisor.py — the concurrent (opt-in) daemon loop.

Runnable with plain python:  python3 tests/test_supervisor.py

The full loop spawns subprocesses + signals, so we test the PURE launch planner (which markets get
a scoped buyer worker), enabled-market parsing, and the critical safety default: with no config,
max_concurrent_workers is 1 → the daemon stays on its proven single-flight loop (no concurrency).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import supervisor  # noqa: E402  (imports agent_daemon transitively — must be import-side-effect-free)
import agent_daemon  # noqa: E402

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _peek(**markets):
    return {"pending": sum(1 for v in markets.values() if v.get("new")), "markets": markets}


def test_plan_only_new_markets():
    print("planner launches only markets flagged new:")
    peek = _peek(fb={"new": True, "snippet": "hi"}, carousell={"new": False}, ebay={"new": True})
    out = supervisor.plan_buyer_launches(peek, ["fb", "carousell", "ebay"], set(), 5)
    check("launches fb + ebay (new), not carousell", out == ["fb", "ebay"])


def test_plan_skips_busy():
    print("planner skips markets already worked:")
    peek = _peek(fb={"new": True}, carousell={"new": True})
    out = supervisor.plan_buyer_launches(peek, ["fb", "carousell"], {"fb"}, 5)
    check("fb is busy → only carousell", out == ["carousell"])


def test_plan_caps_at_free_slots():
    print("planner respects the free-slot cap:")
    peek = _peek(fb={"new": True}, carousell={"new": True}, ebay={"new": True})
    out = supervisor.plan_buyer_launches(peek, ["fb", "carousell", "ebay"], set(), 2)
    check("at most free_slots launched (2)", len(out) == 2 and out == ["fb", "carousell"])
    check("zero free slots → nothing", supervisor.plan_buyer_launches(peek, ["fb"], set(), 0) == [])


def test_plan_no_markets_section():
    print("planner tolerates a peek with no markets:")
    check("empty peek → nothing", supervisor.plan_buyer_launches({}, ["fb"], set(), 3) == [])


def test_plan_buyer_sweep_one_market_roundrobin():
    print("forced safety-net sweep launches ONE market, round-robin (adaptive concurrency):")
    enabled = ["fb", "carousell", "ebay"]
    check("idx 0 → first eligible", supervisor.plan_buyer_sweep(enabled, set(), 0) == ["fb"])
    check("idx 1 → rotates to second", supervisor.plan_buyer_sweep(enabled, set(), 1) == ["carousell"])
    check("idx wraps", supervisor.plan_buyer_sweep(enabled, set(), 3) == ["fb"])
    check("skips busy markets", supervisor.plan_buyer_sweep(["fb", "carousell"], {"fb"}, 0) == ["carousell"])
    check("none eligible → nothing", supervisor.plan_buyer_sweep(["fb"], {"fb"}, 0) == [])
    check("never fans out past one", len(supervisor.plan_buyer_sweep(enabled, set(), 7)) == 1)


def test_plan_recheck_launches():
    print("count-net forced sweep launches ONLY markets the deterministic recheck flags unhandled:")
    rc = {"markets": {"fb": {"unhandled": False}, "carousell": {"unhandled": True},
                      "ebay": {"unhandled": True}}}
    enabled = ["fb", "carousell", "ebay"]
    check("only flagged markets, in enabled order",
          supervisor.plan_recheck_launches(rc, enabled, set(), 5) == ["carousell", "ebay"])
    check("skips busy", supervisor.plan_recheck_launches(rc, enabled, {"carousell"}, 5) == ["ebay"])
    check("caps at free slots", len(supervisor.plan_recheck_launches(rc, enabled, set(), 1)) == 1)
    check("zero free → nothing", supervisor.plan_recheck_launches(rc, enabled, set(), 0) == [])
    check("all clear → nothing", supervisor.plan_recheck_launches(
        {"markets": {"fb": {"unhandled": False}}}, ["fb"], set(), 5) == [])
    check("no markets section → nothing", supervisor.plan_recheck_launches({}, ["fb"], set(), 3) == [])


def test_sell_threads_from_peek_pure():
    print("C-followup (supervisor poll path): sell_threads_from_peek rebuilds {market:[ids]} from the"
          " buyer_peek result so the SELL memo is advanced ONCE (no second _sell_threads_new probe):")
    bp = {"markets": {"fb": {"sell_threads": ["fb:9988"], "new": True},
                      "carousell": {"sell_threads": [], "new": False}}}
    out = supervisor.sell_threads_from_peek(bp)
    check("rebuilds the per-market thread map", out == {"fb": ["fb:9988"], "carousell": []})
    # peek_thread_for then derives the single-thread hint without advancing the memo again.
    check("single fresh thread → that thread", supervisor.peek_thread_for("fb", out) == "fb:9988")
    check("zero fresh threads → None", supervisor.peek_thread_for("carousell", out) is None)
    # Fail-open on a malformed / old-shape peek.
    check("no markets section → {}", supervisor.sell_threads_from_peek({}) == {})
    check("old-shape market without sell_threads → empty list",
          supervisor.sell_threads_from_peek({"markets": {"fb": {"new": True}}}) == {"fb": []})


def test_plan_continuations_within_budget():
    print("Bug C3: plan_continuations (the SOLE gate) launches a capped market within the budget and"
          " INCREMENTS the counter on launch:")
    # attempt 1 already spent → a continuation is attempt 2, still allowed (cap 2 means <2 launches).
    attempts = {"fb": 1}
    out = supervisor.plan_continuations({"fb"}, set(), 5, attempts)
    check("capped market launched within budget", out["launch"] == ["fb"])
    check("no escalation within budget", out["escalate"] == [])
    check("counter incremented on launch (now at the cap)", attempts["fb"] == 2)


def test_plan_continuations_past_budget():
    print("Bug C3: at/over the budget plan_continuations escalates (not launch) and resets the counter:")
    a1 = {"fb": 2}
    out1 = supervisor.plan_continuations({"fb"}, set(), 5, a1)
    check("at the cap → no launch", out1["launch"] == [])
    check("at the cap → escalate", out1["escalate"] == ["fb"])
    check("counter reset after escalation", "fb" not in a1)
    a2 = {"fb": 9}
    out2 = supervisor.plan_continuations({"fb"}, set(), 5, a2)
    check("over the cap → escalate", out2["escalate"] == ["fb"] and out2["launch"] == [])


def test_plan_continuations_skips_busy_and_respects_slots():
    print("Bug C3: plan_continuations skips busy markets and respects free slots (without spending"
          " the budget of a market it can't fit this tick):")
    check("busy market skipped (no launch, no escalate)",
          supervisor.plan_continuations({"fb"}, {"fb"}, 5, {"fb": 1}) == {"launch": [], "escalate": []})
    attempts = {"fb": 1, "carousell": 1}
    out = supervisor.plan_continuations({"fb", "carousell"}, set(), 1, attempts)
    check("launches capped at free slots (1)", len(out["launch"]) == 1)
    # The market that didn't fit keeps its budget UNTOUCHED so it retries next tick.
    not_launched = ({"fb", "carousell"} - set(out["launch"])).pop()
    check("the unfit market's budget is untouched (retries next tick)", attempts[not_launched] == 1)
    check("zero free slots → no launch (budget not spent on a market with budget left)",
          supervisor.plan_continuations({"fb"}, set(), 0, {"fb": 1}) == {"launch": [], "escalate": []})


def test_plan_continuations_deterministic_order():
    print("Bug C3: plan_continuations launches in deterministic (sorted) order so it is testable:")
    out = supervisor.plan_continuations({"fb", "carousell", "ebay"}, set(), 5,
                                        {"fb": 1, "carousell": 1, "ebay": 1})
    check("sorted launch order", out["launch"] == ["carousell", "ebay", "fb"])


class _FakeProc:
    def __init__(self, rc):
        self._rc = rc
        self.returncode = rc

    def poll(self):
        return self._rc


def test_reap_caphit_reports_capped_without_touching_budget():
    print("Bug C3: _reap on a cap-hit worker REPORTS it as capped but does NOT touch the budget"
          " (plan_continuations is the sole gate — no double-gate):")
    workers = {"fb": {"proc": _FakeProc(supervisor.CAP_HIT_SIGNAL),
                      "holder": "sup:buyer:fb:1", "started": 0.0}}
    attempts = {}
    out = supervisor._reap(workers, cont_attempts=attempts)
    check("capped market released from the live set", "fb" not in workers)
    check("reported as capped", out.get("capped") == ["fb"])
    check("_reap left no 'relaunch'/'escalate' keys (sole gate moved out)",
          "relaunch" not in out and "escalate" not in out)
    check("budget NOT incremented by _reap (the double-gate is gone)", "fb" not in attempts)


def test_reap_then_plan_escalates_after_budget():
    print("Bug C3: after the retry budget, plan_continuations escalates over the channel (the"
          " escalation now lives at the SOLE gate, with run() enqueuing the notify):")
    import os as _os
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            workers = {"fb": {"proc": _FakeProc(supervisor.CAP_HIT_SIGNAL),
                              "holder": "sup:buyer:fb:9", "started": 0.0}}
            attempts = {"fb": supervisor.CONTINUATION_RETRY_CAP}  # budget already spent
            reaped = supervisor._reap(workers, cont_attempts=attempts)
            plan = supervisor.plan_continuations(set(reaped["capped"]), set(), 5, attempts)
            check("no further continuation past the cap", plan["launch"] == [])
            check("escalation scheduled for the capped market", plan["escalate"] == ["fb"])
            # run() enqueues the escalation; emulate that step.
            for market in plan["escalate"]:
                supervisor._escalate_cap_hit(market, None, False)
            outbox = Path(d) / "channel_outbox.jsonl"
            check("a notify was enqueued on the channel outbox", outbox.exists())
            if outbox.exists():
                recs = [_json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
                check("notify mentions the capped market + turn cap",
                      any("fb" in r.get("text", "") and "turn cap" in r.get("text", "") for r in recs))
                check("kind is notify", any(r.get("kind") == "notify" for r in recs))
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)


def test_reap_caphit_budget_matches_daemon_count():
    print("Bug C3: repeated cap-hit reaps + the SOLE gate give exactly CONTINUATION_RETRY_CAP"
          " continuations then ONE escalation — the same total the single-flight daemon produces:")
    attempts = {}
    relaunches = 0
    escalations = 0
    for _ in range(10):
        workers = {"fb": {"proc": _FakeProc(supervisor.CAP_HIT_SIGNAL),
                          "holder": "h", "started": 0.0}}
        reaped = supervisor._reap(workers, cont_attempts=attempts)
        plan = supervisor.plan_continuations(set(reaped["capped"]), set(), 5, attempts)
        relaunches += len(plan["launch"])
        escalations += len(plan["escalate"])
        if plan["escalate"]:
            break
    check("exactly CONTINUATION_RETRY_CAP continuations before escalation",
          relaunches == supervisor.CONTINUATION_RETRY_CAP)
    check("escalated exactly once", escalations == 1)
    check("budget reset after escalation (next cap-hit starts fresh)", "fb" not in attempts)


def test_reap_then_plan_no_double_gate():
    print("Bug C3: _reap + plan_continuations together (the real run-loop sequence) deliver exactly"
          " CONTINUATION_RETRY_CAP continuations then ONE escalation — NO double-gate (was 1 + skip):")
    attempts = {}
    relaunches = 0
    escalations = 0
    # Simulate the supervisor loop: each tick a worker exits capped, _reap REPORTS it as capped (no
    # budget change), then plan_continuations is the SOLE gate that decides launch vs escalate. The
    # two must not BOTH touch the budget (the old bug: _reap incremented AND plan_continuations
    # re-filtered, dropping the second continuation and the escalation).
    for _ in range(10):
        workers = {"fb": {"proc": _FakeProc(supervisor.CAP_HIT_SIGNAL),
                          "holder": "h", "started": 0.0}}
        reaped = supervisor._reap(workers, cont_attempts=attempts)
        plan = supervisor.plan_continuations(set(reaped["capped"]), set(), 5, attempts)
        relaunches += len(plan["launch"])
        escalations += len(plan["escalate"])
        if plan["escalate"]:
            break
    check("exactly CONTINUATION_RETRY_CAP continuations actually LAUNCH (not double-gated to 1)",
          relaunches == supervisor.CONTINUATION_RETRY_CAP)
    check("escalated exactly once (not skipped by the double-gate)", escalations == 1)
    check("budget reset after escalation (next cap-hit starts fresh)", "fb" not in attempts)


def test_reap_natural_exit_no_continuation():
    print("Bug C3: a NATURAL (rc=0) worker exit reaps cleanly — not capped, clears any retry history:")
    workers = {"fb": {"proc": _FakeProc(0), "holder": "sup:buyer:fb:1", "started": 0.0}}
    attempts = {"fb": 1}  # a prior retry history
    out = supervisor._reap(workers, cont_attempts=attempts)
    check("worker reaped", "fb" not in workers)
    check("not reported as capped for a clean exit", out.get("capped") == [])
    check("clean exit clears any retry history", "fb" not in attempts)


class _LiveProc:
    """A worker still running (poll None) — used to exercise the watchdog (MAX_WORKER_SEC) branch."""
    def __init__(self):
        self.returncode = None

    def poll(self):
        return None


def test_reap_watchdog_kill_clears_retry_counter():
    print("Review fix: a watchdog kill (exceeded MAX_WORKER_SEC) is a TIMEOUT, not a cap-hit — it"
          " clears any stale cap-hit retry counter so the next real cap-hit doesn't escalate early:")
    saved_confirm = supervisor._confirm_dead
    supervisor._confirm_dead = lambda *a, **k: None  # don't actually signal a process group in a test
    try:
        # started far in the past → now - started > MAX_WORKER_SEC → watchdog branch.
        workers = {"fb": {"proc": _LiveProc(), "holder": "h", "started": -10_000.0}}
        attempts = {"fb": 1}  # a stale counter from a prior cap-hit cycle
        out = supervisor._reap(workers, cont_attempts=attempts)
        check("watchdog-killed worker removed from the live set", "fb" not in workers)
        check("not reported as capped (a timeout is not a cap-hit)", out.get("capped") == [])
        check("watchdog kill cleared the stale retry counter", "fb" not in attempts)
    finally:
        supervisor._confirm_dead = saved_confirm


def test_continuation_launch_none_rolls_back_budget():
    print("Bug C7: when a continuation launch FAILS (_launch_buyer returns None on a lease race /"
          " Popen OSError) the budget slot must NOT be spent — a later real cap-hit still gets the"
          " full CONTINUATION_RETRY_CAP continuations (mirror single-flight, which only counts a"
          " continuation that actually runs):")
    import argparse
    saved = {}

    def patch(name, fn):
        saved[name] = getattr(supervisor, name)
        setattr(supervisor, name, fn)

    captured = {}

    # _reap reports the capped market AND lets us capture the live cont_attempts dict run() carries,
    # so we can assert the post-iteration budget. We seed cont_attempts[fb]=1 so plan_continuations
    # increments it to 2 at decision time; the failed launch must roll it back to 1, not leave it at 2.
    def fake_reap(workers, cont_attempts=None, dry_run=False):
        if cont_attempts is not None:
            cont_attempts.setdefault("fb", 1)
            captured["cont_attempts"] = cont_attempts
        return {"capped": ["fb"]}

    patch("_reap", fake_reap)
    # The continuation launch RACES and returns None (no worker added) — the bug consumes a slot anyway.
    launch_calls = []

    def fake_launch(market, env, peek, holder, dry_run, hint=None, peek_thread=None):
        launch_calls.append(market)
        return None

    patch("_launch_buyer", fake_launch)
    patch("_heartbeat", lambda *a, **k: None)
    patch("_drain_outbox", lambda *a, **k: None)
    patch("_escalate_cap_hit", lambda *a, **k: None)
    # Neutralize the ad.* probes so the single iteration is otherwise idle.
    ad_saved = {}

    def patch_ad(name, fn):
        ad_saved[name] = getattr(supervisor.ad, name)
        setattr(supervisor.ad, name, fn)

    patch_ad("channel_peek", lambda *a, **k: {"pending": 0, "latest_text": ""})
    patch_ad("buyer_peek", lambda *a, **k: {"pending": 0, "markets": {}})
    patch_ad("buy_peek", lambda *a, **k: {"pending": 0})
    patch_ad("notify_trigger", lambda *a, **k: {"pending": 0, "latest_text": "", "markets": {}})
    patch_ad("tab_park", type("T", (), {"park": staticmethod(lambda *a, **k: None)}))
    patch_ad("_touch_heartbeat", lambda *a, **k: None)
    patch_ad("_source_fingerprint", lambda *a, **k: 0)
    patch_ad("_log_wake_mode", lambda *a, **k: None)
    paused_saved = supervisor.ad.control.is_paused
    supervisor.ad.control.is_paused = lambda: False
    cfg = {"buyer_poll_sec": 10 ** 9, "buy_poll_sec": 10 ** 9, "maint_poll_sec": 10 ** 9,
           "eval_poll_sec": 10 ** 9, "update_poll_sec": 10 ** 9, "followup_poll_sec": 10 ** 9,
           "force_buyer_pass_every": 0}
    ns = argparse.Namespace(once=True, dry_run=False)
    try:
        supervisor.run(cfg, {"adapter": "telegram", "detail": {}}, dict(os.environ), ns, 3, 1)
        check("a continuation launch WAS attempted for the capped market", launch_calls == ["fb"])
        attempts = captured.get("cont_attempts", {})
        check("a failed launch did NOT consume the budget slot (rolled back to 1, not left at 2)",
              attempts.get("fb") == 1)
    except Exception as exc:  # noqa: BLE001
        check(f"iteration did not raise (got {type(exc).__name__}: {exc})", False)
    finally:
        for n, fn in saved.items():
            setattr(supervisor, n, fn)
        for n, fn in ad_saved.items():
            setattr(supervisor.ad, n, fn)
        supervisor.ad.control.is_paused = paused_saved


def test_continuation_hint_does_not_double_advance_sell_memo():
    print("Bug C8: when the continuation block AND the buyer-poll both fire in one iteration, the"
          " SELL memo is advanced AT MOST once — the continuation derives its hint read-only"
          " (sell_actionable_now, persists nothing) instead of a second memo-advancing peek, so the"
          " poll path still sees a genuinely-fresh enumerable market:")
    import argparse
    saved = {}

    def patch(name, fn):
        saved[name] = getattr(supervisor, name)
        setattr(supervisor, name, fn)

    # _reap reports a capped market so the continuation block fires this iteration.
    patch("_reap", lambda workers, cont_attempts=None, dry_run=False: (
        cont_attempts.setdefault("fb", 1) if cont_attempts is not None else None,
        {"capped": ["fb"]})[1])
    launched = []

    def fake_launch(market, env, peek, holder, dry_run, hint=None, peek_thread=None):
        launched.append((market, peek_thread))
        return None  # don't actually start a proc

    patch("_launch_buyer", fake_launch)
    patch("_heartbeat", lambda *a, **k: None)
    patch("_drain_outbox", lambda *a, **k: None)
    patch("_escalate_cap_hit", lambda *a, **k: None)

    # Count memo-ADVANCING peeks. The continuation block MUST NOT advance the SELL memo (the old bug
    # called _sell_threads_new() → inbox_scan.sell_threads_new(), a SECOND advancing probe per tick).
    advancing_calls = {"n": 0}
    saved_threads_new = supervisor.inbox_scan.sell_threads_new

    def counting_threads_new():
        advancing_calls["n"] += 1
        return {"fb": ["fb:111"]}

    supervisor.inbox_scan.sell_threads_new = counting_threads_new

    # The buyer-poll fires too (buyer_poll_sec=0) and flags fb new with a single genuinely-fresh
    # thread, so the poll path must STILL see + scope it (the bug would null this after the
    # continuation's second advance).
    ad_saved = {}

    def patch_ad(name, fn):
        ad_saved[name] = getattr(supervisor.ad, name)
        setattr(supervisor.ad, name, fn)

    patch_ad("channel_peek", lambda *a, **k: {"pending": 0, "latest_text": ""})
    patch_ad("buyer_peek", lambda *a, **k: {"markets": {"fb": {"new": True, "snippet": "hi",
                                                              "sell_threads": ["fb:9988"]}},
                                            "pending": 1})
    patch_ad("buy_peek", lambda *a, **k: {"pending": 0})
    patch_ad("notify_trigger", lambda *a, **k: {"pending": 0, "latest_text": "", "markets": {}})
    patch_ad("tab_park", type("T", (), {"park": staticmethod(lambda *a, **k: None)}))
    patch_ad("_touch_heartbeat", lambda *a, **k: None)
    patch_ad("_source_fingerprint", lambda *a, **k: 0)
    patch_ad("_log_wake_mode", lambda *a, **k: None)
    paused_saved = supervisor.ad.control.is_paused
    supervisor.ad.control.is_paused = lambda: False
    # enabled markets so the poll path considers fb.
    saved_enabled = supervisor.enabled_sell_markets
    supervisor.enabled_sell_markets = lambda *a, **k: ["fb"]
    cfg = {"buyer_poll_sec": 0, "buy_poll_sec": 10 ** 9, "maint_poll_sec": 10 ** 9,
           "eval_poll_sec": 10 ** 9, "update_poll_sec": 10 ** 9, "followup_poll_sec": 10 ** 9,
           "force_buyer_pass_every": 0, "force_buyer_sweep_hours": 0}
    ns = argparse.Namespace(once=True, dry_run=False)
    try:
        supervisor.run(cfg, {"adapter": "telegram", "detail": {}}, dict(os.environ), ns, 3, 1)
        check("the SELL memo is NOT advanced a second time by the continuation block",
              advancing_calls["n"] == 0)
        # Both the continuation launch and the poll launch happened (fb appears for both).
        poll_launch = [pt for (m, pt) in launched if m == "fb"]
        check("the poll path still launched fb (not skipped by a nulled hint)", "fb" in
              [m for (m, _pt) in launched])
        check("the poll path still scoped fb to its genuinely-fresh thread (hint not nulled)",
              "fb:9988" in poll_launch)
    except Exception as exc:  # noqa: BLE001
        check(f"iteration did not raise (got {type(exc).__name__}: {exc})", False)
    finally:
        for n, fn in saved.items():
            setattr(supervisor, n, fn)
        for n, fn in ad_saved.items():
            setattr(supervisor.ad, n, fn)
        supervisor.inbox_scan.sell_threads_new = saved_threads_new
        supervisor.enabled_sell_markets = saved_enabled
        supervisor.ad.control.is_paused = paused_saved


def test_enabled_sell_markets():
    print("enabled_sell_markets parses both config shapes:")
    with tempfile.TemporaryDirectory() as d:
        os.environ["BAZAAR_DATA_DIR"] = d
        try:
            (Path(d) / "seller_config.json").write_text(json.dumps(
                {"marketplaces": {"fb": {"enabled": True}, "carousell": {"enabled": False},
                                  "ebay": {"enabled": True}}}))
            check("object shape → only enabled", set(supervisor.enabled_sell_markets()) == {"fb", "ebay"})
            (Path(d) / "seller_config.json").write_text(json.dumps({"marketplaces": ["fb", "carousell"]}))
            check("legacy array shape → all", set(supervisor.enabled_sell_markets()) == {"fb", "carousell"})
        finally:
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_run_once_idle_no_launches():
    print("loop smoke: one idle iteration completes + launches nothing (probes monkeypatched):")
    import argparse
    saved = {}
    def patch(name, fn):
        saved[name] = getattr(agent_daemon, name)
        setattr(agent_daemon, name, fn)
    patch("channel_peek", lambda *a, **k: {"pending": 0, "latest_text": ""})
    patch("buyer_peek", lambda *a, **k: {"pending": 0, "markets": {}})
    patch("buy_peek", lambda *a, **k: {"pending": 0})
    patch("notify_trigger", lambda *a, **k: {"pending": 0, "latest_text": "", "markets": {}})
    for n in ("_distribution_active", "_inbox_detect_active", "_listing_health_session_active"):
        patch(n, lambda *a, **k: False)
    for n in ("_scan_due", "_inbox_sweep_due", "_eval_due"):
        patch(n, lambda *a, **k: False)
    patch("_listing_health_due", lambda *a, **k: None)
    patch("_followup_due", lambda *a, **k: {"nudges": 0, "drops": 0, "enabled": False, "due_nudges": []})
    patch("run_followup_reconcile", lambda *a, **k: None)
    paused_saved = agent_daemon.control.is_paused
    agent_daemon.control.is_paused = lambda: False
    # update_poll_sec is large so the (network) upstream update check does NOT fire in this idle
    # in-process smoke — it's covered by test_update_check / test_update_notice_hook + the daemon
    # dry-run. The other *_poll_sec are 0 to exercise their gates.
    cfg = {"buyer_poll_sec": 0, "buy_poll_sec": 0, "maint_poll_sec": 0, "eval_poll_sec": 0,
           "update_poll_sec": 10 ** 9, "followup_poll_sec": 0, "force_buyer_pass_every": 0}
    ns = argparse.Namespace(once=True, dry_run=True)
    try:
        rc = supervisor.run(cfg, {"adapter": "telegram", "detail": {}}, dict(os.environ), ns, 3, 1)
        check("idle iteration returns 0", rc == 0)
    except Exception as exc:  # noqa: BLE001 — any crash in the loop body is a failure
        check(f"idle iteration did not raise (got {type(exc).__name__}: {exc})", False)
    finally:
        for n, fn in saved.items():
            setattr(agent_daemon, n, fn)
        agent_daemon.control.is_paused = paused_saved


def test_confirm_dead_kills_grandchild():
    print("CRITICAL fix: _confirm_dead kills the whole process GROUP (no orphaned grandchild):")
    import subprocess as sp
    # A wrapper (sh) that spawns a grandchild `sleep` and waits — mirrors run_pass.sh → harness_run
    # → claude. start_new_session=True puts both in one group so killpg reaches the grandchild.
    proc = sp.Popen(["sh", "-c", "sleep 60 & echo $! ; wait"], stdout=sp.PIPE, text=True,
                    start_new_session=True)
    gc_pid = int(proc.stdout.readline().strip())
    supervisor._confirm_dead(proc, grace=3)
    check("wrapper is dead", proc.poll() is not None)
    gone = False
    try:
        os.kill(gc_pid, 0)
    except ProcessLookupError:
        gone = True
    check("grandchild killed too (orphan bug fixed)", gone)


def test_int_or_coercion():
    print("max_concurrent_workers coercion (a fat-fingered value never crashes the dispatch):")
    check("'2' (string) → 2", agent_daemon._int_or("2", 1) == 2)
    check("None → default 1", agent_daemon._int_or(None, 1) == 1)
    check("garbage → default 1", agent_daemon._int_or("lots", 1) == 1)
    check("0 clamps up to 1", agent_daemon._int_or(0, 1) == 1)


def test_instance_lock_singleton():
    print("daemon singleton: a second instance lock is refused (now PID-aware → dict result):")
    first = agent_daemon._acquire_instance_lock()
    if not first["acquired"]:
        check("a daemon already holds the lock → skip (trivially safe)", True)
        return
    try:
        second = agent_daemon._acquire_instance_lock()
        check("second acquire is refused while first is held", second["acquired"] is False)
        check("refusal reports the live holder pid (us)", second["holder_pid"] == os.getpid())
        check("refusal reports the holder is alive", second["holder_alive"] is True)
    finally:
        os.close(first["fd"])
        try:
            agent_daemon.INSTANCE_LOCK.unlink()
        except OSError:
            pass


def test_concurrency_reflects_config():
    print("max_concurrent_workers wiring: load_config reflects data/config.json (now defaults to 2):")
    import json as _json
    cfg = agent_daemon.load_config()
    raw = _json.load(open(agent_daemon.CONFIG_PATH))
    expected = agent_daemon._int_or(raw.get("max_concurrent_workers", 1), 1)
    mcw = cfg.get("max_concurrent_workers")
    check("reflects data/config.json", mcw == expected)
    check("is a valid int >= 1 (absent/garbage would fall back safely)", isinstance(mcw, int) and mcw >= 1)


def test_source_change_routes_through_relaunch_self():
    print("Fix D: a source-change exit routes through agent_daemon.relaunch_self (clean single bounce):")
    import argparse
    saved = {}

    def patch(name, fn):
        saved[name] = getattr(agent_daemon, name)
        setattr(agent_daemon, name, fn)

    # Make the FIRST fingerprint read match (startup capture), the SECOND differ → trigger the bounce.
    fps = iter([1000, 2000, 2000, 2000])
    patch("_source_fingerprint", lambda: next(fps, 2000))
    patch("channel_peek", lambda *a, **k: {"pending": 0, "latest_text": ""})
    patch("buyer_peek", lambda *a, **k: {"pending": 0, "markets": {}})
    patch("buy_peek", lambda *a, **k: {"pending": 0})
    patch("notify_trigger", lambda *a, **k: {"pending": 0, "latest_text": "", "markets": {}})
    relaunches = []
    patch("relaunch_self", lambda: relaunches.append(True))
    paused_saved = agent_daemon.control.is_paused
    agent_daemon.control.is_paused = lambda: False
    cfg = {"buyer_poll_sec": 10 ** 9, "buy_poll_sec": 10 ** 9, "maint_poll_sec": 10 ** 9,
           "eval_poll_sec": 10 ** 9, "update_poll_sec": 10 ** 9, "followup_poll_sec": 10 ** 9,
           "force_buyer_pass_every": 0}
    ns = argparse.Namespace(once=False, dry_run=True)
    try:
        rc = supervisor.run(cfg, {"adapter": "telegram", "detail": {}}, dict(os.environ), ns, 3, 1)
        check("clean exit (0)", rc == 0)
        check("relaunch_self called exactly once on source change", relaunches == [True])
    finally:
        for n, fn in saved.items():
            setattr(agent_daemon, n, fn)
        agent_daemon.control.is_paused = paused_saved


def test_supervisor_stall_guard_warns_over_budget():
    print("Fix D: the supervisor shares agent_daemon's per-iteration stall guard (WARN over budget):")
    over = agent_daemon.iteration_stall_warning(agent_daemon.LOOP_ITER_BUDGET + 1,
                                                agent_daemon.LOOP_ITER_BUDGET)
    check("over budget → a WARN message", isinstance(over, str) and over != "")
    check("under budget → no message", agent_daemon.iteration_stall_warning(
        1.0, agent_daemon.LOOP_ITER_BUDGET) is None)


if __name__ == "__main__":
    print("supervisor tests\n")
    test_plan_only_new_markets()
    test_plan_skips_busy()
    test_plan_caps_at_free_slots()
    test_plan_no_markets_section()
    test_plan_buyer_sweep_one_market_roundrobin()
    test_plan_recheck_launches()
    test_sell_threads_from_peek_pure()
    test_plan_continuations_within_budget()
    test_plan_continuations_past_budget()
    test_plan_continuations_skips_busy_and_respects_slots()
    test_plan_continuations_deterministic_order()
    test_reap_caphit_reports_capped_without_touching_budget()
    test_reap_then_plan_escalates_after_budget()
    test_reap_caphit_budget_matches_daemon_count()
    test_reap_then_plan_no_double_gate()
    test_reap_natural_exit_no_continuation()
    test_reap_watchdog_kill_clears_retry_counter()
    test_continuation_launch_none_rolls_back_budget()
    test_continuation_hint_does_not_double_advance_sell_memo()
    test_enabled_sell_markets()
    test_run_once_idle_no_launches()
    test_confirm_dead_kills_grandchild()
    test_int_or_coercion()
    test_instance_lock_singleton()
    test_concurrency_reflects_config()
    test_source_change_routes_through_relaunch_self()
    test_supervisor_stall_guard_warns_over_budget()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
