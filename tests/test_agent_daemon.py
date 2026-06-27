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


def test_peek_cmd_dispatch():
    print("_peek_cmd: builds the right non-consuming command per adapter (console → None):")
    tg = agent_daemon._peek_cmd({"adapter": "telegram", "detail": {}}, 25)
    check("telegram peek with --timeout", tg[-3:] == ["peek", "--timeout", "25"] and "telegram.py" in tg[1])
    im = agent_daemon._peek_cmd({"adapter": "imessage", "detail": {"handle": "+650000"}}, 25)
    check("imessage peek with --handle", "imessage.py" in im[1] and im[-2:] == ["--handle", "+650000"])
    wa = agent_daemon._peek_cmd({"adapter": "whatsapp", "detail": {}}, 25)
    check("whatsapp peek", "whatsapp.py" in wa[1] and wa[-1] == "peek")
    check("console has no daemon → None", agent_daemon._peek_cmd({"adapter": "console", "detail": {}}, 25) is None)


if __name__ == "__main__":
    print("agent_daemon tests\n")
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
    test_peek_cmd_dispatch()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
