#!/usr/bin/env python3
"""Tests for ui_cache — the "page memory" selector cache for the listing flow.

Runnable with plain python (no pytest needed):

    python3 tests/test_ui_cache.py

Focus: the cache is a HINT that fails open. A miss/corrupt/stale entry must always degrade to "no
usable cache" (→ vision), never to a confident wrong action. Plus immutable doc updates, the page-
url guard, freshness staleness, atomic round-trips via the CLI, and prune.
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

import ui_cache as uc  # noqa: E402

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
CLI = [sys.executable, str(ROOT / "bin" / "ui_cache.py")]

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _iso(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _step(url="carousell.*/sell/new", verified_days_ago=0, fail_count=0):
    return {"strategy": "css", "query": "input[name='title']", "action_kind": "type",
            "page_url_pattern": url, "recorded_at": _iso(verified_days_ago),
            "last_verified_at": _iso(verified_days_ago), "last_ok_at": _iso(verified_days_ago),
            "fail_count": fail_count}


def test_safe_segment():
    print("safe_segment rejects path traversal:")
    check("accepts a plain id", uc.safe_segment("carousell", "market") == "carousell")
    for bad in ["../etc", "a/b", "..", ".", ".hidden", ""]:
        rejected = False
        try:
            uc.safe_segment(bad, "market")
        except ValueError:
            rejected = True
        check(f"rejects {bad!r}", rejected)


def test_record_step_immutable():
    print("record_step is immutable and preserves recorded_at, resets fail_count:")
    doc = uc.new_doc("carousell", "listing", _iso(5))
    doc["steps"]["title_field"] = _step(verified_days_ago=5, fail_count=2)
    fields = {"strategy": "aria", "query": "[aria-label='Title']", "action_kind": "type",
              "page_url_pattern": "carousell.*/sell/new"}
    updated = uc.record_step(doc, "carousell", "listing", "title_field", fields, NOW.isoformat())
    check("returns the new query", updated["steps"]["title_field"]["query"] == "[aria-label='Title']")
    check("resets fail_count to 0", updated["steps"]["title_field"]["fail_count"] == 0)
    check("preserves original recorded_at", updated["steps"]["title_field"]["recorded_at"] == _iso(5))
    check("refreshes last_verified_at", updated["steps"]["title_field"]["last_verified_at"] == NOW.isoformat())
    check("does not mutate the input doc", doc["steps"]["title_field"]["query"] == "input[name='title']")


def test_record_step_creates_doc_when_absent():
    print("record_step bootstraps a doc when there is none:")
    fields = {"strategy": "css", "query": "#price", "action_kind": "type",
              "page_url_pattern": "carousell.*/sell/new"}
    updated = uc.record_step(None, "carousell", "listing", "price_field", fields, NOW.isoformat())
    check("creates the steps map", updated["steps"]["price_field"]["query"] == "#price")
    check("stamps schema_version", updated["schema_version"] == uc.SCHEMA_VERSION)


def test_is_stale():
    print("is_stale: fresh ok; missing url guard, age, and fail_count all force stale:")
    check("fresh step is NOT stale", uc.is_stale(_step(), NOW) is False)
    check("missing page_url_pattern is stale", uc.is_stale(_step(url=""), NOW) is True)
    check("verified 60 days ago is stale", uc.is_stale(_step(verified_days_ago=60), NOW) is True)
    check("verified 5 days ago is NOT stale", uc.is_stale(_step(verified_days_ago=5), NOW) is False)
    check("fail_count >= STALE_FAILS is stale", uc.is_stale(_step(fail_count=uc.STALE_FAILS), NOW) is True)
    check("non-dict is stale", uc.is_stale(None, NOW) is True)


def test_drop_step():
    print("drop_step removes one step, immutable, idempotent:")
    doc = uc.new_doc("fb", "listing", NOW.isoformat())
    doc["steps"]["title_field"] = _step()
    updated, dropped = uc.drop_step(doc, "title_field")
    check("reports dropped", dropped is True)
    check("step is gone", "title_field" not in updated["steps"])
    check("input doc unchanged", "title_field" in doc["steps"])
    _, dropped_again = uc.drop_step(updated, "title_field")
    check("dropping a missing step reports False", dropped_again is False)


def test_prune_doc():
    print("prune_doc drops stale steps, keeps fresh ones:")
    doc = uc.new_doc("carousell", "listing", NOW.isoformat())
    doc["steps"]["fresh"] = _step(verified_days_ago=1)
    doc["steps"]["old"] = _step(verified_days_ago=99)
    doc["steps"]["noguard"] = _step(url="")
    updated, removed = uc.prune_doc(doc, NOW)
    check("keeps the fresh step", "fresh" in updated["steps"])
    check("removes the aged-out step", "old" in removed and "old" not in updated["steps"])
    check("removes the unguarded step", "noguard" in removed)


def _cli(args, data_dir):
    env = {**os.environ, "BAZAAR_DATA_DIR": data_dir}
    return subprocess.run(CLI + args, capture_output=True, text=True, env=env)


def test_cli_record_get_invalidate_roundtrip():
    print("CLI: record -> get(hit) -> invalidate -> get(miss), isolated via BAZAAR_DATA_DIR:")
    with tempfile.TemporaryDirectory() as d:
        rec = _cli(["record", "--market", "carousell", "--flow", "listing", "--step", "title_field",
                    "--strategy", "css", "--query", "input[name='title']", "--action-kind", "type",
                    "--url-pattern", "carousell.*/sell/new"], d)
        check("record exits 0", rec.returncode == 0)
        # The file actually landed where cache_path says.
        cfile = Path(d) / "ui_cache" / "carousell" / "listing.json"
        check("cache file written", cfile.exists())
        got = _cli(["get", "--market", "carousell", "--flow", "listing", "--step", "title_field"], d)
        gp = json.loads(got.stdout)
        check("get reports a hit", gp["hit"] is True)
        check("fresh entry is not stale", gp["stale"] is False)
        check("selector query round-trips", gp["selector"]["query"] == "input[name='title']")
        inv = _cli(["invalidate", "--market", "carousell", "--flow", "listing", "--step", "title_field"], d)
        check("invalidate reports dropped", json.loads(inv.stdout)["dropped"] is True)
        miss = _cli(["get", "--market", "carousell", "--flow", "listing", "--step", "title_field"], d)
        mp = json.loads(miss.stdout)
        check("get after invalidate is a miss", mp["hit"] is False and mp["selector"] is None)


def test_cli_missing_file_is_fail_open_miss():
    print("CLI: get with no cache file at all is a clean miss (exit 0), never an error:")
    with tempfile.TemporaryDirectory() as d:
        got = _cli(["get", "--market", "fb", "--flow", "listing", "--step", "price_field"], d)
        check("get exits 0 with no file", got.returncode == 0)
        check("reports hit:false", json.loads(got.stdout)["hit"] is False)


def test_cli_corrupt_file_fails_open():
    print("CLI: a corrupt cache file degrades to a miss, not a crash:")
    with tempfile.TemporaryDirectory() as d:
        cfile = Path(d) / "ui_cache" / "fb" / "listing.json"
        cfile.parent.mkdir(parents=True)
        cfile.write_text("{ this is not json")
        got = _cli(["get", "--market", "fb", "--flow", "listing", "--step", "title_field"], d)
        check("get exits 0 on corrupt file", got.returncode == 0)
        check("corrupt file reads as a miss", json.loads(got.stdout)["hit"] is False)


def test_cli_record_validation():
    print("CLI: record rejects a missing url-pattern and a bad strategy:")
    with tempfile.TemporaryDirectory() as d:
        no_url = _cli(["record", "--market", "fb", "--flow", "listing", "--step", "title_field",
                       "--strategy", "css", "--query", "#t"], d)
        check("record without --url-pattern exits 2", no_url.returncode == 2)
        bad_strat = _cli(["record", "--market", "fb", "--flow", "listing", "--step", "title_field",
                          "--strategy", "xpath", "--query", "#t", "--url-pattern", "fb.*"], d)
        check("record with an unknown --strategy exits 2", bad_strat.returncode == 2)


def test_cli_path_traversal_rejected():
    print("CLI: a market id that could traverse the tree is rejected:")
    with tempfile.TemporaryDirectory() as d:
        out = _cli(["get", "--market", "../../etc", "--flow", "listing", "--step", "x"], d)
        check("traversal market exits 2", out.returncode == 2)


def test_cli_prune_ages_out_and_deletes_empty():
    print("CLI: prune drops an aged-out step and deletes the now-empty flow file:")
    with tempfile.TemporaryDirectory() as d:
        cfile = Path(d) / "ui_cache" / "carousell" / "listing.json"
        cfile.parent.mkdir(parents=True)
        old = datetime.now(timezone.utc) - timedelta(days=99)
        doc = uc.new_doc("carousell", "listing", old.isoformat())
        doc["steps"]["title_field"] = {"strategy": "css", "query": "#t", "action_kind": "type",
                                       "page_url_pattern": "carousell.*", "recorded_at": old.isoformat(),
                                       "last_verified_at": old.isoformat(), "last_ok_at": old.isoformat(),
                                       "fail_count": 0}
        cfile.write_text(json.dumps(doc))
        out = _cli(["prune", "--max-age-days", "30"], d)
        check("prune exits 0", out.returncode == 0)
        check("aged-out step is reported pruned", "title_field" in json.dumps(json.loads(out.stdout)))
        check("empty flow file is deleted", not cfile.exists())


if __name__ == "__main__":
    print("ui_cache tests\n")
    test_safe_segment()
    test_record_step_immutable()
    test_record_step_creates_doc_when_absent()
    test_is_stale()
    test_drop_step()
    test_prune_doc()
    test_cli_record_get_invalidate_roundtrip()
    test_cli_missing_file_is_fail_open_miss()
    test_cli_corrupt_file_fails_open()
    test_cli_record_validation()
    test_cli_path_traversal_rejected()
    test_cli_prune_ages_out_and_deletes_empty()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
