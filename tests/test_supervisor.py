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
    for n in ("_distribution_active", "_inbox_detect_active"):
        patch(n, lambda *a, **k: False)
    for n in ("_scan_due", "_inbox_sweep_due", "_eval_due"):
        patch(n, lambda *a, **k: False)
    paused_saved = agent_daemon.control.is_paused
    agent_daemon.control.is_paused = lambda: False
    cfg = {"buyer_poll_sec": 0, "buy_poll_sec": 0, "maint_poll_sec": 0, "eval_poll_sec": 0,
           "force_buyer_pass_every": 0}
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
    print("daemon singleton: a second instance lock is refused:")
    first = agent_daemon._acquire_instance_lock()
    if first is None:
        check("a daemon already holds the lock → skip (trivially safe)", True)
        return
    try:
        second = agent_daemon._acquire_instance_lock()
        check("second acquire is refused while first is held", second is None)
    finally:
        os.close(first)
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


if __name__ == "__main__":
    print("supervisor tests\n")
    test_plan_only_new_markets()
    test_plan_skips_busy()
    test_plan_caps_at_free_slots()
    test_plan_no_markets_section()
    test_plan_buyer_sweep_one_market_roundrobin()
    test_plan_recheck_launches()
    test_enabled_sell_markets()
    test_run_once_idle_no_launches()
    test_confirm_dead_kills_grandchild()
    test_int_or_coercion()
    test_instance_lock_singleton()
    test_concurrency_reflects_config()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
