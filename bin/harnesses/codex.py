"""Codex harness — .codex/config.toml + .codex/.env + `codex exec`.

STATUS: install/config layer is implemented; the runtime `pass_argv` is a **best-effort, UNVERIFIED
stub** kept here so the seam stays genuinely harness-agnostic and a future contributor has a starting
point. SELLY ships Claude-only today (see ARCHITECTURE.md §2 / README). The headless runner refuses
to run a non-claude-code harness until its `pass_argv` is verified.

Honest notes vs Claude Code:
  • Codex configures MCP servers in TOML under [mcp_servers.<name>] (we write .codex/config.toml).
  • Codex has no allow-list; autonomy maps to its approval/sandbox mode (APPROVAL_MODE below).
  • Codex has no `--append-system-prompt` and no prompt-caching — those are DROPPED; fold any
    always-needed skills into the prompt instead.
  • Secrets: we write a .codex/.env dotenv the runner can source.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Harness, PassInvocation, PassSpec

# Map the autonomy level to Codex's approval posture (applied by pass_argv / runner when wired up).
APPROVAL_MODE = {"hands-free": "full-auto", "balanced": "on-failure", "all-steps": "untrusted"}


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class CodexHarness(Harness):
    name = "codex"
    cli = "codex"

    def detect(self) -> dict:
        base = super().detect()
        if not base["cli_present"]:
            base["evidence"] = "codex not on PATH"
            return base
        # Best-effort sign-in probe: `codex login status` exits 0 when authenticated. If the
        # subcommand is missing (older CLI), fall back to presence-implies-signed-in.
        try:
            proc = subprocess.run([self.cli, "login", "status"], capture_output=True,
                                  text=True, timeout=20)
            if proc.returncode in (0, 1):  # recognized command -> trust its verdict
                base["signed_in"] = proc.returncode == 0
                base["evidence"] = "signed in" if proc.returncode == 0 else "CLI present, not signed in"
            else:
                base["evidence"] = "codex CLI present (sign-in check unavailable)"
        except (subprocess.SubprocessError, OSError):
            base["evidence"] = "codex CLI present (sign-in check unavailable)"
        return base

    def _env_path(self, dest: Path) -> Path:
        return dest / ".codex" / ".env"

    def _config_path(self, dest: Path) -> Path:
        return dest / ".codex" / "config.toml"

    # --- install/config layer -------------------------------------------------------------

    def write_settings(self, dest: Path, env: dict, allow: list[str]) -> dict:
        # Codex has no allow-list; persist env as a dotenv and note the approval posture the
        # autonomy level implies (derived from allow-list breadth: wide => hands-free => full-auto).
        path = self._env_path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in env.items()]
        path.write_text("\n".join(lines) + ("\n" if lines else ""))
        path.chmod(0o600)  # dotenv may hold secrets — owner-only, never world-readable
        return {"harness": self.name, "path": str(path),
                "note": "Codex uses approval modes, not an allow-list — set via headless flags",
                "approval_modes": APPROVAL_MODE}

    def write_mcp(self, dest: Path, servers: dict) -> dict:
        path = self._config_path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        blocks = ["# SELLY MCP servers (generated)\n"]
        for name, spec in servers.items():
            blocks.append(f"[mcp_servers.{name}]")
            blocks.append(f'command = "{_toml_escape(spec.get("command", ""))}"')
            args = ", ".join(f'"{_toml_escape(a)}"' for a in spec.get("args", []))
            blocks.append(f"args = [{args}]")
            blocks.append("")
        path.write_text("\n".join(blocks))
        return {"harness": self.name, "path": str(path)}

    def config_files(self, dest: Path) -> list[Path]:
        return [self._env_path(dest), self._config_path(dest)]

    def load_env(self, dest: Path) -> dict[str, str]:
        """Parse the .codex/.env dotenv back into a dict. {} if missing/unreadable."""
        path = self._env_path(dest)
        if not path.exists():
            return {}
        out: dict[str, str] = {}
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip()
        except OSError:
            return {}
        return out

    def skills_dir(self) -> Path:
        return Path.home() / ".codex" / "skills"

    # --- runtime layer (UNVERIFIED stub) --------------------------------------------------

    def pass_argv(self, spec: PassSpec) -> PassInvocation:
        """Best-effort, UNVERIFIED translation to `codex exec`. Drops --append-system-prompt and
        prompt-caching (no Codex equivalent) and ignores the Claude allow-list (Codex uses approval
        modes). NOT exercised by the shipped runner — present so the seam is real and a future
        contributor can finish + verify it."""
        argv: list[str] = [self.cli, "exec", spec.prompt]
        if spec.model:
            argv += ["-m", spec.model]
        # permission_mode acceptEdits → run without per-step prompts. Exact flag TBD on a real CLI.
        if spec.permission_mode == "acceptEdits":
            argv += ["--full-auto"]
        return PassInvocation(argv=argv, env={})
