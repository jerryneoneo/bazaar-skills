"""Claude Code harness — settings.local.json + .mcp.json + `claude -p`.

The one harness implemented end-to-end today. Other harnesses share the same interface (base.py)
and are added later; nothing here is special-cased by callers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .base import Harness, PassInvocation, PassSpec


class ClaudeCodeHarness(Harness):
    name = "claude-code"
    cli = "claude"

    def detect(self) -> dict:
        base = super().detect()
        if base["cli_present"]:
            # `claude -p` succeeds (rc=0) only when signed in (reuses this auth; no API key) — we
            # read ONLY the returncode, the "say ok" prompt text is irrelevant.
            # `--no-session-persistence` is MANDATORY for a probe: without it every sign-in check
            # spawns a real, on-disk session ("say ok" → "OK") that litters the `/resume` search
            # list with resumable stubs. With it, no transcript is written. (Do NOT add `--bare`:
            # it skips the login plumbing and makes the probe always report "Not logged in" → rc=1,
            # which would falsely fail the auth check.)
            try:
                proc = subprocess.run(
                    [self.cli, "--no-session-persistence", "-p", "say ok"],
                    capture_output=True, text=True, timeout=60)
                base["signed_in"] = proc.returncode == 0
                base["evidence"] = "signed in" if proc.returncode == 0 else "CLI present, not signed in"
            except (subprocess.SubprocessError, OSError):
                base["signed_in"] = False
                base["evidence"] = "CLI present, auth check failed"
        return base

    def _settings_path(self, dest: Path) -> Path:
        return dest / ".claude" / "settings.local.json"

    def _mcp_path(self, dest: Path) -> Path:
        return dest / ".mcp.json"

    # --- install/config layer -------------------------------------------------------------

    def write_settings(self, dest: Path, env: dict, allow: list[str]) -> dict:
        """Write env tokens + the autonomy allow-list, MERGING with any existing file.

        Existing env tokens are PRESERVED (so a later call without the token in the environment
        never wipes it — e.g. `./setup` re-running gen-settings to refresh the allow-list). The
        permissions allow-list is REPLACED with the given autonomy set (the level is its source of
        truth). Any other top-level / permission keys the user added are kept untouched.
        """
        path = self._settings_path(dest)
        existing = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                existing = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                existing = {}
        settings = dict(existing)
        settings["env"] = {**existing.get("env", {}), **env}
        perms = dict(existing.get("permissions") or {})
        perms["allow"] = list(allow)
        settings["permissions"] = perms
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(settings, indent=2) + "\n"
        json.loads(text)  # validate before write
        path.write_text(text)
        return {"harness": self.name, "path": str(path), "permissions": len(allow)}

    def write_mcp(self, dest: Path, servers: dict) -> dict:
        path = self._mcp_path(dest)
        text = json.dumps({"mcpServers": servers}, indent=2) + "\n"
        json.loads(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return {"harness": self.name, "path": str(path)}

    def config_files(self, dest: Path) -> list[Path]:
        return [self._settings_path(dest), self._mcp_path(dest)]

    def load_env(self, dest: Path) -> dict[str, str]:
        """Tokens live in settings.local.json's `env` block. {} if not written yet / unreadable."""
        path = self._settings_path(dest)
        if not path.exists():
            return {}
        try:
            return dict(json.loads(path.read_text()).get("env", {}))
        except (OSError, ValueError):
            return {}

    def skills_dir(self) -> Path:
        return Path.home() / ".claude" / "skills"

    # --- runtime layer --------------------------------------------------------------------

    def pass_argv(self, spec: PassSpec) -> PassInvocation:
        """Build the `claude -p` argv with full control over model/tools/permission/cache.
        `--allowedTools` greedily consumes following args, so it goes LAST."""
        argv: list[str] = [self.cli, "-p", spec.prompt]
        if spec.system_prompt_append:
            argv += ["--append-system-prompt", spec.system_prompt_append]
        if spec.strict_mcp:
            servers = spec.mcp_servers if spec.mcp_servers is not None else {}
            argv += ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers})]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.max_turns is not None:
            argv += ["--max-turns", str(spec.max_turns)]
        if spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]
        if spec.allowed_tools:
            argv += ["--allowedTools", *spec.allowed_tools]
        env = {"ENABLE_PROMPT_CACHING_1H": "1"} if spec.prompt_cache_1h else {}
        return PassInvocation(argv=argv, env=env)
