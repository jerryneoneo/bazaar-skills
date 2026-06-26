"""macOS platform — launchd supervisor + TCC-aware runtime dir.

This is the only place launchd/TCC knowledge lives. The runtime must sit outside the
TCC-protected ~/Documents, ~/Desktop, ~/Downloads (SETUP.md §3), so we use ~/bazaar-skills.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Platform

# Directories TCC blocks launchd-spawned processes from reading.
TCC_BLOCKED = ("Documents", "Desktop", "Downloads")


class MacOSPlatform(Platform):
    name = "macos"

    def runtime_dir(self) -> Path:
        return Path.home() / "bazaar-skills"

    def is_tcc_blocked(self, path: Path) -> bool:
        try:
            rel = path.expanduser().resolve().relative_to(Path.home())
        except ValueError:
            return False
        return len(rel.parts) > 0 and rel.parts[0] in TCC_BLOCKED

    def supervisor_kind(self) -> str:
        return "launchd"

    def tcc_fix_hint(self) -> str:
        return ("Grant Full Disk Access to the host app (Terminal / Claude Code) in "
                "System Settings > Privacy & Security > Full Disk Access — required for the "
                "launchd daemon to read files and (for iMessage) chat.db.")

    def install_supervisor(self, dry_run: bool = True) -> dict:
        """Drive launchd/install_daemon.sh. With dry_run, report intent only."""
        repo = Path(__file__).resolve().parent.parent.parent
        script = repo / "launchd" / "install_daemon.sh"
        plan = {
            "supervisor": "launchd",
            "script": str(script),
            "jobs": ["com.bazaarskills.chrome", "com.bazaarskills.agent"],
            "path_hints": self.path_hints(),
            "dry_run": dry_run,
        }
        if dry_run:
            plan["status"] = "planned"
            return plan
        if not script.exists():
            plan["status"] = "error"
            plan["error"] = f"missing {script}"
            return plan
        proc = subprocess.run([str(script), "install"], capture_output=True, text=True,
                              cwd=str(repo))
        plan["status"] = "installed" if proc.returncode == 0 else "error"
        if proc.returncode != 0:
            plan["error"] = proc.stderr.strip()
        return plan
