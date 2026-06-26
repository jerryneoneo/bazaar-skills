"""Windows platform — always-on via Task Scheduler (the launchd analogue).

Scope: "Mac now, Windows designed-for." The interactive flow works today; this module gives Windows
a REAL always-on story (Task Scheduler `ONLOGON` jobs for the daemon + warm Chrome) behind the same
Platform interface, so callers (install.py, preflight.py, bazaar-install.md) are unchanged.

Remaining Windows-specific work, flagged honestly (not silently missing):
  • bin/agent_daemon.py shells out to `run_pass.sh` (bash). Windows needs a `run_pass.ps1` (or Git
    Bash) wrapper — install_supervisor() reports this in `notes` until it exists.
  • Channels supported today are Telegram + console; iMessage + WhatsApp land later.
Windows has no TCC, so there is no privacy-folder restriction on the runtime dir.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import Platform

DAEMON_TASK = "BazaarSkillsAgent"
CHROME_TASK = "BazaarSkillsChrome"


class WindowsPlatform(Platform):
    name = "windows"

    def runtime_dir(self) -> Path:
        return Path.home() / "bazaar-skills"  # no TCC on Windows; home root is fine

    def is_tcc_blocked(self, path: Path) -> bool:
        return False  # Windows has no equivalent privacy gate on these folders

    def supervisor_kind(self) -> str:
        return "task-scheduler"

    def tcc_fix_hint(self) -> str:
        return ""  # not applicable on Windows

    def install_supervisor(self, dry_run: bool = True) -> dict:
        repo = Path(__file__).resolve().parent.parent.parent
        daemon = repo / "bin" / "agent_daemon.py"
        runner = repo / "bin" / "run_pass.ps1"
        pyw = self.path_hints().get("python3") or "python"
        # Two ONLOGON tasks: keep the daemon alive, and keep warm Chrome alive (CDP on :9222).
        commands = [
            ["schtasks", "/Create", "/TN", DAEMON_TASK, "/SC", "ONLOGON", "/RL", "LIMITED",
             "/TR", f'"{pyw}" "{daemon}"', "/F"],
            ["schtasks", "/Create", "/TN", CHROME_TASK, "/SC", "ONLOGON", "/RL", "LIMITED",
             "/TR", f'powershell -File "{repo / "bin" / "chrome_debug.ps1"}"', "/F"],
        ]
        notes = []
        if not runner.exists():
            notes.append("run_pass.ps1 not present yet — the daemon's pass-runner needs a Windows "
                         "wrapper before always-on is fully functional (interactive /sell-run works now).")
        plan = {
            "supervisor": "task-scheduler",
            "tasks": [DAEMON_TASK, CHROME_TASK],
            "commands": [" ".join(c) for c in commands],
            "path_hints": self.path_hints(),
            "notes": notes,
            "dry_run": dry_run,
        }
        if dry_run:
            plan["status"] = "planned"
            return plan
        if sys.platform != "win32":
            plan["status"] = "error"
            plan["error"] = "schtasks is only available on Windows"
            return plan
        errors = []
        for cmd in commands:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                errors.append(proc.stderr.strip())
        plan["status"] = "installed" if not errors else "error"
        if errors:
            plan["error"] = "; ".join(errors)
        return plan
