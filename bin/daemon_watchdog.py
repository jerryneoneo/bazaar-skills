#!/usr/bin/env python3
"""daemon_watchdog.py — an independent, read-only restart-on-stall watchdog (Fix D).

Runs on a launchd StartInterval (every 120s — NOT KeepAlive), separate from the agent it guards, so
it survives a wedged agent. It restarts the agent ONLY when launchd thinks the job is loaded but its
loop is actually dead: either the loop heartbeat (.daemon.heartbeat) went stale (a hung subprocess
froze the loop — the incident's ~7-min stall) or the instance-lock holder PID is no longer alive (a
crashed daemon that launchd hasn't noticed). The restart is a single atomic `launchctl kickstart -k`.

It does NO LLM, NO browser, NO secrets — it only reads two small files and asks launchctl whether the
job is loaded. Everything is fail-open and it ALWAYS exits 0, so a watchdog hiccup never disturbs the
agent. The pure should_restart() truth table is unit-tested; the side-effecting probes are thin
seams the tests monkeypatch.

Usage:
    daemon_watchdog.py            # one read-only pass; kickstart the agent iff it's loaded + stalled

Exit: always 0.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance_lock  # noqa: E402  PID liveness + lock-holder parse (shared with the daemon)
import healthcheck  # noqa: E402  reuse heartbeat_status (single source of truth for staleness)

AGENT_LABEL = "com.bazaarskills.agent"
RUNTIME_DIR = Path(__file__).resolve().parent.parent
HEARTBEAT_PATH = RUNTIME_DIR / ".daemon.heartbeat"
LOCK_PATH = RUNTIME_DIR / ".daemon.instancelock"
# Match the agent loop's normal cadence: ~15s idle / ~4s mid-pass. A gap past this means the loop is
# genuinely wedged, not merely busy. Reuse healthcheck's threshold so the two never disagree.
STALE_SEC = healthcheck.HEARTBEAT_STALE_SEC


def should_restart(loaded: bool, heartbeat_age, holder_alive: bool) -> bool:
    """PURE: restart the agent ONLY when it is LOADED under launchd AND it looks dead — its heartbeat
    is stale (age > STALE_SEC) OR its instance-lock holder is no longer alive.

    `heartbeat_age` is seconds since the last tick, or None when unknown (missing/unreadable
    heartbeat). An UNKNOWN age alone is NOT grounds to restart (we won't bounce a daemon we can't
    prove is wedged) — but if the holder is also dead, that IS a confident failure, so restart.
    When not loaded we never act: a not-loaded job is interactive mode or a deliberate stop."""
    if not loaded:
        return False
    if not holder_alive:
        return True
    if heartbeat_age is None:
        return False
    return heartbeat_age > STALE_SEC


def _agent_loaded() -> bool:
    """True if the agent LaunchAgent is loaded. Unqueryable/non-macOS → False (fail-open: do nothing)."""
    if not shutil.which("launchctl"):
        return False
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return AGENT_LABEL in out.stdout


def _heartbeat_age():
    """Seconds since the last loop tick, or None when the heartbeat is missing/unreadable. Reuses
    healthcheck.heartbeat_status so 'stale' here means exactly what it means in the health summary."""
    import time
    try:
        raw = HEARTBEAT_PATH.read_text()
    except OSError:
        raw = None
    _level, age = healthcheck.heartbeat_status(raw, time.time())
    return age


def _holder_alive() -> bool:
    """True if the instance-lock holder PID is alive. A missing/garbage lock → False (treated as a
    dead holder: with the job loaded, that's the crashed-daemon case the watchdog exists to heal)."""
    pid = instance_lock.read_holder_pid(LOCK_PATH)
    if pid is None:
        return False
    return instance_lock.is_pid_alive(pid)


def _kickstart() -> None:
    """Best-effort single atomic restart of the agent LaunchAgent. Fail-open: logged, never raised."""
    target = f"gui/{os.getuid()}/{AGENT_LABEL}"
    try:
        out = subprocess.run(["launchctl", "kickstart", "-k", target],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            logging.info("watchdog: restarted %s (was loaded but stalled)", target)
        else:
            logging.warning("watchdog: kickstart %s rc=%s: %s",
                            target, out.returncode, out.stderr.strip()[:120])
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("watchdog: kickstart failed (%s)", exc)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s watchdog %(message)s")
    loaded = _agent_loaded()
    if not loaded:
        return 0  # interactive mode or a deliberate stop — not ours to restart
    age = _heartbeat_age()
    alive = _holder_alive()
    if should_restart(loaded, age, alive):
        logging.info("watchdog: agent loaded but stalled (heartbeat_age=%s, holder_alive=%s) → restart",
                     "?" if age is None else f"{age:.0f}s", alive)
        _kickstart()
    return 0  # ALWAYS exit 0 — a watchdog must never become a problem itself


if __name__ == "__main__":
    sys.exit(main(sys.argv))
