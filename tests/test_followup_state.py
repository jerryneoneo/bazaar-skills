#!/usr/bin/env python3
"""Tests for followup_state.py — stale-chat follow-up detection + scheduling.

    python3 tests/test_followup_state.py

Focus: the deterministic DETECT + SCHEDULE core. The nudge count is DERIVED from the transcript
tail (never trusted from the ledger), the schedule is a pure lookup, and `scan_due` partitions
threads into due nudges vs due drops at a fixed `now`. Composition + sending is the LLM pass's job
and is not exercised here. State is isolated per test via tmp dirs / BAZAAR_DATA_DIR.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import followup_state as fs  # noqa: E402

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


# ---- fixture helpers -------------------------------------------------------

def _msg(direction, text, ts):
    return {"msg_id": f"{direction}:{text}", "dir": direction, "text": text, "ts": ts}


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def _thread(status="active", rows=None, marketplace="fb", thread_id="fb:x",
            buyer_handle="alice", item_id="widget"):
    return {"thread_id": thread_id, "marketplace": marketplace, "status": status,
            "buyer_handle": buyer_handle, "item_id": item_id, "transcript": rows or []}


def _write_json(tmp, rel, payload):
    p = Path(tmp) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload))
    return p


def _write_jsonl(tmp, rel, rows):
    p = Path(tmp) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


# ---- pure: trailing_outbound -----------------------------------------------

def test_trailing_outbound():
    print("trailing_outbound: counts the consecutive outbound tail only:")
    check("empty transcript -> []", fs.trailing_outbound(_thread(rows=[])) == [])
    last_in = _thread(rows=[_msg("out", "hi", _ago(2)), _msg("in", "yo", _ago(1))])
    check("last row inbound -> []", fs.trailing_outbound(last_in) == [])
    one = _thread(rows=[_msg("in", "hi", _ago(2)), _msg("out", "reply", _ago(1))])
    check("one trailing out -> 1", len(fs.trailing_outbound(one)) == 1)
    two = _thread(rows=[_msg("in", "hi", _ago(3)), _msg("out", "reply", _ago(2)),
                        _msg("out", "nudge", _ago(1))])
    check("out,out tail -> 2", len(fs.trailing_outbound(two)) == 2)
    interrupted = _thread(rows=[_msg("out", "a", _ago(3)), _msg("in", "b", _ago(2)),
                                _msg("out", "c", _ago(1))])
    check("out,in,out -> 1 (only the tail run)", len(fs.trailing_outbound(interrupted)) == 1)


# ---- pure: awaiting_counterpart --------------------------------------------

def test_awaiting_counterpart():
    print("awaiting_counterpart: tail outbound + non-terminal status:")
    active = _thread(status="active", rows=[_msg("in", "hi", _ago(2)), _msg("out", "r", _ago(1))])
    check("trailing-out + active -> True", fs.awaiting_counterpart(active, fs.SELL_TERMINAL) is True)
    for st in sorted(fs.SELL_TERMINAL):
        t = _thread(status=st, rows=[_msg("in", "hi", _ago(2)), _msg("out", "r", _ago(1))])
        check(f"trailing-out + {st} -> False", fs.awaiting_counterpart(t, fs.SELL_TERMINAL) is False)
    replied = _thread(status="active", rows=[_msg("out", "r", _ago(2)), _msg("in", "yo", _ago(1))])
    check("trailing-in + active -> False", fs.awaiting_counterpart(replied, fs.SELL_TERMINAL) is False)


# ---- pure: schedule_for (the heart) ----------------------------------------

def test_schedule_for():
    print("schedule_for: run length -> action + gap (intervals=[1,3], drop=3, max=2):")
    iv, drop, mx = [1, 3], 3, 2
    check("run 0 -> None", fs.schedule_for(0, iv, drop, mx) is None)
    check("run 1 -> nudge @1d", fs.schedule_for(1, iv, drop, mx) == ("nudge", 1.0))
    check("run 2 -> nudge @3d", fs.schedule_for(2, iv, drop, mx) == ("nudge", 3.0))
    check("run 3 -> drop @3d", fs.schedule_for(3, iv, drop, mx) == ("drop", 3.0))
    check("run 4 -> None", fs.schedule_for(4, iv, drop, mx) is None)
    check("max_nudges 0 disables", fs.schedule_for(1, iv, drop, 0) is None)
    # fewer intervals than max_nudges -> clamp, never IndexError
    check("clamp: run 2 with 1 interval", fs.schedule_for(2, [2], drop, mx) == ("nudge", 2.0))


# ---- pure: due_decision (with --now) ---------------------------------------

def _decide(thread, side="sell", terminal=None):
    return fs.due_decision(thread, side, terminal or fs.SELL_TERMINAL, [1, 3], 3, 2, NOW)


def test_due_decision():
    print("due_decision: fires only once aged past the gap; fail-closed on bad ts:")
    young = _thread(rows=[_msg("in", "hi", _ago(2)), _msg("out", "r", _ago(0.5))])
    check("nudge#1 not yet (0.5d < 1d) -> None", _decide(young) is None)
    ripe1 = _thread(rows=[_msg("in", "hi", _ago(3)), _msg("out", "r", _ago(1.1))])
    d1 = _decide(ripe1)
    check("nudge#1 due (1.1d >= 1d)", d1 and d1["action"] == "nudge" and d1["nudges_sent"] == 0)
    ripe2 = _thread(rows=[_msg("in", "hi", _ago(6)), _msg("out", "r", _ago(4)),
                          _msg("out", "n1", _ago(3.1))])
    d2 = _decide(ripe2)
    check("nudge#2 due (3.1d >= 3d)", d2 and d2["action"] == "nudge" and d2["nudges_sent"] == 1)
    ripe3 = _thread(rows=[_msg("in", "hi", _ago(9)), _msg("out", "r", _ago(7)),
                          _msg("out", "n1", _ago(6)), _msg("out", "n2", _ago(3.1))])
    d3 = _decide(ripe3)
    check("drop due (3.1d >= 3d after 2 nudges)", d3 and d3["action"] == "drop" and d3["nudges_sent"] == 2)
    bad_ts = _thread(rows=[_msg("in", "hi", _ago(3)), {"dir": "out", "text": "r", "ts": "garbage"}])
    check("un-parseable anchor -> None (fail-closed)", _decide(bad_ts) is None)
    esc = _thread(status="escalated", rows=[_msg("in", "hi", _ago(3)), _msg("out", "r", _ago(2))])
    check("escalated (awaiting user) -> None", _decide(esc) is None)


# ---- pure: reconcile_ledger ------------------------------------------------

def test_reconcile_ledger():
    print("reconcile_ledger: keep still-awaiting, drop gone/answered, immutable:")
    ledger = {"fb:a": {"disposition": "active"}, "fb:b": {"disposition": "active"}}
    out = fs.reconcile_ledger(ledger, {"fb:a"})
    check("answered/gone thread dropped", "fb:b" not in out)
    check("still-awaiting kept", "fb:a" in out)
    check("input not mutated", "fb:b" in ledger)


# ---- config parsing --------------------------------------------------------

def test_config_parsing():
    print("config parsing: defaults, explicit, rejects bad:")
    check("enabled default true", fs._enabled_from_config({}) is True)
    check("enabled explicit false", fs._enabled_from_config({"followup_enabled": False}) is False)
    check("intervals default", fs._intervals_from_config({}) == (1.0, 3.0))
    check("intervals explicit", fs._intervals_from_config({"followup_nudge_intervals_days": [2, 5]}) == (2.0, 5.0))
    check("max default", fs._max_nudges_from_config({}) == 2)
    raised = False
    try:
        fs._intervals_from_config({"followup_nudge_intervals_days": "soon"})
    except ValueError:
        raised = True
    check("non-numeric intervals rejected", raised)


# ---- scan_due (filesystem integration) -------------------------------------

def test_scan_due_partitions():
    print("scan_due: partitions sell+buy threads into nudges/drops, honors enable + escalation:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"followup_enabled": True})
        # sell thread due for nudge #1
        _write_json(tmp, "threads/fb:nudge.json",
                    _thread(thread_id="fb:nudge", rows=[_msg("in", "hi", _ago(3)),
                                                        _msg("out", "r", _ago(1.5))]))
        # buy thread due for a drop (2 nudges already, aged out)
        _write_json(tmp, "buyer_threads/csll:drop.json",
                    {"thread_id": "csll:drop", "marketplace": "carousell", "status": "liaising",
                     "seller_handle": "bob", "want_id": "ipad",
                     "transcript": [_msg("in", "hi", _ago(12)), _msg("out", "r", _ago(9)),
                                    _msg("out", "n1", _ago(6)), _msg("out", "n2", _ago(3.5))]})
        # caught-up thread (they replied) -> ignored
        _write_json(tmp, "threads/fb:caught.json",
                    _thread(thread_id="fb:caught", rows=[_msg("out", "r", _ago(2)),
                                                         _msg("in", "yo", _ago(1))]))
        res = fs.scan_due(Path(tmp), NOW)
        check("enabled", res["enabled"] is True)
        check("one nudge due", res["counts"]["nudges"] == 1)
        check("nudge is the sell thread", res["due_nudges"][0]["thread_id"] == "fb:nudge")
        check("one drop due", res["counts"]["drops"] == 1)
        check("drop is the buy thread", res["due_drops"][0]["thread_id"] == "csll:drop")
        check("buy-side gap honored", res["due_drops"][0]["side"] == "buy")


def test_scan_due_disabled_and_escalation():
    print("scan_due: disabled -> empty; open escalation excluded:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"followup_enabled": False})
        _write_json(tmp, "threads/fb:nudge.json",
                    _thread(thread_id="fb:nudge", rows=[_msg("in", "hi", _ago(3)),
                                                        _msg("out", "r", _ago(1.5))]))
        res = fs.scan_due(Path(tmp), NOW)
        check("disabled -> not enabled", res["enabled"] is False)
        check("disabled -> no work", res["counts"]["nudges"] == 0 and res["counts"]["drops"] == 0)
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"followup_enabled": True})
        _write_json(tmp, "threads/fb:esc.json",
                    _thread(thread_id="fb:esc", rows=[_msg("in", "hi", _ago(3)),
                                                      _msg("out", "r", _ago(1.5))]))
        _write_jsonl(tmp, "escalations.jsonl", [{"thread_id": "fb:esc", "status": "open"}])
        res = fs.scan_due(Path(tmp), NOW)
        check("open-escalation thread excluded from nudges", res["counts"]["nudges"] == 0)


# ---- mark-nudge / mark-drop / reconcile (ledger IO) ------------------------

def test_mark_nudge_then_not_due():
    print("mark-nudge refreshes ledger; thread not due again until next interval:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"followup_enabled": True})
        # transcript already shows nudge #1 sent ~0.2d ago (post-send state)
        _write_json(tmp, "threads/fb:t.json",
                    _thread(thread_id="fb:t", rows=[_msg("in", "hi", _ago(3)),
                                                    _msg("out", "r", _ago(1.5)),
                                                    _msg("out", "n1", _ago(0.2))]))
        entry = fs.run_mark_nudge("fb:t", "sell", NOW, base=base)
        check("followup_count derived = 1", entry["followup_count"] == 1)
        check("disposition active", entry["disposition"] == "active")
        res = fs.scan_due(base, NOW)
        check("not due again (nudge#2 needs 3d, only 0.2d)", res["counts"]["nudges"] == 0)
        again = fs.run_mark_nudge("fb:t", "sell", NOW, base=base)
        check("idempotent re-mark stable count", again["followup_count"] == 1)


def test_mark_drop_disposition():
    print("mark-drop flips disposition; second call no-op; due list excludes it:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"followup_enabled": True})
        _write_json(tmp, "threads/fb:d.json",
                    _thread(thread_id="fb:d", rows=[_msg("in", "hi", _ago(9)), _msg("out", "r", _ago(7)),
                                                    _msg("out", "n1", _ago(6)), _msg("out", "n2", _ago(3.5))]))
        e1 = fs.run_mark_drop("fb:d", "sell", NOW, base=base)
        check("disposition not_interested", e1["disposition"] == "not_interested")
        e2 = fs.run_mark_drop("fb:d", "sell", NOW, base=base)
        check("second call still not_interested (idempotent)", e2["disposition"] == "not_interested")


def test_run_drops_notifies_once():
    print("run_drops marks not_interested + enqueues ONE notice, deduped on re-run:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"followup_enabled": True})
        _write_json(tmp, "threads/fb:d.json",
                    _thread(thread_id="fb:d", buyer_handle="carol", item_id="lamp",
                            rows=[_msg("in", "hi", _ago(9)), _msg("out", "r", _ago(7)),
                                  _msg("out", "n1", _ago(6)), _msg("out", "n2", _ago(3.5))]))
        r1 = fs.run_drops(NOW, base=base)
        check("one dropped", r1["dropped"] == 1)
        check("one notified", r1["notified"] == 1)
        outbox = (base / "channel_outbox.jsonl").read_text().strip().splitlines()
        check("one outbox record", len(outbox) == 1)
        rec = json.loads(outbox[0])
        check("notice names the buyer", "carol" in rec["text"])
        check("notice has no em-dash", "—" not in rec["text"])
        r2 = fs.run_drops(NOW, base=base)
        check("re-run notifies nobody (deduped)", r2["notified"] == 0)
        outbox2 = (base / "channel_outbox.jsonl").read_text().strip().splitlines()
        check("still one outbox record", len(outbox2) == 1)


def test_reconcile_prunes_answered():
    print("reconcile drops ledger entries for answered/gone threads:")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_json(tmp, "config.json", {"followup_enabled": True})
        # ledger has an entry, but the thread now shows a fresh inbound (they replied)
        _write_json(tmp, "followup_state.json",
                    {"fb:r": {"side": "sell", "disposition": "active"},
                     "fb:gone": {"side": "sell", "disposition": "not_interested"}})
        _write_json(tmp, "threads/fb:r.json",
                    _thread(thread_id="fb:r", rows=[_msg("out", "r", _ago(2)), _msg("in", "yo", _ago(1))]))
        res = fs.run_reconcile(NOW, base=base)
        check("both stale entries pruned", res["dropped"] == 2)
        check("nothing kept", res["kept"] == 0)


# ---- CLI smoke -------------------------------------------------------------

def test_cli():
    print("CLI: due/mark-nudge/mark-drop exit codes + JSON shape via BAZAAR_DATA_DIR:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"followup_enabled": True})
        _write_json(tmp, "threads/fb:t.json",
                    _thread(thread_id="fb:t", rows=[_msg("in", "hi", _ago(3)), _msg("out", "r", _ago(1.5))]))
        env = {**os.environ, "BAZAAR_DATA_DIR": tmp}
        exe = [sys.executable, str(ROOT / "bin" / "followup_state.py")]
        p = subprocess.run(exe + ["due", "--now", NOW.isoformat()], capture_output=True, text=True, env=env)
        check("due exit 0", p.returncode == 0)
        check("due JSON has one nudge", json.loads(p.stdout)["counts"]["nudges"] == 1)
        bad = subprocess.run(exe + ["mark-nudge"], capture_output=True, text=True, env=env)
        check("mark-nudge without args -> exit 2", bad.returncode == 2)
        ok = subprocess.run(exe + ["mark-nudge", "--thread", "fb:t", "--side", "sell",
                                   "--now", NOW.isoformat()], capture_output=True, text=True, env=env)
        check("mark-nudge exit 0", ok.returncode == 0)


if __name__ == "__main__":
    print("followup_state.py tests\n")
    test_trailing_outbound()
    test_awaiting_counterpart()
    test_schedule_for()
    test_due_decision()
    test_reconcile_ledger()
    test_config_parsing()
    test_scan_due_partitions()
    test_scan_due_disabled_and_escalation()
    test_mark_nudge_then_not_due()
    test_mark_drop_disposition()
    test_run_drops_notifies_once()
    test_reconcile_prunes_answered()
    test_cli()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
