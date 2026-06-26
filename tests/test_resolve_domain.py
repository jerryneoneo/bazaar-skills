#!/usr/bin/env python3
"""Tests for bin/resolve_domain.py — region → marketplace host resolution.

    python3 tests/test_resolve_domain.py

The regional `domains` map wins; a region with no entry falls back to `domains["*"]`; a market with
no `domains` map at all falls back to the `listing_url.host` suffix; unknown markets resolve to None.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import resolve_domain as r  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_regional_host_wins():
    print("exact regional host:")
    check("ebay SG → ebay.com.sg", r.resolve("ebay", "SG") == "www.ebay.com.sg")
    check("ebay US → ebay.com", r.resolve("ebay", "US") == "www.ebay.com")
    check("carousell MY → com.my", r.resolve("carousell", "MY") == "www.carousell.com.my")
    check("mercari JP → jp.mercari", r.resolve("mercari", "JP") == "jp.mercari.com")


def test_star_fallback():
    print("'*' default when region absent from map:")
    # eBay has a "*" default; JP is not an eBay region entry → falls back to the global host.
    check("ebay JP → '*' (ebay.com)", r.resolve("ebay", "JP") == "www.ebay.com")
    check("ebay no region → '*'", r.resolve("ebay", None) == "www.ebay.com")
    check("fb any region → '*'", r.resolve("fb", "SG") == "www.facebook.com")


def test_listing_url_suffix_fallback():
    print("listing_url.host fallback when no domains map:")
    # craigslist has no `domains` map (host is metro-derived) → falls back to listing_url.host.
    check("craigslist → craigslist.org", r.resolve("craigslist", "US") == "craigslist.org")
    # mercari has no SG entry and no "*" → falls back to listing_url.host suffix.
    check("mercari SG → 'mercari.' suffix", r.resolve("mercari", "SG") == "mercari.")


def test_unknown_market():
    print("unknown market → None:")
    check("nope → None", r.resolve("nope", "SG") is None)


def test_cli_exit_codes():
    print("CLI exit codes:")
    good = r.main(["resolve_domain.py", "--market", "ebay", "--region", "SG"])
    bad = r.main(["resolve_domain.py", "--market", "nope", "--region", "SG"])
    check("resolvable → exit 0", good == 0)
    check("unknown → exit 1", bad == 1)


if __name__ == "__main__":
    print("resolve_domain tests\n")
    test_regional_host_wins()
    test_star_fallback()
    test_listing_url_suffix_fallback()
    test_unknown_market()
    test_cli_exit_codes()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
