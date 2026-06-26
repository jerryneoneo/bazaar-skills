#!/usr/bin/env python3
"""Tests for tab_registry — resolve a marketplace to the STABLE IDENTITY of its browser tab.

Runnable with plain python (no pytest needed):

    python3 tests/test_tab_registry.py

Focus: the resolver returns the correct stable host / url_prefix / match_hint for a known
market+region, infers the region from the seller/buyer config, lets an explicit --region
override win, and fails loudly on unknown markets and bad input. Plus the property that makes
it safe under concurrency: it is a PURE function of (market, region) — any number of workers
resolving the same marketplace at once get the EXACT SAME stable identity, so no worker is ever
handed a different (index-drifted) tab to drive.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import tab_registry as tr  # noqa: E402

BIN = str(ROOT / "bin" / "tab_registry.py")

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def _write_config(directory, *, side="seller", region="SG", marketplaces=None):
    """Write a minimal <side>_config.json into a temp data dir; return its path."""
    if marketplaces is None:
        marketplaces = {
            "fb": {"enabled": True},
            "carousell": {"enabled": True},
            "ebay": {"enabled": False},
        }
    payload = {"region": region, "marketplaces": marketplaces}
    path = Path(directory) / f"{side}_config.json"
    path.write_text(json.dumps(payload))
    return path


def _run(args, env=None):
    return subprocess.run([sys.executable, BIN] + args, capture_output=True, text=True, env=env)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_url_prefix_normalises_trailing_slash():
    print("url_prefix normalisation:")
    check("adds https + trailing slash", tr.url_prefix_for_host("www.carousell.sg") == "https://www.carousell.sg/")
    check("does not double the slash", tr.url_prefix_for_host("offerup.com/") == "https://offerup.com/")


def test_match_hint_strips_www():
    print("match_hint stability:")
    check("drops leading www. so it matches bare + www tab URLs",
          tr.match_hint_for_host("www.carousell.sg") == "carousell.sg")
    check("bare host passes through", tr.match_hint_for_host("offerup.com") == "offerup.com")
    check("lowercases", tr.match_hint_for_host("WWW.Ebay.COM.sg") == "ebay.com.sg")


def test_build_tab_identity_immutable_shape():
    print("build_tab_identity shape + immutability:")
    identity = tr.build_tab_identity("carousell", "www.carousell.sg", "SG")
    check("market", identity["market"] == "carousell")
    check("host", identity["host"] == "www.carousell.sg")
    check("url_prefix", identity["url_prefix"] == "https://www.carousell.sg/")
    check("match_hint", identity["match_hint"] == "carousell.sg")
    check("region", identity["region"] == "SG")
    # Mutating the returned dict must not be observable elsewhere (it is a fresh dict).
    snapshot = dict(identity)
    identity["host"] = "tampered"
    second = tr.build_tab_identity("carousell", "www.carousell.sg", "SG")
    check("each call returns a fresh dict", second == snapshot)


def test_enabled_markets_shapes():
    print("enabled_markets tolerates both config shapes:")
    check("object shape filters on enabled",
          tr.enabled_markets({"fb": {"enabled": True}, "ebay": {"enabled": False}}) == ["fb"])
    check("legacy array shape: all enabled",
          tr.enabled_markets(["fb", "carousell"]) == ["fb", "carousell"])
    check("garbage -> empty", tr.enabled_markets(None) == [])


def test_resolve_region_override_wins():
    print("resolve_region precedence:")
    check("override wins over config", tr.resolve_region({"region": "SG"}, "MY") == "MY")
    check("falls back to config region", tr.resolve_region({"region": "SG"}, None) == "SG")
    check("no region anywhere -> None", tr.resolve_region({}, None) is None)


# ---------------------------------------------------------------------------
# CLI: resolve
# ---------------------------------------------------------------------------
def test_cli_resolve_known_market_region():
    print("CLI resolve known market+region (carousell SG -> www.carousell.sg):")
    with tempfile.TemporaryDirectory() as d:
        _write_config(d, side="seller", region="SG")
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = _run(["resolve", "--market", "carousell"], env=env)
        check("exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("host is the SG regional host", payload["host"] == "www.carousell.sg")
        check("url_prefix is canonical https", payload["url_prefix"] == "https://www.carousell.sg/")
        check("match_hint is the stable substring", payload["match_hint"] == "carousell.sg")
        check("region inferred from seller config", payload["region"] == "SG")
        check("market echoed back", payload["market"] == "carousell")


def test_cli_region_inferred_from_buyer_config():
    print("CLI resolve infers region from the BUYER config (--side buy):")
    with tempfile.TemporaryDirectory() as d:
        _write_config(d, side="buyer", region="MY")
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = _run(["resolve", "--market", "carousell", "--side", "buy"], env=env)
        check("exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("region inferred from buyer config", payload["region"] == "MY")
        check("host follows the inferred region", payload["host"] == "www.carousell.com.my")


def test_cli_region_override_wins():
    print("CLI --region override beats the config region:")
    with tempfile.TemporaryDirectory() as d:
        _write_config(d, side="seller", region="SG")  # config says SG
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = _run(["resolve", "--market", "carousell", "--region", "HK"], env=env)
        check("exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("override region used", payload["region"] == "HK")
        check("host follows the override, not the config", payload["host"] == "www.carousell.com.hk")


def test_cli_unknown_market_exits_3():
    print("CLI unknown market -> exit 3:")
    with tempfile.TemporaryDirectory() as d:
        _write_config(d, side="seller", region="SG")
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = _run(["resolve", "--market", "nope_not_a_market"], env=env)
        check("unknown market exits 3", out.returncode == 3)
        check("error on stderr", "error" in json.loads(out.stderr))


def test_cli_explicit_config_path():
    print("CLI --config explicit path overrides the data-dir default:")
    with tempfile.TemporaryDirectory() as d:
        cfg = _write_config(d, side="seller", region="TW")
        # No BAZAAR_DATA_DIR set — prove --config alone supplies the region.
        out = _run(["resolve", "--market", "carousell", "--config", str(cfg)])
        check("exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        check("region read from the explicit config", payload["region"] == "TW")
        check("host follows it", payload["host"] == "www.carousell.tw")


def test_cli_bad_input_exits_2():
    print("CLI bad input -> exit 2:")
    bad = [
        ["resolve"],                                   # missing --market
        ["resolve", "--market", ""],                   # empty market
        ["resolve", "--market", "fb", "--side", "x"],  # invalid side
        ["bogus", "--market", "fb"],                   # unknown command
    ]
    ok = True
    for args in bad:
        proc = _run(args)
        if proc.returncode != 2:
            ok = False
            print(f"    expected exit 2, got {proc.returncode} for: {args}")
    check("all malformed input exits 2", ok)


def test_cli_missing_config_exits_3():
    print("CLI missing config (no region source) -> exit 3:")
    with tempfile.TemporaryDirectory() as d:
        # Empty data dir: no seller_config.json present.
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = _run(["resolve", "--market", "carousell"], env=env)
        check("missing config exits 3", out.returncode == 3)


# ---------------------------------------------------------------------------
# CLI: list
# ---------------------------------------------------------------------------
def test_cli_list_enabled_only():
    print("CLI list resolves only ENABLED markets:")
    with tempfile.TemporaryDirectory() as d:
        _write_config(d, side="seller", region="SG", marketplaces={
            "fb": {"enabled": True},
            "carousell": {"enabled": True},
            "ebay": {"enabled": False},
        })
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        out = _run(["list"], env=env)
        check("exits 0", out.returncode == 0)
        payload = json.loads(out.stdout)
        markets = [t["market"] for t in payload["tabs"]]
        check("includes the two enabled markets", markets == ["fb", "carousell"])
        check("excludes the disabled market", "ebay" not in markets)
        hosts = {t["market"]: t["host"] for t in payload["tabs"]}
        check("fb host", hosts["fb"] == "www.facebook.com")
        check("carousell SG host", hosts["carousell"] == "www.carousell.sg")


# ---------------------------------------------------------------------------
# concurrency invariant: a pure resolver is race-free and index-free
# ---------------------------------------------------------------------------
def test_concurrent_resolve_is_deterministic():
    print("INVARIANT: concurrent resolves all yield the SAME stable identity (no index drift):")
    n_workers = 12
    with tempfile.TemporaryDirectory() as d:
        _write_config(d, side="seller", region="SG")
        env = {**os.environ, "BAZAAR_DATA_DIR": d}
        args = [sys.executable, BIN, "resolve", "--market", "carousell"]
        procs = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(n_workers)]
        payloads = []
        codes = []
        for p in procs:
            stdout, _ = p.communicate()
            codes.append(p.returncode)
            payloads.append(json.loads(stdout))
        check("every concurrent resolve exits 0", all(c == 0 for c in codes))
        # The whole point: a stable property, not a shifting index. All workers must agree.
        first = payloads[0]
        check("all workers resolve to the identical stable identity",
              all(p == first for p in payloads))
        check("the agreed identity is the SG host", first["host"] == "www.carousell.sg")
        check("no payload exposes a raw tab index",
              all("index" not in p and "tab_index" not in p for p in payloads))


if __name__ == "__main__":
    print("tab_registry tests\n")
    test_url_prefix_normalises_trailing_slash()
    test_match_hint_strips_www()
    test_build_tab_identity_immutable_shape()
    test_enabled_markets_shapes()
    test_resolve_region_override_wins()
    test_cli_resolve_known_market_region()
    test_cli_region_inferred_from_buyer_config()
    test_cli_region_override_wins()
    test_cli_unknown_market_exits_3()
    test_cli_explicit_config_path()
    test_cli_bad_input_exits_2()
    test_cli_missing_config_exits_3()
    test_cli_list_enabled_only()
    test_concurrent_resolve_is_deterministic()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
