#!/usr/bin/env python3
"""Tests for pacing_gate — the atomic account-safety pacing engine.

Runnable with plain python (no pytest needed):

    python3 tests/test_pacing_gate.py

Focus: the ONE invariant that makes concurrency safe — under a fixed cap, no matter
how many workers call `reserve` at once, the number that get a "go" never exceeds the
remaining budget. Plus the deterministic decision logic (quiet hours, window pruning,
immutability of state updates).
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import pacing_gate as pg  # noqa: E402

NOON_UTC = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)  # not inside quiet [23,8]

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _cfg(cap=3, delay=(0, 0), idelay=(0, 0), quiet=(23, 8), window_seconds=3600):
    return {
        "cap": cap,
        "delay_min": delay[0],
        "delay_max": delay[1],
        "idelay_min": idelay[0],
        "idelay_max": idelay[1],
        "quiet_start": quiet[0],
        "quiet_end": quiet[1],
        "window_seconds": window_seconds,
    }


def _ts(minutes_ago):
    secs = minutes_ago * 60
    return (NOON_UTC.timestamp() - secs)


def _iso(minutes_ago):
    return datetime.fromtimestamp(_ts(minutes_ago), tz=timezone.utc).isoformat()


def test_prune_drops_old_keeps_recent():
    print("prune window:")
    actions = [
        {"ts": _iso(120), "kind": "reply"},   # 2h ago -> drop
        {"ts": _iso(59), "kind": "reply"},     # 59m ago -> keep
        {"ts": _iso(10), "kind": "click"},     # 10m ago -> keep
    ]
    kept = pg.prune_actions(actions, NOON_UTC, 3600)
    check("drops actions older than the window", len(kept) == 2)
    check("does not mutate the input list", len(actions) == 3)


def test_record_action_immutable():
    print("record immutability:")
    state = {"fb": {"actions": [{"ts": _iso(5), "kind": "reply"}]}}
    new_state = pg.record_action(state, "fb", NOON_UTC.isoformat(), "click")
    check("appends to the marketplace ledger", len(new_state["fb"]["actions"]) == 2)
    check("original state unchanged", len(state["fb"]["actions"]) == 1)
    check("creates a ledger for a new marketplace",
          len(pg.record_action({}, "carousell", NOON_UTC.isoformat(), "reply")["carousell"]["actions"]) == 1)


def test_quiet_hours_wrap():
    print("quiet hours (wrap midnight, [23,8]):")
    check("23:00 is quiet", pg.in_quiet_hours(23, 23, 8) is True)
    check("00:00 is quiet", pg.in_quiet_hours(0, 23, 8) is True)
    check("07:00 is quiet", pg.in_quiet_hours(7, 23, 8) is True)
    check("08:00 is NOT quiet (end exclusive)", pg.in_quiet_hours(8, 23, 8) is False)
    check("12:00 is NOT quiet", pg.in_quiet_hours(12, 23, 8) is False)
    check("22:00 is NOT quiet", pg.in_quiet_hours(22, 23, 8) is False)
    print("quiet hours (non-wrap, [1,5]):")
    check("03:00 is quiet", pg.in_quiet_hours(3, 1, 5) is True)
    check("06:00 is NOT quiet", pg.in_quiet_hours(6, 1, 5) is False)


def test_evaluate_under_cap_go_records():
    print("evaluate under cap -> go + records:")
    state = {"fb": {"actions": [{"ts": _iso(5), "kind": "reply"}]}}
    result, new_state = pg.evaluate(state, "fb", "reply", NOON_UTC, _cfg(cap=3))
    check("decision is go", result["decision"] == "go")
    check("records the action", new_state is not None and len(new_state["fb"]["actions"]) == 2)
    check("count reflects pre-existing in-window action", result["count"] == 1)
    check("delay within reply_delay_sec range", 0 <= result["delay_sec"] <= 0)


def test_evaluate_at_cap_waits_without_recording():
    print("evaluate at cap -> wait + no record:")
    actions = [{"ts": _iso(m), "kind": "reply"} for m in (1, 20, 40)]  # 3 in-window
    state = {"carousell": {"actions": actions}}
    result, new_state = pg.evaluate(state, "carousell", "reply", NOON_UTC, _cfg(cap=3))
    check("decision is wait", result["decision"] == "wait")
    check("does NOT record at the cap", new_state is None)
    check("count equals cap", result["count"] == 3)
    check("suggests a positive retry delay", result["delay_sec"] > 0)


def test_evaluate_quiet_hours_no_record():
    print("evaluate inside quiet hours -> quiet + no record:")
    quiet_now = datetime(2026, 6, 25, 2, 0, 0, tzinfo=timezone.utc)  # 02:00 inside [23,8]
    result, new_state = pg.evaluate({}, "fb", "reply", quiet_now, _cfg(cap=3))
    check("decision is quiet", result["decision"] == "quiet")
    check("does NOT record during quiet hours", new_state is None)


def test_interactive_mode_draws_from_interactive_range():
    print("interactive mode -> go, jitter from interactive range (not the reply range):")
    # reply range is a tight 50..50; interactive range a distinct 5..5 — the delay proves which won.
    cfg = _cfg(cap=3, delay=(50, 50), idelay=(5, 5))
    result, new_state = pg.evaluate({}, "fb", "reply", NOON_UTC, cfg, mode="interactive")
    check("decision is go", result["decision"] == "go")
    check("records the action", new_state is not None and len(new_state["fb"]["actions"]) == 1)
    check("delay comes from the interactive range", result["delay_sec"] == 5.0)
    check("result echoes the mode", result["mode"] == "interactive")
    # The same state, default (unattended) mode, must draw the longer reply-range jitter.
    unatt, _ = pg.evaluate({}, "fb", "reply", NOON_UTC, cfg)
    check("default mode draws the reply (unattended) range", unatt["delay_sec"] == 50.0)
    check("default mode is unattended", unatt["mode"] == "unattended")


def test_interactive_mode_still_respects_cap():
    print("interactive mode does NOT relax the hourly cap:")
    actions = [{"ts": _iso(m), "kind": "reply"} for m in (1, 20, 40)]  # 3 in-window, cap=3
    state = {"carousell": {"actions": actions}}
    result, new_state = pg.evaluate(state, "carousell", "reply", NOON_UTC, _cfg(cap=3), mode="interactive")
    check("decision is wait even in interactive mode", result["decision"] == "wait")
    check("does NOT record at the cap", new_state is None)


def test_interactive_mode_still_quiet():
    print("interactive mode does NOT bypass quiet hours:")
    quiet_now = datetime(2026, 6, 25, 2, 0, 0, tzinfo=timezone.utc)  # 02:00 inside [23,8]
    result, new_state = pg.evaluate({}, "fb", "reply", quiet_now, _cfg(cap=3), mode="interactive")
    check("decision is quiet even in interactive mode", result["decision"] == "quiet")
    check("does NOT record during quiet hours", new_state is None)


def test_cli_mode_flag_selects_range():
    print("CLI --mode selects the jitter range; absent --mode == legacy unattended:")
    with tempfile.TemporaryDirectory() as d:
        # Distinct, deterministic ranges so the returned delay_sec proves which range was used.
        (Path(d) / "config.json").write_text(json.dumps({
            "max_actions_per_hour": 9, "reply_delay_sec": [50, 50],
            "interactive_reply_delay_sec": [5, 5], "quiet_hours": [0, 0],
        }))
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "pacing_gate.py"), "reserve",
                "--marketplace", "fb", "--kind", "reply"]
        interactive = subprocess.run(base + ["--mode", "interactive"],
                                     capture_output=True, text=True, env=env)
        check("interactive reserve exits 0", interactive.returncode == 0)
        ip = json.loads(interactive.stdout)
        check("interactive uses the interactive range", ip["delay_sec"] == 5.0)
        check("interactive reports its mode", ip["mode"] == "interactive")
        default = subprocess.run(base, capture_output=True, text=True, env=env)
        check("default reserve exits 0", default.returncode == 0)
        dp = json.loads(default.stdout)
        check("absent --mode reproduces the unattended range", dp["delay_sec"] == 50.0)
        check("absent --mode reports unattended", dp["mode"] == "unattended")
        bad = subprocess.run(base + ["--mode", "bogus"], capture_output=True, text=True, env=env)
        check("an unknown --mode value exits nonzero", bad.returncode != 0)


def test_load_cfg_interactive_delay_validation():
    print("load_cfg: interactive_reply_delay_sec validated symmetrically; missing key falls back:")
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "config.json"
        # Missing key -> falls back to DEFAULT_INTERACTIVE_DELAY, no raise.
        cfg_path.write_text(json.dumps({"max_actions_per_hour": 3}))
        cfg = pg.load_cfg(cfg_path)
        check("missing interactive key falls back to default min",
              cfg["idelay_min"] == float(pg.DEFAULT_INTERACTIVE_DELAY[0]))
        check("missing interactive key falls back to default max",
              cfg["idelay_max"] == float(pg.DEFAULT_INTERACTIVE_DELAY[1]))
        # Malformed (min > max) -> raises, like reply_delay_sec.
        cfg_path.write_text(json.dumps({"interactive_reply_delay_sec": [9, 1]}))
        raised = False
        try:
            pg.load_cfg(cfg_path)
        except ValueError:
            raised = True
        check("min > max interactive range raises", raised)
        # Malformed (wrong shape) -> raises.
        cfg_path.write_text(json.dumps({"interactive_reply_delay_sec": [3]}))
        raised = False
        try:
            pg.load_cfg(cfg_path)
        except ValueError:
            raised = True
        check("wrong-shape interactive range raises", raised)


def test_cli_go_then_status():
    print("CLI reserve -> status (isolated data dir via BAZAAR_DATA_DIR, real wall clock):")
    with tempfile.TemporaryDirectory() as d:
        # quiet_hours [0,0] => never quiet, so the test passes regardless of wall-clock hour.
        (Path(d) / "config.json").write_text(json.dumps({
            "max_actions_per_hour": 3, "reply_delay_sec": [0, 0], "quiet_hours": [0, 0],
        }))
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "pacing_gate.py")]
        out = subprocess.run(base + ["reserve", "--marketplace", "fb", "--kind", "reply"],
                             capture_output=True, text=True, env=env)
        check("reserve exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("reserve returns go", payload.get("decision") == "go")
        st = subprocess.run(base + ["status", "--marketplace", "fb"],
                            capture_output=True, text=True, env=env)
        check("status exits 0", st.returncode == 0)
        check("status shows 1 recorded action", json.loads(st.stdout)["count"] == 1)


def test_concurrent_reserve_respects_cap():
    print("INVARIANT: concurrent reserves never exceed remaining budget:")
    cap = 3
    n_workers = 10
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config.json").write_text(json.dumps({
            "max_actions_per_hour": cap, "reply_delay_sec": [0, 0], "quiet_hours": [0, 0],
        }))
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "pacing_gate.py"), "reserve",
                "--marketplace", "fb", "--kind", "reply"]
        # Launch all at once; flock must serialize the check-and-record.
        procs = [subprocess.Popen(base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(n_workers)]
        results = []
        for p in procs:
            out, _ = p.communicate()
            results.append(json.loads(out)["decision"])
        gos = results.count("go")
        check(f"exactly {cap} gos out of {n_workers} concurrent reservers (got {gos})", gos == cap)
        check("the rest are told to wait", results.count("wait") == n_workers - cap)
        # And the persisted ledger holds exactly `cap` recorded actions.
        persisted = json.loads((Path(d) / "pacing_state.json").read_text())
        check("ledger persisted exactly cap actions",
              len(persisted["fb"]["actions"]) == cap)


def test_now_clamp_rejects_time_travel():
    print("hardening: --now far from wall clock is rejected (no window time-travel):")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config.json").write_text(json.dumps({"max_actions_per_hour": 3}))
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "pacing_gate.py"), "reserve",
                "--marketplace", "fb"]
        future = subprocess.run(base + ["--now", "2099-01-01T00:00:00Z"],
                                capture_output=True, text=True, env=env)
        check("future --now exits nonzero", future.returncode != 0)
        past = subprocess.run(base + ["--now", "2000-01-01T00:00:00Z"],
                              capture_output=True, text=True, env=env)
        check("distant-past --now exits nonzero", past.returncode != 0)


def test_cap_below_one_rejected():
    print("hardening: max_actions_per_hour < 1 is rejected (no silent lockout):")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config.json").write_text(json.dumps({"max_actions_per_hour": 0}))
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "pacing_gate.py"),
                              "reserve", "--marketplace", "fb"],
                             capture_output=True, text=True, env=env)
        check("cap=0 config exits nonzero", out.returncode != 0)


def test_hard_ceiling_clamps_cap():
    print("hardening: an over-large cap is clamped DOWN to the safety ceiling:")
    cfg = _cfg(cap=10_000)
    # load_cfg is what clamps; emulate via the same ceiling constant.
    check("ceiling constant exists", isinstance(pg.HARD_CAP_CEILING, int) and pg.HARD_CAP_CEILING > 0)
    # A cap above the ceiling must never let more than the ceiling through.
    state = {}
    gos = 0
    now = NOON_UTC
    for _ in range(pg.HARD_CAP_CEILING + 5):
        result, new_state = pg.evaluate(state, "fb", "reply", now,
                                        {**_cfg(cap=pg.HARD_CAP_CEILING), "delay_max": 0})
        if result["decision"] == "go":
            gos += 1
            state = new_state
    check("evaluate honors the ceiling cap exactly", gos == pg.HARD_CAP_CEILING)


def test_reply_delay_max_clamped_below_grace():
    print("J2: an unbounded reply_delay_sec max is clamped DOWN to the hard ceiling, and that ceiling"
          " is strictly below journal_reconcile.GRACE_SEC so a healthy in-flight intent can't outlive"
          " the fold floor:")
    import journal_reconcile as jr  # the GRACE_SEC the ceiling must stay below
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "config.json"
        # An operator/tampered config asks for a 1200s reply delay — well above the ceiling.
        cfg_path.write_text(json.dumps({"reply_delay_sec": [0, 1200],
                                        "interactive_reply_delay_sec": [0, 1200]}))
        cfg = pg.load_cfg(cfg_path)
        check("HARD_DELAY_CEILING_SEC exists",
              isinstance(pg.HARD_DELAY_CEILING_SEC, (int, float)) and pg.HARD_DELAY_CEILING_SEC > 0)
        check("reply_delay_sec max clamped to the ceiling",
              cfg["delay_max"] == float(pg.HARD_DELAY_CEILING_SEC))
        check("interactive_reply_delay_sec max clamped to the ceiling",
              cfg["idelay_max"] == float(pg.HARD_DELAY_CEILING_SEC))
        check("min is preserved (only the max is clamped)", cfg["delay_min"] == 0.0)
        check("the clamped max is strictly below GRACE_SEC (a healthy intent is never folded)",
              pg.HARD_DELAY_CEILING_SEC < jr.GRACE_SEC)
        # A within-ceiling config is left untouched (clamp only ever tightens).
        cfg_path.write_text(json.dumps({"reply_delay_sec": [10, 200]}))
        cfg2 = pg.load_cfg(cfg_path)
        check("a within-ceiling max is left unchanged", cfg2["delay_max"] == 200.0)


def test_maybe_block_sleeps_go_and_zeroes_delay():
    print("_maybe_block (B1) sleeps a 'go' delay server-side, then returns delay_sec=0 + slept_sec:")
    slept = []
    saved = pg.time.sleep
    pg.time.sleep = lambda s: slept.append(s)
    try:
        out = pg._maybe_block({"decision": "go", "delay_sec": 47.0, "count": 1})
    finally:
        pg.time.sleep = saved
    check("slept the requested delay once", slept == [47.0])
    check("delay_sec zeroed for the caller (already waited)", out["delay_sec"] == 0)
    check("slept_sec records what was waited", out["slept_sec"] == 47.0)
    check("blocked flag set", out.get("blocked") is True)
    check("decision preserved", out["decision"] == "go")


def test_maybe_block_leaves_wait_and_quiet_untouched():
    print("_maybe_block never sleeps or alters a wait/quiet decision (the caller handles those):")
    slept = []
    saved = pg.time.sleep
    pg.time.sleep = lambda s: slept.append(s)
    try:
        for dec in ("wait", "quiet"):
            res = pg._maybe_block({"decision": dec, "delay_sec": 99})
            check(f"{dec} returned untouched", res == {"decision": dec, "delay_sec": 99})
    finally:
        pg.time.sleep = saved
    check("never slept for a non-go decision", slept == [])


def test_maybe_block_clamps_to_ceiling():
    print("_maybe_block clamps a runaway delay to HARD_DELAY_CEILING_SEC:")
    slept = []
    saved = pg.time.sleep
    pg.time.sleep = lambda s: slept.append(s)
    try:
        out = pg._maybe_block({"decision": "go", "delay_sec": 99999})
    finally:
        pg.time.sleep = saved
    check("slept at most the ceiling", slept == [float(pg.HARD_DELAY_CEILING_SEC)])
    check("slept_sec reflects the clamp", out["slept_sec"] == float(pg.HARD_DELAY_CEILING_SEC))


def test_cli_reserve_block_flag():
    print("CLI reserve --block reports it waited (delay 0 here, so no real sleep) and zeroes delay_sec:")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config.json").write_text(json.dumps({
            "max_actions_per_hour": 9, "reply_delay_sec": [0, 0],
            "interactive_reply_delay_sec": [0, 0], "quiet_hours": [0, 0],
        }))
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        base = [sys.executable, str(ROOT / "bin" / "pacing_gate.py"), "reserve",
                "--marketplace", "fb", "--kind", "reply", "--block"]
        out = subprocess.run(base, capture_output=True, text=True, env=env)
        check("reserve --block exits 0", out.returncode == 0)
        res = json.loads(out.stdout)
        check("decision go", res["decision"] == "go")
        check("blocked flag set", res.get("blocked") is True)
        check("delay zeroed (the wait already happened server-side)", res["delay_sec"] == 0)
        check("slept_sec present (0.0 with a zero configured delay)", res.get("slept_sec") == 0.0)


def test_bad_input_rejected():
    print("input validation:")
    base = [sys.executable, str(ROOT / "bin" / "pacing_gate.py")]
    bad = [
        ["reserve"],                                  # missing --marketplace
        ["reserve", "--marketplace", ""],             # empty marketplace
        ["bogus", "--marketplace", "fb"],             # unknown command
    ]
    ok = True
    for args in bad:
        proc = subprocess.run(base + args, capture_output=True, text=True)
        if proc.returncode == 0:
            ok = False
            print(f"    accepted bad input: {args}")
    check("malformed input exits nonzero", ok)


if __name__ == "__main__":
    print("pacing_gate tests\n")
    test_prune_drops_old_keeps_recent()
    test_record_action_immutable()
    test_quiet_hours_wrap()
    test_evaluate_under_cap_go_records()
    test_evaluate_at_cap_waits_without_recording()
    test_evaluate_quiet_hours_no_record()
    test_interactive_mode_draws_from_interactive_range()
    test_interactive_mode_still_respects_cap()
    test_interactive_mode_still_quiet()
    test_cli_mode_flag_selects_range()
    test_load_cfg_interactive_delay_validation()
    test_cli_go_then_status()
    test_concurrent_reserve_respects_cap()
    test_now_clamp_rejects_time_travel()
    test_cap_below_one_rejected()
    test_hard_ceiling_clamps_cap()
    test_reply_delay_max_clamped_below_grace()
    test_maybe_block_sleeps_go_and_zeroes_delay()
    test_maybe_block_leaves_wait_and_quiet_untouched()
    test_maybe_block_clamps_to_ceiling()
    test_cli_reserve_block_flag()
    test_bad_input_rejected()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
