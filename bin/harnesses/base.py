"""Harness interface — the agent-runtime abstraction boundary.

Bazaar is harness-agnostic *by design*: it runs under Claude Code today, and other harnesses
(Codex, Cursor, OpenCode, …) slot in later behind this same interface. The pieces that differ
between harnesses live here:
  • where secrets/permissions live + the MCP config format  (write_settings / write_mcp / load_env)
  • the global dir where slash-command launchers are installed (skills_dir)
  • how to invoke ONE non-interactive pass with full control over model / tools / permissions /
    caching — the `pass_argv` seam.

Callers (install.py, harness_run.py, agent_daemon.py, preflight.py) depend only on this interface,
never on a specific harness. Adding a harness = one new subclass; no caller changes.

This is the agent-runtime analogue of bin/platforms/ (which abstracts the OS).
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class UnknownHarness(Exception):
    """Raised when no harness can be detected and none was specified."""


@dataclass(frozen=True)
class PassSpec:
    """A harness-agnostic description of one headless pass. Each harness translates it to its
    own CLI argv via `pass_argv`. Immutable — callers build a fresh spec per pass."""

    prompt: str
    model: str | None = None                 # "sonnet" | "haiku" | None (harness default)
    max_turns: int | None = None
    allowed_tools: tuple[str, ...] = ()       # e.g. "Bash(python3:*)", "mcp__playwright__browser_navigate"
    permission_mode: str | None = None        # "acceptEdits" → run unattended without per-tool prompts
    system_prompt_append: str | None = None    # static skills folded into the cached prefix
    mcp_servers: dict | None = None            # None → harness/project default; {} with strict → no MCP
    strict_mcp: bool = False                   # pass only `mcp_servers` (used by the MCP-less intent line)
    prompt_cache_1h: bool = False              # bump the static-prefix cache TTL where supported


@dataclass(frozen=True)
class PassInvocation:
    """The concrete command to run a pass: argv + extra env vars to merge into the environment."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


class Harness(ABC):
    """Agent-runtime-specific operations the installer + runner need. Keep this surface small."""

    name: str = "base"
    cli: str = ""  # the CLI binary used to detect presence / run headless

    def detect(self) -> dict:
        """Cheap probe: is this harness's CLI present (and, best-effort, signed in)?
        Returns {name, cli_present, signed_in, evidence}. Subclasses refine signed_in."""
        present = shutil.which(self.cli) is not None
        return {"name": self.name, "cli_present": present,
                "signed_in": present, "evidence": self.cli if present else "CLI not on PATH"}

    # --- install/config layer -------------------------------------------------------------

    @abstractmethod
    def write_settings(self, dest: Path, env: dict, allow: list[str]) -> dict:
        """Persist secrets (env) + the permission allow-list in this harness's format.
        Must validate what it writes. Returns a status dict (never echoes secret values)."""

    @abstractmethod
    def write_mcp(self, dest: Path, servers: dict) -> dict:
        """Persist MCP server config in this harness's format. Returns a status dict."""

    @abstractmethod
    def config_files(self, dest: Path) -> list[Path]:
        """The files write_settings/write_mcp produce — used by `install.py validate`."""

    @abstractmethod
    def load_env(self, dest: Path) -> dict[str, str]:
        """Read back the secret env this harness persisted (so the daemon can find the token
        without knowing the harness's storage format). Returns {} if nothing is stored yet."""

    def verify_settings(self, dest: Path, required_allow: list[str]) -> dict:
        """Read-only audit: are the autonomous-run essentials actually granted in this harness's
        config? Default: not applicable (e.g. an approval-mode runtime has no allow-list to check) ->
        ok. Claude Code overrides this with a real allow-list + safety-hook check. Never reads a
        secret value. Returns {harness, ok, applicable, missing, hooks_present, allow_count}."""
        return {"harness": self.name, "ok": True, "applicable": False,
                "missing": [], "hooks_present": None, "allow_count": None}

    @abstractmethod
    def skills_dir(self) -> Path:
        """The harness's GLOBAL dir where slash-command launchers are installed, so the commands
        work from any project (e.g. ~/.claude/skills). gstack's per-host install model."""

    # --- runtime layer --------------------------------------------------------------------

    @abstractmethod
    def pass_argv(self, spec: PassSpec) -> PassInvocation:
        """Translate a harness-agnostic PassSpec into this harness's concrete argv + env.
        The single seam the headless runner goes through — no caller hardcodes a CLI."""
