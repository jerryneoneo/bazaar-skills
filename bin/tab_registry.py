#!/usr/bin/env python3
"""tab_registry.py — resolve a marketplace to the STABLE IDENTITY of "its" browser tab.

Under concurrency each per-marketplace worker drives ONE tab inside the shared warm Chrome.
A worker MUST select that tab by a stable property — the regional host / URL prefix — never
by a raw tab index. Indices renumber every time a tab opens or closes, so two workers racing
on indices would step on each other's tabs; the host is invariant for the life of the session.

This is a PURE RESOLVER. It tells a worker how to RECOGNISE its tab; it persists nothing and
mutates nothing. Given a marketplace (and a region, inferred from the seller/buyer config when
omitted), it returns the regional host, the canonical url_prefix to navigate to, and a
`match_hint` substring a tab picker can use to find the already-open tab for this marketplace.

Region → host resolution is NOT reimplemented here: it delegates to ``resolve_domain.resolve``
(the single source of truth that reads the ``domains`` map in ``data/marketplaces.json``), so the
two stay in lockstep.

Usage:
    python3 tab_registry.py resolve --market <id> [--region <r>] [--side sell|buy] [--config <path>]
    python3 tab_registry.py list   [--side sell|buy] [--config <path>]
    (tests relocate the data dir via SELLY_DATA_DIR for config isolation.)

Output (stdout, JSON). `resolve`:
    {"market": "carousell", "host": "www.carousell.sg",
     "url_prefix": "https://www.carousell.sg/", "match_hint": "carousell.sg", "region": "SG"}

`list` -> {"region": <r>, "side": <side>, "tabs": [<resolve payload>, ...]} for enabled markets.

Exit codes: 0 ok · 2 bad input · 3 data/config missing-or-invalid (incl. unknown market).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Reuse the region->host single source of truth instead of duplicating the domains lookup.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_domain  # noqa: E402

VALID_SIDES = ("sell", "buy")
DEFAULT_SIDE = "sell"
# The --side flag is sell|buy; the config files on disk are seller_/buyer_config.json.
SIDE_CONFIG_FILE = {"sell": "seller_config.json", "buy": "buyer_config.json"}
HTTPS_PREFIX = "https://"


def data_dir() -> Path:
    """The data directory — relocatable via SELLY_DATA_DIR (used by tests for config isolation)."""
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


# ---------------------------------------------------------------------------
# pure helpers (no IO) — directly unit-tested
# ---------------------------------------------------------------------------
def url_prefix_for_host(host: str) -> str:
    """The canonical https URL prefix a worker navigates to for a host.

    A worker matches its tab on the prefix; we always normalise to a trailing slash so the
    prefix is a clean path boundary (``https://www.carousell.sg/`` not ``...sg``)."""
    return f"{HTTPS_PREFIX}{host.rstrip('/')}/"


def match_hint_for_host(host: str) -> str:
    """The stable substring a tab picker searches open tab URLs for.

    Drop a leading ``www.`` so the hint matches both the bare and ``www`` forms of the regional
    host (the warm tab may have been opened either way), while staying specific enough to not
    collide across marketplaces."""
    bare = host.strip().lower()
    if bare.startswith("www."):
        bare = bare[len("www."):]
    return bare


def build_tab_identity(market: str, host: str, region: str | None) -> dict:
    """Return a NEW dict describing how a worker recognises its tab. Never mutates inputs."""
    return {
        "market": market,
        "host": host,
        "url_prefix": url_prefix_for_host(host),
        "match_hint": match_hint_for_host(host),
        "region": region,
    }


def enabled_markets(marketplaces) -> list[str]:
    """Ordered list of enabled market ids from a config's ``marketplaces`` selection.

    Tolerates the legacy ARRAY shape (all listed are enabled) and the object shape
    ``{id: {enabled: bool}}`` (mirrors scan_state.enabled_markets)."""
    if isinstance(marketplaces, list):
        return list(marketplaces)
    if isinstance(marketplaces, dict):
        return [mid for mid, sel in marketplaces.items()
                if isinstance(sel, dict) and sel.get("enabled")]
    return []


# ---------------------------------------------------------------------------
# config IO
# ---------------------------------------------------------------------------
def _config_path_for_side(side: str) -> Path:
    return data_dir() / SIDE_CONFIG_FILE[side]


def load_config(side: str, config_override: str | None) -> dict:
    """Read the seller/buyer config for region + enabled-market inference.

    An explicit --config path wins; otherwise the per-side default under the data dir.
    Missing/invalid is a data error (exit 3) — region inference has nothing to stand on."""
    path = Path(config_override) if config_override else _config_path_for_side(side)
    if not path.exists():
        raise FileNotFoundError(f"config not found at {path}")
    text = path.read_text().strip()
    if not text:
        raise ValueError(f"config at {path} is empty")
    config = json.loads(text)
    if not isinstance(config, dict):
        raise ValueError(f"config at {path} must be a JSON object")
    return config


def resolve_region(config: dict, region_override: str | None) -> str | None:
    """Region override wins; otherwise infer from the config's ``region`` field."""
    if region_override:
        return region_override
    region = config.get("region")
    return region.strip() if isinstance(region, str) and region.strip() else None


def resolve_tab(market: str, region: str | None) -> dict:
    """Resolve a single market to its stable tab identity. Raises KeyError if unresolvable.

    Delegates the region->host decision to resolve_domain (the SSOT). A market with no
    resolvable host (unknown id, or a stub with no domains/listing_url) is a hard error so a
    worker never silently drives the wrong tab."""
    host = resolve_domain.resolve(market, region)
    if not host:
        raise KeyError(f"no resolvable host for market '{market}' (region {region!r})")
    return build_tab_identity(market, host, region)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def run_resolve(market: str, region_override: str | None, side: str, config_override: str | None) -> dict:
    config = load_config(side, config_override)
    region = resolve_region(config, region_override)
    return resolve_tab(market, region)


def run_list(region_override: str | None, side: str, config_override: str | None) -> dict:
    config = load_config(side, config_override)
    region = resolve_region(config, region_override)
    tabs = [resolve_tab(mid, region) for mid in enabled_markets(config.get("marketplaces"))]
    return {"region": region, "side": side, "tabs": tabs}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="tab_registry.py", add_help=False)
    parser.add_argument("command", choices=["resolve", "list"])
    parser.add_argument("--market", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--side", default=DEFAULT_SIDE)
    parser.add_argument("--config", default="")
    return parser.parse_args(argv[1:])


def main(argv):
    try:
        ns = _parse_args(argv)
        side = ns.side.strip() or DEFAULT_SIDE
        if side not in VALID_SIDES:
            raise ValueError(f"--side must be one of {VALID_SIDES}, got {side!r}")
        market = ns.market.strip()
        if ns.command == "resolve" and not market:
            raise ValueError("resolve requires --market <id>")
        region_override = ns.region.strip() or None
        config_override = ns.config.strip() or None
    except (ValueError, SystemExit) as exc:
        print(json.dumps({"error": str(exc) or "bad input"}), file=sys.stderr)
        return 2
    try:
        if ns.command == "resolve":
            result = run_resolve(market, region_override, side, config_override)
        else:
            result = run_list(region_override, side, config_override)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
