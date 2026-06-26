"""Platform interface — the OS-abstraction boundary for the installer.

Concrete platforms (macos.py, later windows.py) implement this. Callers (preflight.py,
install.py, bazaar-install.md) depend only on this interface, never on launchd/TCC directly.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class UnsupportedPlatform(Exception):
    """Raised by get_platform() when the host OS has no implementation yet."""


class Platform(ABC):
    """OS-specific operations the installer needs. Keep this surface small."""

    name: str = "base"

    @abstractmethod
    def runtime_dir(self) -> Path:
        """The live runtime directory (must be outside any privacy-restricted location)."""

    @abstractmethod
    def supervisor_kind(self) -> str:
        """A label for the always-on supervisor (e.g. 'launchd', 'task-scheduler')."""

    @abstractmethod
    def tcc_fix_hint(self) -> str:
        """Actionable hint for granting the file-access permission the daemon needs (or '' if N/A)."""

    @abstractmethod
    def install_supervisor(self, dry_run: bool = True) -> dict:
        """Generate + (optionally) load the always-on supervisor jobs. Returns a status dict.
        With dry_run=True, only reports what it would do (no system changes)."""

    def path_hints(self) -> dict:
        """Where the executables the supervisor needs live (for a minimal-PATH supervisor).
        OS-agnostic default: resolve via PATH; subclasses may add OS-specific dirs."""
        hints = {}
        for exe in ("claude", "npx", "node", "python3"):
            resolved = shutil.which(exe)
            hints[exe] = str(Path(resolved).parent) if resolved else None
        return hints
