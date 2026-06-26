#!/usr/bin/env python3
"""preflight.py — read-only dependency checks for the Bazaar installer.

Automates SETUP.md §2 (and the §11 friction inventory's preflight row). Emits structured JSON so
bazaar-install.md can present each check and its fix hint. NEVER changes anything (no installs,
no writes) — it only inspects.

Run:
  preflight.py            -> {"ok": bool, "platform": str, "checks": [{name, ok, detail, fix_hint}]}
  preflight.py --quiet    -> same JSON; exit 0 if all ok, 1 if any check fails

Exit: 0 all checks pass · 1 one or more failed · 3 platform unsupported.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import UnsupportedPlatform, get_platform  # noqa: E402  (local bin/platforms package)
from harnesses import UnknownHarness, get_harness  # noqa: E402  (local bin/harnesses package)


def _check_exe(name: str, why: str) -> dict:
    path = shutil.which(name)
    return {
        "name": name,
        "ok": path is not None,
        "detail": path or "not found on PATH",
        "fix_hint": "" if path else f"install {name} ({why})",
    }


def _check_chrome() -> dict:
    app = Path("/Applications/Google Chrome.app")
    ok = app.exists()
    return {
        "name": "google-chrome",
        "ok": ok,
        "detail": str(app) if ok else "not installed",
        "fix_hint": "" if ok else "install Google Chrome (the real browser the agent drives)",
    }


def _check_harness(name: str | None) -> list[dict]:
    """CLI-present + signed-in checks for the SELECTED harness (not hardcoded claude). A signed-in
    Codex user passes here too. Passes reuse the harness's own auth — no API key."""
    try:
        harness = get_harness(name)
    except UnknownHarness as exc:
        return [{"name": "harness", "ok": False, "detail": str(exc)[:120],
                 "fix_hint": "install + sign in to Claude Code (or another supported harness)"}]
    d = harness.detect()
    h = harness.name
    return [
        {"name": f"{h}-cli", "ok": d["cli_present"],
         "detail": d.get("evidence", ""),
         "fix_hint": "" if d["cli_present"] else f"install the {h} CLI"},
        {"name": f"{h}-auth", "ok": d["signed_in"],
         "detail": d.get("evidence", ""),
         "fix_hint": "" if d["signed_in"] else f"sign in to {h} (run it once and complete login)"},
    ]


def run_checks(harness_name: str | None = None) -> dict:
    plat = get_platform()
    checks = [
        _check_exe("python3", "all bin/*.py"),
        _check_exe("node", "Playwright MCP browser tool"),
        _check_exe("npx", "Playwright MCP browser tool"),
        _check_chrome(),
        *_check_harness(harness_name),
    ]
    return {
        "ok": all(c["ok"] for c in checks),
        "platform": plat.name,
        "runtime_dir": str(plat.runtime_dir()),
        "path_hints": plat.path_hints(),
        "checks": checks,
    }


def main(argv) -> int:
    p = argparse.ArgumentParser(prog="preflight.py")
    p.add_argument("--quiet", action="store_true", help="JSON only; exit 1 on any failure")
    p.add_argument("--harness", default="", help="claude-code | codex (default: $BAZAAR_HARNESS or autodetect)")
    ns = p.parse_args(argv[1:])
    harness_name = ns.harness or os.environ.get("BAZAAR_HARNESS") or None
    try:
        result = run_checks(harness_name)
    except UnsupportedPlatform as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 3
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
