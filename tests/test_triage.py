#!/usr/bin/env python3
"""Tests for triage.py — the read-only "awaiting you" aggregator.

    python3 tests/test_triage.py

Focus: build_digest reads only local data/ state and reports every actionable signal
(open escalations both sides, unread managed threads both sides, draft/undistributed
listings, open checkouts, open wants, overdue cadence) without ever reading or echoing
a secret. State is isolated by passing an explicit data_dir to build_digest; the digest
is deterministic given a fixed `now`. This also pins the consolidated behavior of the
old find_unread.py / find_unhandled.py prototypes so removing them loses no coverage.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import triage  # noqa: E402

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-06-01T00:00:00+00:00"      # ~26 days before NOW -> overdue
RECENT = "2026-06-27T11:30:00+00:00"   # 30 min before NOW -> not overdue

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


# ---- fixture writers -------------------------------------------------------

def _dir(tmp, *parts):
    p = Path(tmp).joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


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


def _thread(status, transcript, cursor_id):
    return {"status": status, "cursor": {"last_handled_msg_id": cursor_id}, "transcript": transcript}


def _in(mid, text):
    return {"msg_id": mid, "dir": "in", "text": text, "ts": "2026-06-26T10:00:00+00:00"}


def _out(mid, text):
    return {"msg_id": mid, "dir": "out", "text": text, "ts": "2026-06-26T11:00:00+00:00"}


# ---- tests -----------------------------------------------------------------

def test_open_escalations_both_sides():
    print("open escalations (sell + buy), resolved ones excluded:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_jsonl(tmp, "escalations.jsonl", [
            {"thread_id": "fb:1", "item_id": "x", "kind": "price_offer",
             "open_question": "accept $20?", "status": "open", "ts": "t"},
            {"thread_id": "fb:2", "item_id": "y", "kind": "listing_anomaly", "status": "resolved"},
        ])
        _write_jsonl(tmp, "buyer_escalations.jsonl", [
            {"thread_id": "csll:9", "kind": "no_buyer_protection", "status": "open"},
        ])
        d = triage.build_digest(Path(tmp), NOW)
        check("two open escalations counted", d["counts"]["escalations"] == 2)
        sides = sorted(e["side"] for e in d["escalations"])
        check("one sell + one buy side", sides == ["buy", "sell"])
        check("resolved escalation excluded", all(e["item_id"] != "y" for e in d["escalations"]))


def test_unread_sell_thread_detected():
    print("unread sell thread: new inbound after cursor is flagged:")
    with tempfile.TemporaryDirectory() as tmp:
        # buyer's last message is AFTER the cursor -> unread
        _write_json(tmp, "threads/fb:1.json",
                    _thread("active", [_out("o1", "hi"), _in("i2", "still there?")], "o1"))
        d = triage.build_digest(Path(tmp), NOW)
        check("one buyer waiting", d["counts"]["buyers_waiting"] == 1)
        check("last inbound text carried", d["buyers_waiting"][0]["last_in_text"] == "still there?")


def test_replied_thread_not_unread():
    print("thread we already replied to (outbound after last inbound) is NOT unread:")
    with tempfile.TemporaryDirectory() as tmp:
        # cursor sits on our outbound reply, which came after the buyer's message
        _write_json(tmp, "threads/fb:1.json",
                    _thread("active", [_in("i1", "is it available?"), _out("o2", "yes!")], "o2"))
        d = triage.build_digest(Path(tmp), NOW)
        check("caught-up thread not flagged", d["counts"]["buyers_waiting"] == 0)


def test_unread_skips_terminal_and_escalated():
    print("handover / lost / escalated / held threads are excluded from unread:")
    with tempfile.TemporaryDirectory() as tmp:
        for i, st in enumerate(["handover", "lost", "escalated", "held", "closed"]):
            _write_json(tmp, f"threads/fb:{i}.json",
                        _thread(st, [_out("o1", "hi"), _in("i2", "ping")], "o1"))
        d = triage.build_digest(Path(tmp), NOW)
        check("no terminal/escalated thread flagged", d["counts"]["buyers_waiting"] == 0)


def test_unread_no_cursor_means_unread():
    print("thread with inbound and no cursor -> unread:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "threads/fb:1.json", {"status": "active", "transcript": [_in("i1", "hello")]})
        d = triage.build_digest(Path(tmp), NOW)
        check("uncursored inbound flagged", d["counts"]["buyers_waiting"] == 1)


def test_unread_buy_thread_detected():
    print("unread buy thread (seller replied) -> sellers_waiting:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "buyer_threads/csll:5.json",
                    _thread("liaising", [_out("o1", "still available?"), _in("i2", "yes, $50")], "o1"))
        d = triage.build_digest(Path(tmp), NOW)
        check("one seller waiting", d["counts"]["sellers_waiting"] == 1)


def test_listings_draft_and_undistributed():
    print("draft + live-undistributed listings flagged, fully-listed + sold are not:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "seller_config.json",
                    {"marketplaces": {"fb": {"enabled": True}, "carousell": {"enabled": True},
                                      "ebay": {"enabled": False}}})
        _write_json(tmp, "items/a.json",
                    {"item_id": "a", "title": "A", "status": "draft", "listing_urls": {}})
        _write_json(tmp, "items/b.json",
                    {"item_id": "b", "title": "B", "status": "live",
                     "listing_urls": {"fb": "u"}})  # missing carousell -> undistributed
        _write_json(tmp, "items/c.json",
                    {"item_id": "c", "title": "C", "status": "live",
                     "listing_urls": {"fb": "u", "carousell": "u"}})  # fully listed
        _write_json(tmp, "items/d.json",
                    {"item_id": "d", "title": "D", "status": "sold", "listing_urls": {"fb": "u"}})
        d = triage.build_digest(Path(tmp), NOW)
        issues = {row["item_id"]: row for row in d["listings"]}
        check("two listing tasks", d["counts"]["listings"] == 2)
        check("a is a draft", issues.get("a", {}).get("issue") == "draft")
        check("b is undistributed", issues.get("b", {}).get("issue") == "undistributed")
        check("b names the missing market", "carousell" in issues.get("b", {}).get("detail", ""))
        check("disabled market not required", "ebay" not in issues.get("b", {}).get("detail", ""))
        check("fully-listed item not flagged", "c" not in issues)
        check("sold item not flagged", "d" not in issues)


def test_followups_due_surfaced():
    print("stale chat (we sent last msg, aged past the gap) surfaces under followups:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"followup_enabled": True})
        # buyer's last contact, then our reply ~1d+ ago and unanswered -> nudge #1 due
        _write_json(tmp, "threads/fb:1.json",
                    _thread("active", [_in("i1", "interested"), _out("o2", "yes, available")], "o2"))
        d = triage.build_digest(Path(tmp), NOW)
        check("one follow-up due", d["counts"]["followups"] == 1)
        check("flagged as a nudge", d["followups"][0]["action"] == "nudge")

    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"followup_enabled": False})
        _write_json(tmp, "threads/fb:1.json",
                    _thread("active", [_in("i1", "interested"), _out("o2", "yes, available")], "o2"))
        d = triage.build_digest(Path(tmp), NOW)
        check("disabled -> no follow-ups surfaced", d["counts"]["followups"] == 0)


def test_listings_stale_surfaced():
    print("live published listing with no buyer interest for 7d+ surfaces under listings_stale:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"listing_health_enabled": True, "stale_days": 7})
        # published 2026-06-01 (~26d before NOW), never any inbound -> stale
        _write_json(tmp, "items/silent.json",
                    {"item_id": "silent", "title": "Silent", "status": "live",
                     "list_price": 20, "listing_urls": {"fb": "u"}, "imported_at": "2026-06-01"})
        # a draft is NOT a staleness signal (owned by the draft/undistributed row)
        _write_json(tmp, "items/draft.json",
                    {"item_id": "draft", "title": "Draft", "status": "draft", "listing_urls": {}})
        d = triage.build_digest(Path(tmp), NOW)
        ids = {r["item_id"] for r in d["listings_stale"]}
        check("one stale listing", d["counts"]["listings_stale"] == 1)
        check("the silent one flagged", "silent" in ids)
        check("draft not in stale", "draft" not in ids)
        check("draft still in listings", any(r["item_id"] == "draft" for r in d["listings"]))


def test_open_checkouts():
    print("issued/pending checkouts flagged, completed excluded:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "checkouts/1.json",
                    {"sale_id": "s1", "item_id": "a", "thread_id": "fb:1", "status": "issued"})
        _write_json(tmp, "checkouts/2.json",
                    {"sale_id": "s2", "item_id": "b", "thread_id": "fb:2", "status": "completed"})
        d = triage.build_digest(Path(tmp), NOW)
        check("one open checkout", d["counts"]["checkouts"] == 1)
        check("the issued one", d["checkouts"][0]["sale_id"] == "s1")


def test_open_wants():
    print("wants liaising/agreed/recommend listed, cancelled/completed excluded:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "wants/w1.json", {"want_id": "w1", "query": "ipad", "status": "liaising"})
        _write_json(tmp, "wants/w2.json", {"want_id": "w2", "query": "desk", "status": "cancelled"})
        _write_json(tmp, "wants/w3.json", {"want_id": "w3", "query": "lamp", "status": "recommend"})
        d = triage.build_digest(Path(tmp), NOW)
        check("two open wants", d["counts"]["wants_open"] == 2)
        ids = sorted(w["want_id"] for w in d["wants_open"])
        check("w1 and w3 open", ids == ["w1", "w3"])


def test_cadence_overdue_and_fresh():
    print("scan/eval cadence overdue when stale, quiet when fresh:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"scan_interval_hours": 24, "eval_interval_hours": 24})
        _write_json(tmp, "seller_config.json", {"marketplaces": {"fb": {"enabled": True}}})
        _write_json(tmp, "scan_state.json", {"fb": {"last_scanned_at": OLD}})
        _write_json(tmp, "eval_state.json", {"last_eval_at": OLD})
        d = triage.build_digest(Path(tmp), NOW)
        kinds = sorted(c["kind"] for c in d["cadence"])
        check("scan + eval overdue", kinds == ["eval_overdue", "scan_overdue"])

    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "config.json", {"scan_interval_hours": 24, "eval_interval_hours": 24})
        _write_json(tmp, "seller_config.json", {"marketplaces": {"fb": {"enabled": True}}})
        _write_json(tmp, "scan_state.json", {"fb": {"last_scanned_at": RECENT}})
        _write_json(tmp, "eval_state.json", {"last_eval_at": RECENT})
        d = triage.build_digest(Path(tmp), NOW)
        check("nothing overdue when fresh", d["counts"]["cadence"] == 0)


def test_empty_dir_is_all_caught_up():
    print("empty data dir -> every count zero:")
    with tempfile.TemporaryDirectory() as tmp:
        d = triage.build_digest(Path(tmp), NOW)
        check("total is zero", d["counts"]["total"] == 0)
        check("counts present for every category",
              set(d["counts"]) >= {"escalations", "buyers_waiting", "sellers_waiting",
                                   "wants_open", "listings", "checkouts", "cadence", "total"})


def test_malformed_files_fail_open():
    print("malformed json / jsonl never raises (read-only, fail-open):")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "escalations.jsonl").write_text("{not json\n{\"status\":\"open\",\"kind\":\"k\"}\n")
        _dir(tmp, "items")
        (Path(tmp) / "items" / "bad.json").write_text("{ broken")
        try:
            d = triage.build_digest(Path(tmp), NOW)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"    raised: {exc}")
        check("no exception on malformed input", ok)
        check("valid jsonl line still parsed", ok and d["counts"]["escalations"] == 1)


def test_no_secret_leak():
    print("never reads or echoes a floor / budget secret:")
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "floors/a.json", {"floor_price": 99999})
        _write_json(tmp, "budgets/w1.json", {"max_budget": 88888})
        _write_json(tmp, "items/a.json",
                    {"item_id": "a", "title": "A", "status": "draft", "listing_urls": {}})
        d = triage.build_digest(Path(tmp), NOW)
        blob = json.dumps(d) + triage.render(d)
        check("floor value absent", "99999" not in blob)
        check("budget value absent", "88888" not in blob)
        check("no floor_price key leaked", "floor_price" not in blob)


def test_cli_json_runs(tmpdir_unused=None):
    print("CLI --json against an isolated data dir emits valid JSON, no secret:")
    import os
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        _write_json(tmp, "floors/a.json", {"floor_price": 77777})
        env = {**os.environ, "SELLY_DATA_DIR": tmp}
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "triage.py"), "--json"],
                           capture_output=True, text=True, env=env)
        check("exit 0", p.returncode == 0)
        try:
            parsed = json.loads(p.stdout)
            ok = "counts" in parsed
        except json.JSONDecodeError:
            ok = False
        check("stdout is the digest JSON", ok)
        check("no secret in stdout", "77777" not in p.stdout)


if __name__ == "__main__":
    print("triage tests\n")
    test_open_escalations_both_sides()
    test_unread_sell_thread_detected()
    test_replied_thread_not_unread()
    test_unread_skips_terminal_and_escalated()
    test_unread_no_cursor_means_unread()
    test_unread_buy_thread_detected()
    test_listings_draft_and_undistributed()
    test_followups_due_surfaced()
    test_listings_stale_surfaced()
    test_open_checkouts()
    test_open_wants()
    test_cadence_overdue_and_fresh()
    test_empty_dir_is_all_caught_up()
    test_malformed_files_fail_open()
    test_no_secret_leak()
    test_cli_json_runs()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
