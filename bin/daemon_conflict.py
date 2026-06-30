#!/usr/bin/env python3
"""daemon_conflict.py — warn an interactive run when the always-on daemon would fight it.

Telegram (and WhatsApp) are SINGLE-CONSUMER: getUpdates with an offset ACKs older updates, so a
loaded daemon AND a hand-run /sell-run / /buy-run / /selly-run polling the same bot steal each
other's messages. This is the cheap, read-only pre-check the interactive loop runs at session start
to catch that before it silently drops the seller's messages.

A conflict exists iff: the agent daemon is loaded (launchctl) AND the bound channel is a
single-consumer remote adapter (telegram / whatsapp). `console` never conflicts (the daemon doesn't
consume it). On non-macOS, or when launchctl can't be queried, it reports no conflict (fail-open).

    daemon_conflict.py  -> {"conflict": bool, "daemon_loaded": bool, "channel": str, "reason": str}

Exit: 0 no conflict · 1 conflict.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

AGENT_LABEL = "com.selly.agent"
# Remote channels with a single shared cursor/offset — two consumers steal each other's updates.
SINGLE_CONSUMER = {"telegram", "whatsapp"}


def _data_dir() -> Path:
    env = os.environ.get("SELLY_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data"


def agent_loaded() -> bool:
    """True if the agent LaunchAgent is loaded. Unknown/unqueryable -> False (fail-open)."""
    if not shutil.which("launchctl"):
        return False
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return AGENT_LABEL in out.stdout


def bound_channel() -> str:
    """The bound channel adapter from seller_config (then buyer_config). '' if neither is set."""
    for name in ("seller_config.json", "buyer_config.json"):
        try:
            cfg = json.loads((_data_dir() / name).read_text())
        except (OSError, ValueError):
            continue
        adapter = (cfg.get("channel") or {}).get("adapter")
        if adapter:
            return adapter
    return ""


def assess(loaded: bool, channel: str) -> dict:
    """PURE: a conflict iff the daemon is loaded AND the channel is single-consumer."""
    conflict = bool(loaded and channel in SINGLE_CONSUMER)
    if conflict:
        reason = (f"the always-on daemon is loaded and already consumes the {channel} channel. "
                  "Running an interactive session too will make you both miss messages. Stop the "
                  "daemon first (launchd/install_daemon.sh uninstall), or just let the daemon run it.")
    elif loaded:
        reason = (f"daemon loaded but channel '{channel or 'console'}' is not single-consumer, "
                  "so no conflict.")
    else:
        reason = "no always-on daemon loaded, safe to run interactively."
    return {"conflict": conflict, "daemon_loaded": loaded, "channel": channel, "reason": reason}


def main(argv: list[str]) -> int:
    result = assess(agent_loaded(), bound_channel())
    print(json.dumps(result))
    return 1 if result["conflict"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
