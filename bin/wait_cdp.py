#!/usr/bin/env python3
"""wait_cdp.py — block until the warm Chrome's CDP endpoint is actually serving (or time out).

Onboarding launches `chrome_debug.sh &` then fires a single `curl` at :9222 — but Chrome needs a
moment to bring the debug endpoint up, so the one-shot check races it and a slow/failed Chrome only
surfaces later when a marketplace navigation fails. This polls `/json/version` until it answers 200
(Chrome is ready) or the timeout elapses, turning a silent race into an explicit, fast signal.

Usage:
    wait_cdp.py [--timeout 30] [--interval 0.5] [--port 9222]
        -> {"ready": bool, "waited_sec": float, "attempts": int, "browser": str|null}

Exit: 0 ready · 3 timed out (Chrome never came up).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def probe_cdp(url: str, timeout_sec: float = 2.0) -> dict | None:
    """Return the parsed /json/version body if CDP answers 200, else None (any failure)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:  # nosec B310 — localhost
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — not-up-yet is the normal case while we poll
        return None
    return None


def wait_for_cdp(url: str, timeout: float = 30.0, interval: float = 0.5, *,
                 probe=probe_cdp, sleep=time.sleep, monotonic=time.monotonic) -> dict:
    """Poll `url` until it answers or `timeout` seconds pass. Clock/probe/sleep are injectable so the
    loop is unit-testable without a real Chrome or wall-clock waits."""
    start = monotonic()
    attempts = 0
    while True:
        attempts += 1
        info = probe(url)
        if info is not None:
            return {"ready": True, "waited_sec": round(monotonic() - start, 2),
                    "attempts": attempts, "browser": info.get("Browser")}
        if monotonic() - start >= timeout:
            return {"ready": False, "waited_sec": round(monotonic() - start, 2),
                    "attempts": attempts, "browser": None}
        sleep(interval)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="wait_cdp.py")
    p.add_argument("--timeout", type=float, default=30.0, help="max seconds to wait (default 30)")
    p.add_argument("--interval", type=float, default=0.5, help="seconds between probes (default 0.5)")
    p.add_argument("--port", type=int, default=int(os.environ.get("CHROME_DEBUG_PORT", "9222")))
    ns = p.parse_args(argv[1:])
    url = f"http://127.0.0.1:{ns.port}/json/version"
    result = wait_for_cdp(url, ns.timeout, ns.interval)
    print(json.dumps(result))
    return 0 if result["ready"] else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
