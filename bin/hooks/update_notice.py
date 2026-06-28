#!/usr/bin/env python3
"""update_notice.py — SessionStart hook: surface "a Bazaar update is available" to the user.

Fires when a Claude Code session starts in the Bazaar runtime dir. It runs the throttled, read-only
update_check and, if a newer Bazaar is available upstream (and not already dismissed for this
version), injects a one-line note telling the agent to offer /bazaar-upgrade. This is the interactive
half of the auto-update-check; the always-on daemon has its own channel notice (agent_daemon.py).

NO-OP for the daemon's headless `claude -p` passes — they set BAZAAR_DAEMON_PASS=1 (harness_run.py),
can't act on an interactive prompt, and must never have every pass nagged. Hooks get the event JSON
on stdin (so stdin/stdout are pipes in BOTH modes — tty detection can't tell them apart; the env
marker is the reliable signal).

Output contract (Claude Code SessionStart): JSON on stdout with
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<note>"}}
which is prepended to the session context. FAIL-OPEN: any error prints nothing and exits 0 — a hook
must never wedge or delay a session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent


def main() -> int:
    # Never run inside the daemon's headless passes (they can't act on it and would see it every pass).
    if os.environ.get("BAZAAR_DAEMON_PASS"):
        return 0
    try:
        sys.stdin.read()  # consume the hook event JSON (we don't need its fields)
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run([sys.executable, str(BIN / "update_check.py"), "check"],
                             capture_output=True, text=True, timeout=20)
        info = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0  # fail-open: no note
    if not info.get("should_prompt"):
        return 0
    summary = info.get("summary") or f"v{info.get('current', '?')} -> v{info.get('latest', '?')}"
    note = (f"A Bazaar update is available ({summary}). Proactively tell the user in one short line "
            f"and offer to run /bazaar-upgrade now. If they decline, run "
            f"`python3 bin/update_check.py snooze` so it stops asking about this version.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
