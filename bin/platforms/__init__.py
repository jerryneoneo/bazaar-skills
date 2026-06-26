"""Platform abstraction for the installer.

`get_platform()` returns the right concrete Platform for the host OS. All OS-specific knowledge
(runtime dir, executable path hints, supervisor install, TCC) lives behind this boundary so that
no launchd/TCC string leaks into the channel/marketplace flow specs or shims.

macOS uses launchd; Windows uses Task Scheduler. Both implement the same Platform interface, so
callers never branch on OS.
"""

from __future__ import annotations

import sys

from .base import Platform, UnsupportedPlatform


def get_platform() -> Platform:
    if sys.platform == "darwin":
        from .macos import MacOSPlatform
        return MacOSPlatform()
    if sys.platform == "win32":
        from .windows import WindowsPlatform
        return WindowsPlatform()
    raise UnsupportedPlatform(
        f"{sys.platform!r} is not supported (macOS = launchd, Windows = Task Scheduler)"
    )


__all__ = ["Platform", "UnsupportedPlatform", "get_platform"]
