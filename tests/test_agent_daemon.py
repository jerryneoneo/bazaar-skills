#!/usr/bin/env python3
"""Tests for agent_daemon.py — the always-on loop's cheap probe seams.

    python3 tests/test_agent_daemon.py

The full async loop is integration-tested (test_supervisor `--once --dry-run`; pause in
test_pause_interrupt). Here we unit-test the parse + fail-open contract of the non-LLM probes
(`buyer_peek` / `buy_peek` / `channel_peek`) and the per-adapter `_peek_cmd` dispatch — the seams
that, if they raised instead of failing open, would crash a whole pass on a transient hiccup.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import agent_daemon  # noqa: E402  (must be import-side-effect-free)

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def with_run(fake_run, body):
    """Swap agent_daemon.subprocess.run for the duration of body(), then restore."""
    saved = agent_daemon.subprocess.run
    agent_daemon.subprocess.run = fake_run
    try:
        body()
    finally:
        agent_daemon.subprocess.run = saved


def test_buyer_peek_parses_success():
    print("buyer_peek: parses the probe's JSON on rc=0:")
    def body():
        out = agent_daemon.buyer_peek({})
        check("returns the parsed dict", out == {"pending": 2, "markets": {"fb": {"new": True}}})
    with_run(lambda *a, **k: FakeProc(0, '{"pending": 2, "markets": {"fb": {"new": true}}}'), body)


def test_buyer_peek_failopen_on_rc():
    print("buyer_peek: rc!=0 → fail-open {pending:0} (never crash the loop):")
    def body():
        check("fail-open dict", agent_daemon.buyer_peek({}) == {"pending": 0, "latest_text": ""})
    with_run(lambda *a, **k: FakeProc(1, "", "boom"), body)


def test_buyer_peek_failopen_on_bad_json():
    print("buyer_peek: rc=0 but garbage stdout → fail-open (ValueError swallowed):")
    def body():
        check("fail-open dict", agent_daemon.buyer_peek({}) == {"pending": 0, "latest_text": ""})
    with_run(lambda *a, **k: FakeProc(0, "not json at all"), body)


def test_buyer_peek_failopen_on_exception():
    print("buyer_peek: subprocess error → fail-open (no raise):")
    def boom(*a, **k):
        raise agent_daemon.subprocess.SubprocessError("timed out")
    def body():
        check("fail-open dict (did not raise)",
              agent_daemon.buyer_peek({}) == {"pending": 0, "latest_text": ""})
    with_run(boom, body)


def test_buy_peek_success_and_failopen():
    print("buy_peek: parses JSON, and fails open with want_id=None on rc!=0:")
    def ok():
        check("parsed", agent_daemon.buy_peek({}) == {"pending": 1, "want_id": "w1"})
    with_run(lambda *a, **k: FakeProc(0, '{"pending": 1, "want_id": "w1"}'), ok)

    def fail():
        check("fail-open carries want_id None",
              agent_daemon.buy_peek({}) == {"pending": 0, "latest_text": "", "want_id": None})
    with_run(lambda *a, **k: FakeProc(2, "", "nope"), fail)


def test_channel_peek_console_skips_subprocess():
    print("channel_peek: console has no daemon → {pending:0} without spawning a peek:")
    def must_not_run(*a, **k):
        raise AssertionError("console must not invoke a peek subprocess")
    def body():
        out = agent_daemon.channel_peek({"adapter": "console", "detail": {}}, {}, 5)
        check("fail-open dict", out == {"pending": 0, "latest_text": ""})
    with_run(must_not_run, body)


def test_channel_peek_parses_success():
    print("channel_peek: parses the adapter peek JSON on rc=0:")
    def body():
        out = agent_daemon.channel_peek({"adapter": "telegram", "detail": {}}, {}, 5)
        check("parsed", out == {"pending": 1, "latest_text": "hi"})
    with_run(lambda *a, **k: FakeProc(0, '{"pending": 1, "latest_text": "hi"}'), body)


def test_buyer_force_due_count_net():
    print("buyer_force_due: count-based net fires at/after force_every empty peeks:")
    check("below threshold → not due", agent_daemon.buyer_force_due(1, 2, 0.0, 0.0)[0] is False)
    check("at threshold → due", agent_daemon.buyer_force_due(2, 2, 0.0, 0.0)[0] is True)
    check("force_every=0 disables the count net", agent_daemon.buyer_force_due(99, 0, 0.0, 0.0)[0] is False)


def test_buyer_force_due_time_floor():
    print("buyer_force_due: absolute time floor is the strand backstop when the count net is off:")
    floor = 2 * 3600.0
    check("under the floor → not due", agent_daemon.buyer_force_due(0, 0, floor - 1, floor)[0] is False)
    check("at/over the floor → due", agent_daemon.buyer_force_due(0, 0, floor, floor)[0] is True)
    check("floor=0 disables it", agent_daemon.buyer_force_due(0, 0, 9_999_999, 0.0)[0] is False)
    check("reason names the floor", "floor" in agent_daemon.buyer_force_due(0, 0, floor, floor)[1])


def test_buyer_recheck_failopen_conservative():
    print("buyer_recheck helper: parses JSON, and fails open CONSERVATIVELY (unhandled=1) on error:")
    def ok():
        check("parsed", agent_daemon.buyer_recheck({}) == {"unhandled": 0, "markets": {}})
    with_run(lambda *a, **k: FakeProc(0, '{"unhandled": 0, "markets": {}}'), ok)

    def fail():
        out = agent_daemon.buyer_recheck({})
        # CONSERVATIVE (opposite of buyer_peek): a recheck that can't run must NOT say "clear".
        check("fail-open reports unhandled (fires the LLM pass)", out.get("unhandled") == 1)
    with_run(lambda *a, **k: FakeProc(1, "", "boom"), fail)


def test_buyer_action_decision():
    print("buyer_action: pure decision for a buyer poll (pass | skip | idle):")
    check("real pending → pass", agent_daemon.buyer_action(2, False, False, None) == "pass")
    check("not forced, nothing → idle", agent_daemon.buyer_action(0, False, False, None) == "idle")
    check("floor due → pass even if recheck unrun", agent_daemon.buyer_action(0, True, True, None) == "pass")
    check("count-net force + recheck unhandled → pass",
          agent_daemon.buyer_action(0, True, False, 1) == "pass")
    check("count-net force + recheck clear → skip (~0 tokens)",
          agent_daemon.buyer_action(0, True, False, 0) == "skip")


def test_load_config_sweep_floor():
    print("load_config exposes force_buyer_sweep_hours (the new absolute floor knob):")
    cfg = agent_daemon.load_config()
    val = cfg.get("force_buyer_sweep_hours")
    check("present and numeric >= 0", isinstance(val, (int, float)) and val >= 0)


def test_wake_mode_reflects_fda():
    print("wake_mode: 'instant' when the Notification Center is readable, else 'standard' (fail-open):")
    saved = agent_daemon.notify_db.available
    try:
        agent_daemon.notify_db.available = lambda: True
        check("FDA readable → instant", agent_daemon.wake_mode() == "instant")
        agent_daemon.notify_db.available = lambda: False
        check("FDA not granted → standard", agent_daemon.wake_mode() == "standard")
        agent_daemon.notify_db.available = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        check("error → standard (fail-open, no raise)", agent_daemon.wake_mode() == "standard")
    finally:
        agent_daemon.notify_db.available = saved


def test_buyer_continuation_action_pure():
    print("Fix C: buyer_continuation_action — cap-hit drives one bounded retry then escalation:")
    cap = agent_daemon.CONTINUATION_RETRY_CAP
    sig = agent_daemon.CAP_HIT_SIGNAL
    check("cap-hit with budget left → continue",
          agent_daemon.buyer_continuation_action(sig, 0, cap) == "continue")
    check("cap-hit at the retry cap → escalate",
          agent_daemon.buyer_continuation_action(sig, cap, cap) == "escalate")
    check("cap-hit over the cap → escalate",
          agent_daemon.buyer_continuation_action(sig, cap + 5, cap) == "escalate")
    check("a clean rc=0 → none (no continuation, no escalation)",
          agent_daemon.buyer_continuation_action(0, 0, cap) == "none")
    check("a GENERIC failure (rc=1, not the cap signal) → none (not our loop)",
          agent_daemon.buyer_continuation_action(1, 0, cap) == "none")


def test_buyer_pass_caphit_triggers_one_continuation():
    print("Fix C: a buyer pass exiting with the cap-hit signal triggers exactly ONE continuation:")
    calls = []
    saved_run = agent_daemon.run_pass

    # run_pass returns the cap-hit signal on the FIRST buyer call, then 0 on the continuation.
    def fake_run_pass(mode, channel, env, dry_run, extra_env=None):
        calls.append((mode, (extra_env or {}).get("BAZAAR_BUYER_PEEK_THREAD")))
        return agent_daemon.CAP_HIT_SIGNAL if len(calls) == 1 else 0
    agent_daemon.run_pass = fake_run_pass
    escalations = []
    saved_esc = agent_daemon.escalate_cap_hit
    agent_daemon.escalate_cap_hit = (
        lambda channel, env, resource, dry_run, **kw: escalations.append(resource))
    try:
        agent_daemon.run_buyer_with_continuation(
            "carousell", {"adapter": "telegram"}, {}, False,
            extra_env={"BAZAAR_BUYER_PEEK_TEXT": "hi"}, peek_thread="carousell:1")
        buyer_calls = [c for c in calls if c[0] == "buyer"]
        check("exactly two buyer passes (initial + ONE continuation)", len(buyer_calls) == 2)
        check("the continuation carried the peek-thread hint",
              buyer_calls[1][1] == "carousell:1")
        check("no escalation (the continuation succeeded)", escalations == [])
    finally:
        agent_daemon.run_pass = saved_run
        agent_daemon.escalate_cap_hit = saved_esc


def test_buyer_pass_caphit_retry_guard_stops_hot_loop():
    print("Fix C: the retry guard stops a hot loop — a perpetually-capping market escalates ONCE:")
    calls = []
    saved_run = agent_daemon.run_pass

    def always_cap(mode, channel, env, dry_run, extra_env=None):
        calls.append(mode)
        return agent_daemon.CAP_HIT_SIGNAL  # never recovers
    agent_daemon.run_pass = always_cap
    escalations = []
    saved_esc = agent_daemon.escalate_cap_hit
    agent_daemon.escalate_cap_hit = (
        lambda channel, env, resource, dry_run, **kw: escalations.append(resource))
    try:
        agent_daemon.run_buyer_with_continuation(
            "fb", {"adapter": "telegram"}, {}, False, extra_env={}, peek_thread=None)
        buyer_calls = [c for c in calls if c == "buyer"]
        # initial + exactly CONTINUATION_RETRY_CAP continuations, then it stops (no infinite loop).
        check("bounded number of buyer passes (no hot loop)",
              len(buyer_calls) == 1 + agent_daemon.CONTINUATION_RETRY_CAP)
        check("escalated exactly once after the budget", escalations == ["fb"])
    finally:
        agent_daemon.run_pass = saved_run
        agent_daemon.escalate_cap_hit = saved_esc


def test_escalate_cap_hit_enqueues_channel_notify():
    print("Fix C: escalate_cap_hit via_outbox=True enqueues a channel notify (the concurrent path):")
    import os as _os
    import json as _json
    import tempfile as _tf
    tg = {"adapter": "telegram", "detail": {}}
    with _tf.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            agent_daemon.escalate_cap_hit(tg, {}, "fb", False, via_outbox=True)
            outbox = Path(d) / "channel_outbox.jsonl"
            check("a notify was enqueued", outbox.exists())
            if outbox.exists():
                recs = [_json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
                check("text names the market + turn cap",
                      any("fb" in r.get("text", "") and "turn cap" in r.get("text", "") for r in recs))
                check("kind notify", any(r.get("kind") == "notify" for r in recs))
            # dry-run must not enqueue (no side effects).
            outbox.unlink(missing_ok=True)
            agent_daemon.escalate_cap_hit(tg, {}, "carousell", True, via_outbox=True)
            check("dry-run enqueues nothing", not outbox.exists())
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)


def test_escalate_cap_hit_direct_send_in_single_flight():
    print("Bug D1: in single-flight mode escalate_cap_hit(via_outbox=False) DIRECT-sends via"
          " telegram.py (the single-flight loop has no outbox drainer, so an enqueue would strand):")
    import os as _os
    import tempfile as _tf
    tg = {"adapter": "telegram", "detail": {}}
    sent = []
    enqueued = []
    saved_run = agent_daemon.subprocess.run

    def fake_run(cmd, *a, **k):
        # Distinguish a direct telegram send from an outbox enqueue.
        if any("telegram.py" in str(p) for p in cmd) and "send" in cmd:
            sent.append(cmd)
        if any("channel_outbox.py" in str(p) for p in cmd):
            enqueued.append(cmd)
        return FakeProc(0)
    agent_daemon.subprocess.run = fake_run
    with _tf.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            agent_daemon.escalate_cap_hit(tg, {}, "fb", False, via_outbox=False)
            check("a direct telegram.py send fired (not just an enqueue)", len(sent) == 1)
            check("the direct send is kind=notify (matches the existing notify path)",
                  sent and "--kind" in sent[0] and sent[0][sent[0].index("--kind") + 1] == "notify")
            check("the direct send names the capped market + turn cap",
                  sent and any("fb" in str(p) for p in sent[0])
                  and any("turn cap" in str(p) for p in sent[0]))
            check("it did NOT enqueue to the undrained outbox", enqueued == [])
            # dry-run must neither send nor enqueue.
            sent.clear(); enqueued.clear()
            agent_daemon.escalate_cap_hit(tg, {}, "carousell", True, via_outbox=False)
            check("dry-run direct send sends nothing", sent == [] and enqueued == [])
            # adapter gating: a non-telegram adapter must not attempt a direct send (no wiring).
            sent.clear()
            agent_daemon.escalate_cap_hit({"adapter": "console", "detail": {}}, {}, "ebay",
                                          False, via_outbox=False)
            check("non-telegram adapter → no direct send (gated like the notify path)", sent == [])
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)
            agent_daemon.subprocess.run = saved_run


def test_peek_thread_from_pure():
    print("C-followup: peek_thread_from derives the single-thread hint from the buyer_peek result"
          " (NO second memo advance) — exactly one tracked thread across markets → that thread:")
    # Exactly one fresh tracked thread across all markets → return it.
    bp = {"markets": {"fb": {"sell_threads": ["fb:9988"]}, "carousell": {"sell_threads": []}}}
    check("exactly one across markets → that thread", agent_daemon.peek_thread_from(bp) == "fb:9988")
    # Zero fresh tracked threads (a brand-new enquiry flags the market but has no thread) → None.
    check("zero → None", agent_daemon.peek_thread_from(
        {"markets": {"fb": {"sell_threads": []}}}) is None)
    # More than one (ambiguous) → None (under-hint; mis-routing is the worst outcome).
    check(">1 → None (ambiguous)", agent_daemon.peek_thread_from(
        {"markets": {"fb": {"sell_threads": ["fb:1"]}, "carousell": {"sell_threads": ["carousell:2"]}}})
        is None)
    # Fail-open on a malformed/old-shape peek (no markets / no sell_threads keys) → None.
    check("no markets section → None", agent_daemon.peek_thread_from({}) is None)
    check("old-shape markets without sell_threads → None",
          agent_daemon.peek_thread_from({"markets": {"fb": {"new": True}}}) is None)


def test_peek_cmd_dispatch():
    print("_peek_cmd: builds the right non-consuming command per adapter (console → None):")
    tg = agent_daemon._peek_cmd({"adapter": "telegram", "detail": {}}, 25)
    check("telegram peek with --timeout", tg[-3:] == ["peek", "--timeout", "25"] and "telegram.py" in tg[1])
    im = agent_daemon._peek_cmd({"adapter": "imessage", "detail": {"handle": "+650000"}}, 25)
    check("imessage peek with --handle", "imessage.py" in im[1] and im[-2:] == ["--handle", "+650000"])
    wa = agent_daemon._peek_cmd({"adapter": "whatsapp", "detail": {}}, 25)
    check("whatsapp peek", "whatsapp.py" in wa[1] and wa[-1] == "peek")
    check("console has no daemon → None", agent_daemon._peek_cmd({"adapter": "console", "detail": {}}, 25) is None)


# --- Fix D: daemon reliability (duplicate-instance quiet exit, relaunch_self, stall guard) ---

def test_duplicate_instance_exits_quietly():
    print("Fix D: a duplicate instance logs INFO + returns 0 (not ERROR + rc 3 → no respawn churn):")
    import logging as _logging
    saved_acquire = agent_daemon.instance_lock.acquire
    # The lock is held by a LIVE duplicate → acquire reports not-acquired with truthful holder info.
    agent_daemon.instance_lock.acquire = lambda lock, hb: {
        "acquired": False, "holder_pid": 4242, "holder_alive": True, "reclaimed": False, "fd": None}
    records = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    root = _logging.getLogger()
    root.addHandler(handler)
    saved_level = root.level
    root.setLevel(_logging.INFO)
    try:
        rc = agent_daemon.main(["agent_daemon.py", "--once", "--dry-run"])
        check("returns 0 (clean exit → KeepAlive SuccessfulExit:false won't restart)", rc == 0)
        dup = [r for r in records if "already running" in r.getMessage()]
        check("logged the duplicate-instance line", len(dup) >= 1)
        check("logged at INFO, not ERROR", dup and all(r.levelno == _logging.INFO for r in dup))
        check("line names the holder pid", dup and "4242" in dup[0].getMessage())
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
        agent_daemon.instance_lock.acquire = saved_acquire


def test_clean_exit_clears_instance_lock_holder():
    print("Bug D3: when WE acquired the lock, a clean main() exit clears the lock holder so the")
    print("        watchdog can't be fooled by a recycled PID into a silent stay-down:")
    import os as _os
    import tempfile as _tf

    saved_acquire = agent_daemon.instance_lock.acquire
    saved_lock_path = agent_daemon.INSTANCE_LOCK
    saved_hb_path = agent_daemon.HEARTBEAT
    saved_load_channel = agent_daemon.load_channel
    saved_load_config = agent_daemon.load_config
    with _tf.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        hb = Path(d) / ".daemon.heartbeat"
        agent_daemon.INSTANCE_LOCK = lock
        agent_daemon.HEARTBEAT = hb

        def fake_acquire(lock_path, hb_path):
            # Mimic a real acquire: record OUR pid in the lock body, hand back a held fd.
            fd = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o600)
            _os.ftruncate(fd, 0)
            _os.write(fd, str(_os.getpid()).encode())
            return {"acquired": True, "holder_pid": _os.getpid(), "holder_alive": True,
                    "reclaimed": False, "fd": fd}
        agent_daemon.instance_lock.acquire = fake_acquire
        # console adapter → main returns rc=3 BEFORE the loop (no token IO/network); the clean-exit
        # clear must STILL run via the finally. Stub config/channel so this stays fast + isolated.
        agent_daemon.load_channel = lambda: {"adapter": "console", "detail": {}}
        agent_daemon.load_config = lambda: {"peek_timeout": 5, "max_concurrent_workers": 1}
        try:
            rc = agent_daemon.main(["agent_daemon.py", "--once", "--dry-run"])
            check("exits with the console rc=3 (early return, before the loop)", rc == 3)
            check("lock holder cleared on exit (no live holder for the watchdog)",
                  agent_daemon.instance_lock.read_holder_pid(lock) is None)
        finally:
            agent_daemon.instance_lock.acquire = saved_acquire
            agent_daemon.INSTANCE_LOCK = saved_lock_path
            agent_daemon.HEARTBEAT = saved_hb_path
            agent_daemon.load_channel = saved_load_channel
            agent_daemon.load_config = saved_load_config


def test_duplicate_exit_does_not_clear_live_holders_lock():
    print("Bug D3: a duplicate instance (we did NOT acquire) must NOT clear the live holder's lock —")
    print("        clearing it would let the watchdog wrongly think the REAL daemon is gone:")
    import tempfile as _tf

    saved_acquire = agent_daemon.instance_lock.acquire
    saved_lock_path = agent_daemon.INSTANCE_LOCK
    with _tf.TemporaryDirectory() as d:
        lock = Path(d) / ".daemon.instancelock"
        lock.write_text("4242")  # the live holder's pid, recorded in the lock body
        agent_daemon.INSTANCE_LOCK = lock
        agent_daemon.instance_lock.acquire = lambda lp, hp: {
            "acquired": False, "holder_pid": 4242, "holder_alive": True,
            "reclaimed": False, "fd": None}
        try:
            rc = agent_daemon.main(["agent_daemon.py", "--once", "--dry-run"])
            check("returns 0 (clean duplicate exit)", rc == 0)
            check("live holder's lock body is UNTOUCHED",
                  agent_daemon.instance_lock.read_holder_pid(lock) == 4242)
        finally:
            agent_daemon.instance_lock.acquire = saved_acquire
            agent_daemon.INSTANCE_LOCK = saved_lock_path


def test_relaunch_self_shells_kickstart():
    print("Fix D: relaunch_self best-effort shells `launchctl kickstart -k` (asserts argv):")
    calls = []
    saved_run = agent_daemon.subprocess.run

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return FakeProc(0)
    agent_daemon.subprocess.run = fake_run
    try:
        agent_daemon.relaunch_self()
        check("exactly one shell-out", len(calls) == 1)
        argv = calls[0]
        check("invokes launchctl kickstart -k", argv[:3] == ["launchctl", "kickstart", "-k"])
        check("targets the agent label in a gui/<uid> domain",
              argv[-1].startswith("gui/") and argv[-1].endswith(agent_daemon.AGENT_LABEL))
    finally:
        agent_daemon.subprocess.run = saved_run


def test_relaunch_self_is_best_effort():
    print("Fix D: relaunch_self never raises — a non-zero kickstart is logged + swallowed:")
    saved_run = agent_daemon.subprocess.run

    def boom(*a, **k):
        raise agent_daemon.subprocess.SubprocessError("launchctl missing")
    agent_daemon.subprocess.run = boom
    try:
        raised = False
        try:
            agent_daemon.relaunch_self()
        except Exception:  # noqa: BLE001 — the whole point is that it must NOT raise
            raised = True
        check("does not raise on a subprocess error", raised is False)
    finally:
        agent_daemon.subprocess.run = saved_run

    # A non-zero return code is also fail-open (logged, not raised).
    agent_daemon.subprocess.run = lambda *a, **k: FakeProc(1, "", "no such service")
    try:
        raised = False
        try:
            agent_daemon.relaunch_self()
        except Exception:  # noqa: BLE001
            raised = True
        check("does not raise on a non-zero kickstart", raised is False)
    finally:
        agent_daemon.subprocess.run = saved_run


def test_stall_guard_warns_over_budget():
    print("Fix D: the per-iteration stall guard WARNs when an iteration exceeds LOOP_ITER_BUDGET:")
    check("budget is a positive number", agent_daemon.LOOP_ITER_BUDGET > 0)
    over = agent_daemon.iteration_stall_warning(agent_daemon.LOOP_ITER_BUDGET + 1,
                                                agent_daemon.LOOP_ITER_BUDGET)
    check("over budget → a WARN message string", isinstance(over, str) and over != "")
    under = agent_daemon.iteration_stall_warning(agent_daemon.LOOP_ITER_BUDGET - 1,
                                                 agent_daemon.LOOP_ITER_BUDGET)
    check("under budget → no message (None)", under is None)
    check("exactly at budget → no message", agent_daemon.iteration_stall_warning(
        agent_daemon.LOOP_ITER_BUDGET, agent_daemon.LOOP_ITER_BUDGET) is None)


# --- Nightly self-eval: the LLM judge can run nightly (config.eval_judge_nightly) ---

def test_load_config_eval_judge_nightly():
    print("load_config: eval_judge_nightly is a bool; defaults True ('on for everyone') when absent:")
    cfg = agent_daemon.load_config()
    check("present and bool", isinstance(cfg.get("eval_judge_nightly"), bool))
    saved = agent_daemon.CONFIG_PATH
    agent_daemon.CONFIG_PATH = Path("/nonexistent/bazaar/config.json")  # exists() False → code default
    try:
        check("absent config → default True", agent_daemon.load_config()["eval_judge_nightly"] is True)
    finally:
        agent_daemon.CONFIG_PATH = saved


def test_run_eval_deterministic_argv():
    print("run_eval(use_judge=False): runs eval_run with --no-llm at the deterministic timeout:")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append((cmd, k.get("timeout")))
        return FakeProc(0, "summary line")

    def body():
        agent_daemon.run_eval({}, False, use_judge=False)
        eval_calls = [c for c in calls if any("eval_run.py" in str(p) for p in c[0])]
        check("invoked eval_run.py once", len(eval_calls) == 1)
        cmd, timeout = eval_calls[0]
        check("argv ends with run --no-llm", cmd[-2:] == ["run", "--no-llm"])
        check("uses the deterministic timeout", timeout == agent_daemon.EVAL_TIMEOUT_SEC)
        check("still stamps eval_state mark",
              any("eval_state.py" in str(p) for c in calls for p in c[0]))
    with_run(fake_run, body)


def test_run_eval_judge_argv():
    print("run_eval(use_judge=True): runs eval_run WITHOUT --no-llm at the larger judge timeout:")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append((cmd, k.get("timeout")))
        return FakeProc(0, "summary line")

    def body():
        agent_daemon.run_eval({}, False, use_judge=True)
        eval_calls = [c for c in calls if any("eval_run.py" in str(p) for p in c[0])]
        check("invoked eval_run.py once", len(eval_calls) == 1)
        cmd, timeout = eval_calls[0]
        check("argv has no --no-llm (judge runs)", "--no-llm" not in cmd)
        check("argv ends with run", cmd[-1] == "run")
        check("judge timeout exceeds the deterministic one (judge can take minutes)",
              timeout == agent_daemon.EVAL_JUDGE_TIMEOUT_SEC and timeout > agent_daemon.EVAL_TIMEOUT_SEC)
    with_run(fake_run, body)


def test_run_eval_dry_run_spawns_nothing():
    print("run_eval(dry_run=True): logs intent, spawns no subprocess (both branches):")
    def must_not_run(*a, **k):
        raise AssertionError("dry-run must not spawn a subprocess")

    def body():
        agent_daemon.run_eval({}, True, use_judge=False)
        agent_daemon.run_eval({}, True, use_judge=True)
        check("no subprocess spawned in dry-run", True)
    with_run(must_not_run, body)


def test_followup_due_parses_and_failopen():
    print("_followup_due: parses counts on rc=0; fails open to no-work otherwise:")
    def ok():
        out = agent_daemon._followup_due({})
        check("nudges/drops parsed", out["nudges"] == 2 and out["drops"] == 1)
        check("due_nudges carried", out["due_nudges"] == [{"side": "sell"}])
    with_run(lambda *a, **k: FakeProc(0, '{"enabled": true, "counts": {"nudges": 2, "drops": 1},'
                                         ' "due_nudges": [{"side": "sell"}], "due_drops": []}'), ok)

    def fail():
        check("rc!=0 -> no work", agent_daemon._followup_due({}) == {
            "nudges": 0, "drops": 0, "enabled": False, "due_nudges": []})
    with_run(lambda *a, **k: FakeProc(1, "", "boom"), fail)

    def boom(*a, **k):
        raise agent_daemon.subprocess.SubprocessError("timed out")
    def exc():
        check("exception -> no work (no raise)", agent_daemon._followup_due({})["nudges"] == 0)
    with_run(boom, exc)


def test_followup_drops_dry_run_spawns_nothing():
    print("run_followup_drops(dry_run=True): no subprocess:")
    def must_not_run(*a, **k):
        raise AssertionError("dry-run must not spawn a subprocess")
    with_run(must_not_run, lambda: (agent_daemon.run_followup_drops({}, True),
                                    check("no subprocess in dry-run", True)))


def test_listing_health_due_parses_and_failopen():
    print("_listing_health_due: returns due_item on rc=0; None otherwise:")
    def ok():
        check("returns the due item id", agent_daemon._listing_health_due({}) == "widget")
    with_run(lambda *a, **k: FakeProc(0, '{"due_item": "widget", "stale_count": 3}'), ok)

    def none():
        check("no due item -> None", agent_daemon._listing_health_due({}) is None)
    with_run(lambda *a, **k: FakeProc(0, '{"due_item": null}'), none)

    def fail():
        check("rc!=0 -> None", agent_daemon._listing_health_due({}) is None)
    with_run(lambda *a, **k: FakeProc(2, "", "nope"), fail)


def test_listing_health_start_dry_run_spawns_nothing():
    print("run_listing_health_start(dry_run=True): no subprocess:")
    def must_not_run(*a, **k):
        raise AssertionError("dry-run must not spawn a subprocess")
    with_run(must_not_run, lambda: (agent_daemon.run_listing_health_start({}, "widget", True),
                                    check("no subprocess in dry-run", True)))


def test_followup_poll_sec_in_config():
    print("load_config exposes followup_poll_sec (default 3600):")
    cfg = agent_daemon.load_config()
    check("followup_poll_sec present", "followup_poll_sec" in cfg)
    check("is a positive int", isinstance(cfg["followup_poll_sec"], int) and cfg["followup_poll_sec"] > 0)


def test_plan_outbox_sweep_pure():
    print("plan_outbox_sweep routes stranded pending intents to re-drive / escalate / leave:")
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
    redrive_age = (now - timedelta(seconds=300)).isoformat()   # past redrive_after, within escalate_after
    very_old = (now - timedelta(seconds=2000)).isoformat()     # past escalate_after (1800)
    fresh = (now - timedelta(seconds=10)).isoformat()
    pending = [
        {"id": "a", "thread_id": "fb:1", "market": "fb", "ts": redrive_age, "attempts": 0},     # re-drive
        {"id": "b", "thread_id": "fb:2", "market": "fb", "ts": fresh, "attempts": 0},            # too fresh
        {"id": "c", "thread_id": "cl:3", "market": "carousell", "ts": very_old, "attempts": 3},  # escalate
        {"id": "d", "thread_id": "cl:4", "market": "carousell", "ts": very_old, "attempts": 9,
         "escalated": True},                                                                     # already done
        {"id": "e", "thread_id": "eb:5", "market": "ebay", "ts": very_old, "attempts": 0},       # leased -> skip
        {"id": "f", "thread_id": "fb:6", "market": "fb", "ts": redrive_age, "attempts": 5},      # tries hit,
    ]                                                                                            # too young: redrive
    plan = agent_daemon.plan_outbox_sweep(pending, now, leased_markets={"ebay"},
                                          redrive_after_sec=90, escalate_attempts=3,
                                          escalate_after_sec=1800)
    check("old low-attempt intent queued for re-drive", "a" in plan["redrive"].get("fb", []))
    check("a fresh intent (< redrive_after) is left alone", "b" not in plan["redrive"].get("fb", []))
    check("attempts AND age past the gates escalates exactly that intent",
          [e["id"] for e in plan["escalate"]] == ["c"])
    check("an already-escalated record is left alone (no re-drive, no re-alarm)",
          "carousell" not in plan["redrive"] and all(e["id"] != "d" for e in plan["escalate"]))
    check("a market with a live lease is skipped entirely", "ebay" not in plan["redrive"])
    check("attempts hit but too young to give up → keep re-driving, don't escalate",
          "f" in plan["redrive"].get("fb", []) and all(e["id"] != "f" for e in plan["escalate"]))


def test_sweep_outbox_redrives_and_bumps_attempts():
    print("sweep_outbox flags a stranded market for re-drive and bumps attempts (bounded loop):")
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone
    import thread_outbox as to
    with tempfile.TemporaryDirectory() as d:
        os.environ["BAZAAR_DATA_DIR"] = d
        try:
            old = datetime.now(timezone.utc) - timedelta(seconds=300)
            to.enqueue("fb:vida", "fb", "no defects, all brand new", "1:50 PM|defects?", old, side="sell")
            markets = agent_daemon.sweep_outbox({}, False, busy_markets=set())
            check("the stranded fb send is flagged for re-drive", "fb" in markets)
            recs = to.peek(statuses=to.OPEN_STATUSES)["pending"]
            check("intent still alive (not dropped)", len(recs) == 1)
            check("attempts bumped to 1 (bounds the re-drive loop)", recs[0]["attempts"] == 1)
        finally:
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_sweep_outbox_dry_run_is_noop():
    print("sweep_outbox is a no-op under --dry-run:")
    check("dry-run returns no markets", agent_daemon.sweep_outbox({}, True, busy_markets=set()) == set())


def test_outbox_sweep_poll_sec_in_config():
    print("outbox_sweep_poll_sec has a sane positive default in load_config:")
    cfg = agent_daemon.load_config()
    check("outbox_sweep_poll_sec present", "outbox_sweep_poll_sec" in cfg)
    check("default is a positive int",
          isinstance(cfg["outbox_sweep_poll_sec"], int) and cfg["outbox_sweep_poll_sec"] > 0)


def test_escalation_text_is_honest():
    print("the outbox escalation states uncertainty, never the unverifiable 'send never fired':")
    import os as _os
    import json as _json
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            agent_daemon._enqueue_outbox_escalation(
                {"thread_id": "fb:vida-onepiece", "id": "x", "text": "hi", "attempts": 3}, {})
            outbox = Path(d) / "channel_outbox.jsonl"
            check("a notify was enqueued", outbox.exists())
            recs = ([_json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
                    if outbox.exists() else [])
            txt = " ".join(r.get("text", "") for r in recs).lower()
            check("names the thread", "fb:vida-onepiece" in txt)
            check("does NOT assert the send never fired",
                  "never fired" not in txt and "not a crash" not in txt)
            check("frames it as unconfirmed / still verifying",
                  "verifying" in txt and "may already have" in txt)
            check("kind notify", any(r.get("kind") == "notify" for r in recs))
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)


def test_sweep_skips_intent_already_committed():
    print("a pending intent whose reply the ledger already shows delivered is acked, never escalated"
          " (the vida false-alarm regression):")
    import os as _os
    import json as _json
    import tempfile as _tf
    from datetime import datetime, timedelta, timezone
    import thread_outbox as to
    with _tf.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            # a long-stranded, max-attempts intent that WOULD otherwise escalate ...
            old = datetime.now(timezone.utc) - timedelta(seconds=4000)
            iid = to.enqueue("fb:vida-onepiece", "fb", "Still sorting out the details...",
                             "4:29 PM|hi", old, side="sell")["id"]
            for _ in range(3):
                to.fail(iid)  # attempts -> 3 (at the escalate ceiling)
            # ... but the thread ledger already carries the committed outbound out|<iid>.
            threads = Path(d) / "threads"
            threads.mkdir(parents=True, exist_ok=True)
            (threads / "fb:vida-onepiece.json").write_text(_json.dumps({
                "thread_id": "fb:vida-onepiece",
                "cursor": {"last_handled_msg_id": "4:29 PM|hi"},
                "transcript": [
                    {"msg_id": "4:29 PM|hi", "dir": "in", "text": "hi"},
                    {"msg_id": f"out|{iid}", "dir": "out", "text": "Still sorting out the details..."},
                ],
            }))
            markets = agent_daemon.sweep_outbox({}, False, busy_markets=set())
            check("no market re-driven (intent recognized as already delivered)", markets == set())
            check("the stale intent was acked (dropped, not left pending)",
                  to.peek(statuses=to.OPEN_STATUSES)["count"] == 0)
            outbox = Path(d) / "channel_outbox.jsonl"
            check("NO false-alarm notify was enqueued",
                  not outbox.exists() or outbox.read_text().strip() == "")
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)


def test_sweep_does_not_escalate_before_age_floor():
    print("even at max attempts, a young stranded intent is re-driven (recovery's chance), not escalated:")
    import os as _os
    import tempfile as _tf
    from datetime import datetime, timedelta, timezone
    import thread_outbox as to
    with _tf.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
            iid = to.enqueue("fb:young", "fb", "holding line", "1:00 PM|hi", now, side="sell")["id"]
            for _ in range(4):
                to.fail(iid)  # attempts -> 4 (>= ceiling)
            # sweep 400s later: past redrive_after(90), well under escalate_after(1800)
            markets = agent_daemon.sweep_outbox({}, False, busy_markets=set(),
                                                now=now + timedelta(seconds=400))
            check("re-driven (not escalated) because too young to give up", markets == {"fb"})
            outbox = Path(d) / "channel_outbox.jsonl"
            check("no escalation notice yet",
                  not outbox.exists() or outbox.read_text().strip() == "")
            recs = to.peek(statuses=to.OPEN_STATUSES)["pending"]
            check("intent still pending, not marked escalated", recs and not recs[0].get("escalated"))
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)


def test_sweep_escalates_only_past_attempts_and_age():
    print("once attempts AND the age floor are both met, escalate exactly once (honestly):")
    import os as _os
    import json as _json
    import tempfile as _tf
    from datetime import datetime, timedelta, timezone
    import thread_outbox as to
    with _tf.TemporaryDirectory() as d:
        _os.environ["BAZAAR_DATA_DIR"] = d
        try:
            now = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
            iid = to.enqueue("fb:stuck", "fb", "holding line", "1:00 PM|hi", now, side="sell")["id"]
            for _ in range(3):
                to.fail(iid)  # attempts -> 3
            late = now + timedelta(seconds=2000)  # past escalate_after(1800)
            markets = agent_daemon.sweep_outbox({}, False, busy_markets=set(), now=late)
            check("not re-driven once escalated", "fb" not in markets)
            outbox = Path(d) / "channel_outbox.jsonl"
            check("an escalation notice was enqueued", outbox.exists())
            recs = ([_json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
                    if outbox.exists() else [])
            check("the notice uses the honest wording", any("verifying" in r.get("text", "") for r in recs))
            pend = to.peek(statuses=to.OPEN_STATUSES)["pending"]
            check("intent marked escalated (durable exactly-once)",
                  pend and pend[0].get("escalated") is True)
            # a SECOND sweep must NOT escalate again (the escalated marker holds).
            before = len(recs)
            agent_daemon.sweep_outbox({}, False, busy_markets=set(), now=late + timedelta(seconds=200))
            recs2 = ([_json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
                     if outbox.exists() else [])
            check("no second escalation on the next sweep", len(recs2) == before)
        finally:
            _os.environ.pop("BAZAAR_DATA_DIR", None)


if __name__ == "__main__":
    print("agent_daemon tests\n")
    test_plan_outbox_sweep_pure()
    test_sweep_outbox_redrives_and_bumps_attempts()
    test_sweep_outbox_dry_run_is_noop()
    test_outbox_sweep_poll_sec_in_config()
    test_escalation_text_is_honest()
    test_sweep_skips_intent_already_committed()
    test_sweep_does_not_escalate_before_age_floor()
    test_sweep_escalates_only_past_attempts_and_age()
    test_buyer_peek_parses_success()
    test_buyer_peek_failopen_on_rc()
    test_buyer_peek_failopen_on_bad_json()
    test_buyer_peek_failopen_on_exception()
    test_buy_peek_success_and_failopen()
    test_channel_peek_console_skips_subprocess()
    test_channel_peek_parses_success()
    test_buyer_force_due_count_net()
    test_buyer_force_due_time_floor()
    test_buyer_recheck_failopen_conservative()
    test_buyer_action_decision()
    test_load_config_sweep_floor()
    test_wake_mode_reflects_fda()
    test_buyer_continuation_action_pure()
    test_buyer_pass_caphit_triggers_one_continuation()
    test_buyer_pass_caphit_retry_guard_stops_hot_loop()
    test_escalate_cap_hit_enqueues_channel_notify()
    test_escalate_cap_hit_direct_send_in_single_flight()
    test_peek_thread_from_pure()
    test_peek_cmd_dispatch()
    test_duplicate_instance_exits_quietly()
    test_clean_exit_clears_instance_lock_holder()
    test_duplicate_exit_does_not_clear_live_holders_lock()
    test_relaunch_self_shells_kickstart()
    test_relaunch_self_is_best_effort()
    test_stall_guard_warns_over_budget()
    test_load_config_eval_judge_nightly()
    test_run_eval_deterministic_argv()
    test_run_eval_judge_argv()
    test_run_eval_dry_run_spawns_nothing()
    test_followup_due_parses_and_failopen()
    test_followup_drops_dry_run_spawns_nothing()
    test_listing_health_due_parses_and_failopen()
    test_listing_health_start_dry_run_spawns_nothing()
    test_followup_poll_sec_in_config()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
