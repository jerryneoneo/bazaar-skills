#!/usr/bin/env python3
"""onboarding_notice.py — SessionStart hook: self-heal an unfinished Bazaar setup.

Fires when a Claude Code session starts in the Bazaar runtime dir. If Bazaar is installed but
onboarding never completed (no data/seller_config.json yet), it injects a one-line note telling the
agent to offer to finish setup by following .claude/commands/bazaar-install.md.

This covers the case where `./setup` was run from inside an agent / non-TTY shell: there, the
first-run handoff can't exec an attachable interactive session, so onboarding may never have run.
Once data/seller_config.json exists the note stops on its own (no snooze needed).

NO-OP for the daemon's headless `claude -p` passes — they set BAZAAR_DAEMON_PASS=1 (harness_run.py),
can't act on an interactive prompt, and must never have every pass nagged.

Output contract (Claude Code SessionStart): JSON on stdout with
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<note>"}}
which is prepended to the session context. FAIL-OPEN: any error prints nothing and exits 0 — a hook
must never wedge or delay a session.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# This file lives at <root>/bin/hooks/onboarding_notice.py -> parents[2] is the runtime dir.
ROOT = Path(__file__).resolve().parents[2]


def _data_dir() -> Path:
    """The data dir — relocatable via BAZAAR_DATA_DIR (used by tests for isolation), matching bin/."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else ROOT / "data"


def main() -> int:
    # Never run inside the daemon's headless passes (they can't act on it and would see it every pass).
    if os.environ.get("BAZAAR_DAEMON_PASS"):
        return 0
    try:
        sys.stdin.read()  # consume the hook event JSON (we don't need its fields)
    except (OSError, ValueError):
        pass
    try:
        # seller_config.json is the onboarding gate (same signal ./setup uses); present -> done.
        if (_data_dir() / "seller_config.json").exists():
            return 0
    except OSError:
        return 0  # fail-open: never wedge a session
    note = ("Bazaar is installed here but onboarding isn't finished yet (no data/seller_config.json). "
            "Proactively tell the user in one short line and offer to finish setup now by following "
            ".claude/commands/bazaar-install.md (the /bazaar-install runbook).")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
