#!/usr/bin/env python3
"""Tests for bin/verify_listing_url.py — the fabricated-link guard.

    python3 tests/test_verify_listing_url.py

Real per-site permalinks (the ones the listing-flow recipes document reading from the live page)
must pass; wrong-host / made-up / empty URLs must fail closed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import verify_listing_url as v  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def ok(market, url, region=None):
    return v.verify(market, url, region)[0]


def test_real_permalinks_pass():
    print("real per-site permalinks pass:")
    check("fb /marketplace/item/", ok("fb", "https://www.facebook.com/marketplace/item/123456789/"))
    check("carousell /p/", ok("carousell", "https://www.carousell.sg/p/sony-xm5-123456/"))
    check("ebay /itm/", ok("ebay", "https://www.ebay.com/itm/364512345678"))
    check("mercari /item/", ok("mercari", "https://www.mercari.com/us/item/m12345678901/"))
    check("offerup /item/", ok("offerup", "https://offerup.com/item/detail/abc-123/"))
    check("poshmark /listing/", ok("poshmark", "https://poshmark.com/listing/Nike-Tee-66f0a1b2c3"))
    check("craigslist /d/", ok("craigslist", "https://sfbay.craigslist.org/sfc/ele/d/sony-xm5/123.html"))


def test_fabricated_urls_fail():
    print("fabricated / wrong-host / malformed URLs fail:")
    check("wrong host (fb.com shortlink)", not ok("fb", "https://fb.com/item/fake-id"))
    check("plausible but wrong path", not ok("fb", "https://www.facebook.com/profile/123"))
    check("ebay url given to carousell", not ok("carousell", "https://www.ebay.com/itm/364512345678"))
    check("empty string", not ok("fb", ""))
    check("whitespace only", not ok("fb", "   "))
    check("no scheme", not ok("ebay", "ebay.com/itm/123"))
    check("non-http scheme", not ok("fb", "javascript:alert(1)"))
    check("unknown market", not ok("nope", "https://www.facebook.com/marketplace/item/1/"))


def test_region_gate():
    print("region gate — wrong regional site fails closed, right one passes:")
    # SG seller: must land on ebay.com.sg, not global ebay.com.
    check("SG + ebay.com.sg passes", ok("ebay", "https://www.ebay.com.sg/itm/123", "SG"))
    check("SG + ebay.com fails", not ok("ebay", "https://www.ebay.com/itm/123", "SG"))
    check("US + ebay.com passes", ok("ebay", "https://www.ebay.com/itm/123", "US"))
    check("SG + carousell.sg passes", ok("carousell", "https://www.carousell.sg/p/x-1/", "SG"))
    check("SG + carousell.com.my fails",
          not ok("carousell", "https://www.carousell.com.my/p/x-1/", "SG"))
    # No region → legacy suffix behavior, region-blind (back-compat, keeps line 35 working).
    check("no region + ebay.com passes", ok("ebay", "https://www.ebay.com/itm/123"))
    check("no region + ebay.com.sg passes", ok("ebay", "https://www.ebay.com.sg/itm/123"))
    # Market with no domains map (craigslist) → region gate falls back to suffix, still passes.
    check("craigslist + region passes via suffix",
          ok("craigslist", "https://sfbay.craigslist.org/sfc/ele/d/x/1.html", "US"))


def test_carousell_ai_localhost():
    print("carousell-ai (MCP connector) localhost URL — host+path gate still applies:")
    good = "http://localhost:3001/listing/9f8c-abcd"
    check("localhost /listing/ passes (no region)", ok("carousell-ai", good))
    # region is passed by listing.md's loop; carousell-ai has no domains map, so the region gate
    # falls back to the "localhost" suffix match and still passes.
    check("localhost /listing/ passes with region", ok("carousell-ai", good, "SG"))
    check("wrong path fails", not ok("carousell-ai", "http://localhost:3001/p/9f8c"))
    check("empty id/url fails", not ok("carousell-ai", ""))
    check("wrong host fails", not ok("carousell-ai", "http://evil.example/listing/1"))


def test_cli_exit_codes():
    print("CLI exit codes:")
    good = v.main(["verify_listing_url.py", "--market", "fb",
                   "--url", "https://www.facebook.com/marketplace/item/1/"])
    bad = v.main(["verify_listing_url.py", "--market", "fb", "--url", "https://fb.com/x"])
    wrong_region = v.main(["verify_listing_url.py", "--market", "ebay",
                           "--url", "https://www.ebay.com/itm/1", "--region", "SG"])
    check("valid url → exit 0", good == 0)
    check("invalid url → exit 1", bad == 1)
    check("wrong-region url → exit 1", wrong_region == 1)


if __name__ == "__main__":
    print("verify_listing_url tests\n")
    test_real_permalinks_pass()
    test_fabricated_urls_fail()
    test_region_gate()
    test_carousell_ai_localhost()
    test_cli_exit_codes()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
