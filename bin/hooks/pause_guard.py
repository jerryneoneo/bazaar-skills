#!/usr/bin/env python3
"""pause_guard.py — PreToolUse hook: a PAUSED agent physically cannot send.

This is the deterministic backstop for the pause feature (Layer 4). The daemon's mid-flight
interrupt stops a running pass within ~one poll cadence, but during that small window — and inside
an interactive /sell or /buy session, where there is no daemon to interrupt — a tool call could
still fire. This hook closes that gap at the harness level, independent of LLM compliance: while
data/control.json says paused, it DENIES every marketplace MUTATION tool.

Read-only tools (snapshot, screenshot, Read, Grep, navigate-less reads) are NOT matched by the
settings.json hook config, so a paused pass can still finish reading/logging — it just can't act.
The single highest-leverage match is the pacing reserve: no paced marketplace send happens without
a preceding `pacing_gate.py reserve`, so denying that one Bash command blocks new sends regardless
of which browser primitive ultimately submits.

Claude Code calls this with the PreToolUse event JSON on stdin. We emit, on stdout:
  • when paused + a mutating tool  → a "deny" permission decision (Claude surfaces it as blocked)
  • otherwise                       → nothing (exit 0 = allow, no opinion)

Fail-OPEN: any error (unreadable control.json, bad stdin) allows the tool. A hook must never wedge
the agent — the daemon interrupt + the loop guard remain the primary stop; this only adds safety.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import control
except ImportError:  # if the module can't load, fail open (allow)
    control = None

# Substring that identifies the one Bash command we must block while paused. Denying the pacing
# reserve is enough to stop any *new* paced send (it's the documented single authority) — even a
# send issued via browser_evaluate/run_code_unsafe must reserve first, so this is the catch-all.
RESERVE_MARKER = "pacing_gate.py reserve"

# Browser MUTATION tools to deny while paused. Listed explicitly (not "any browser_*") so read-only
# tools (snapshot, take_screenshot, wait_for, tabs, hover) stay allowed even if the settings.json
# matcher is later broadened — a paused pass can keep reading/logging, just not acting.
MUTATING_BROWSER_TOOLS = frozenset({
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_file_upload",
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_handle_dialog",
})


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    if control is None or not control.is_paused():
        return 0  # not paused (or can't tell) → allow, no opinion
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return 0  # unparseable event → fail open
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}

    # A Bash pacing-reserve is the highest-leverage block: deny it and no new paced send proceeds.
    if tool == "Bash":
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if RESERVE_MARKER in command:
            _deny("SELLY is paused — refusing to reserve a send slot until /resume.")
        return 0

    # Browser MUTATION tools (explicit set) are denied; read-only browser tools fall through.
    if tool in MUTATING_BROWSER_TOOLS:
        _deny("SELLY is paused — refusing to act on a marketplace until /resume.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
