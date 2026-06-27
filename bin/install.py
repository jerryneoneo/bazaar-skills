#!/usr/bin/env python3
"""install.py — the stateful half of the Bazaar installer (the only thing here that writes).

Automates the file-generation rows of SETUP.md §11 so bazaar-install.md never hand-edits JSON
(the "missing comma" gotcha). Tokens are read from the ENVIRONMENT, never passed on argv (which
would leak them in the process list) and never echoed back.

Subcommands (all accept --dest <runtime dir>; defaults to the platform runtime dir):
  runtime-dir                 -> {"runtime_dir": PATH, "tcc_blocked": bool}
  gen-settings                -> write .claude/settings.local.json (env tokens + Playwright perms),
                                 validate JSON. Reports which token env vars were found (names only).
  gen-mcp                     -> write .mcp.json (Playwright CDP config), validate JSON.
  supervisor [--no-dry-run]   -> generate/load the always-on supervisor via the platform module.
  validate                    -> re-parse the generated JSON files; report ok/errors.

Exit: 0 ok · 2 bad input · 3 operational error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import UnsupportedPlatform, get_platform  # noqa: E402  (local bin/platforms package)
from harnesses import UnknownHarness, detect_all, get_harness  # noqa: E402  (local bin/harnesses)

# Playwright MCP tools the agent needs (SETUP.md §5). Interactive perms; the daemon passes the
# same set via run_pass.sh --allowedTools.
PLAYWRIGHT_TOOLS = [
    "mcp__playwright__browser_navigate", "mcp__playwright__browser_click",
    "mcp__playwright__browser_type", "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_file_upload", "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_take_screenshot", "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_wait_for", "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_tabs", "mcp__playwright__browser_evaluate",
    "mcp__playwright__browser_run_code_unsafe",
]
# Token env vars install.py will copy into settings.local.json if present (by adapter).
TOKEN_ENV_KEYS = ["TELEGRAM_BOT_TOKEN", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID"]

# Harness-permission layer of autonomy: for an agent to run unattended (list, search, check chats)
# without a prompt on every step, the harness allow-list must cover the tools each task uses. This
# is distinct from config.approvals (the BUSINESS gates). hands-free grants the broadest safe set;
# balanced grants what the normal loop needs; all-steps grants the minimum (the seller is present).
BIN_TOOLS = [
    "Bash(python3 bin/*.py:*)", "Bash(bin/*.sh:*)",
    "Bash(curl http://127.0.0.1:9222/*:*)",
]
DATA_TOOLS = ["Read(data/**)", "Write(data/**)", "Edit(data/**)"]
AUTONOMY_ALLOW = {
    "hands-free": PLAYWRIGHT_TOOLS + BIN_TOOLS + DATA_TOOLS,
    "balanced":   PLAYWRIGHT_TOOLS + BIN_TOOLS + DATA_TOOLS,
    "all-steps":  PLAYWRIGHT_TOOLS + BIN_TOOLS,  # seller is present to approve writes
}

# Pin the Playwright MCP to an EXACT version (not the floating `@latest` dist-tag). `@latest` makes
# npx hit the npm registry to resolve the tag on EVERY browser pass cold start (1 to 4s each); an
# exact version is served straight from the npx cache with no network. Bump this constant
# deliberately so a bad upstream release can never silently break every pass. Keep .mcp.json (the
# runtime file the harness actually launches) in lockstep with this value.
PLAYWRIGHT_MCP_VERSION = "0.0.76"

MCP_CONFIG = {
    "mcpServers": {
        "playwright": {
            "command": "npx",
            "args": ["-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
                     "--cdp-endpoint", "http://127.0.0.1:9222"],
        }
    }
}

# Marks a global launcher WE generated, so cleanup only ever removes Bazaar's own launchers and
# never touches a sibling skill (gstack, etc.) sharing the same skills dir.
LAUNCHER_MARKER = "<!-- bazaar-skills launcher (generated) -->"


def resolve_dest(ns, plat) -> Path:
    return Path(ns.dest).expanduser() if ns.dest else plat.runtime_dir()


def _write_json(path: Path, payload: dict) -> None:
    """Write pretty JSON and immediately re-parse to guarantee validity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    json.loads(text)  # fail before writing if somehow invalid
    path.write_text(text)


def cmd_runtime_dir(ns, plat) -> int:
    dest = resolve_dest(ns, plat)
    blocked = getattr(plat, "is_tcc_blocked", lambda p: False)(dest)
    print(json.dumps({"runtime_dir": str(dest), "tcc_blocked": blocked,
                      "supervisor": plat.supervisor_kind()}))
    return 0 if not blocked else 3


def cmd_harness(ns, plat) -> int:
    """Report which agent harnesses are present / signed in (the install-time selection step).

    Without --name: list every known harness (Stage 1 builds its menu from this).
    With --name <claude-code|codex>: probe just that one and exit-code the sign-in gate
    (0 = signed in, 3 = not signed in) so the shell installers can loop until it's resolved.
    """
    if ns.name:
        detected = get_harness(ns.name).detect()
        print(json.dumps(detected))
        return 0 if detected.get("signed_in") else 3
    print(json.dumps({"harnesses": detect_all()}, indent=2))
    return 0


def cmd_gen_settings(ns, plat) -> int:
    dest = resolve_dest(ns, plat)
    harness = get_harness(ns.harness or None)
    env_block = {k: os.environ[k] for k in TOKEN_ENV_KEYS if os.environ.get(k)}
    allow = AUTONOMY_ALLOW.get(ns.autonomy, AUTONOMY_ALLOW["balanced"])
    result = harness.write_settings(dest, env_block, allow)
    # Report only the NAMES of the tokens written, never the values.
    result.update({"autonomy": ns.autonomy, "tokens_written": sorted(env_block.keys())})
    print(json.dumps(result))
    return 0


def cmd_gen_mcp(ns, plat) -> int:
    dest = resolve_dest(ns, plat)
    harness = get_harness(ns.harness or None)
    print(json.dumps(harness.write_mcp(dest, MCP_CONFIG["mcpServers"])))
    return 0


def _command_files(dest: Path) -> list[Path]:
    """User-facing slash-command specs in the runtime dir (the things we expose globally)."""
    cmd_dir = dest / ".claude" / "commands"
    return sorted(cmd_dir.glob("*.md")) if cmd_dir.is_dir() else []


def _command_description(path: Path) -> str:
    """Pull the front-matter `description:` from a command file (first --- block). Fallback generic."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return f"Bazaar /{path.stem} command"
    if not lines or lines[0].strip() != "---":
        return f"Bazaar /{path.stem} command"
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return f"Bazaar /{path.stem} command"


def _launcher_body(name: str, description: str, abs_dest: str) -> str:
    """A thin global launcher: cd into the Bazaar runtime dir, then follow the real command spec.
    Self-contained — works via the ~/.bazaar/home pointer OR the embedded absolute path fallback."""
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n{LAUNCHER_MARKER}\n\n"
        f"You invoked the Bazaar **/{name}** command. Bazaar runs from its own runtime directory; "
        f"everything it needs (`data/`, `skills/`, `bin/`, `.claude/commands/`) is relative to that "
        f"dir, so you must work from there.\n\n"
        f"1. Find the Bazaar home: read `~/.bazaar/home` if it exists, else use `{abs_dest}`.\n"
        f"2. `cd` into that directory.\n"
        f"3. Then follow `.claude/commands/{name}.md` from there and do exactly what it says.\n"
    )


def _is_bazaar_launcher(skill_md: Path) -> bool:
    try:
        return LAUNCHER_MARKER in skill_md.read_text()
    except OSError:
        return False


def _remove_launchers(skills_dir: Path, keep: set[str]) -> list[str]:
    """Remove every Bazaar-generated launcher in skills_dir whose name isn't in `keep`. Marker-gated,
    so a sibling skill (gstack, etc.) is never touched. Returns the names removed."""
    removed: list[str] = []
    if not skills_dir.is_dir():
        return removed
    for child in sorted(skills_dir.iterdir()):
        if child.is_dir() and child.name not in keep and _is_bazaar_launcher(child / "SKILL.md"):
            for p in sorted(child.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
            child.rmdir()
            removed.append(child.name)
    return removed


def cmd_gen_launchers(ns, plat) -> int:
    """Install thin global launchers for every Bazaar command into the harness's global skills dir,
    so /bazaar, /sell, /buy, … work from any project. Idempotent: regenerates the current set and
    removes stale Bazaar launchers (e.g. when the name prefix flips or a command is removed)."""
    dest = resolve_dest(ns, plat).resolve()
    harness = get_harness(ns.harness or None)
    skills_dir = harness.skills_dir()
    skills_dir.mkdir(parents=True, exist_ok=True)
    prefix = "bazaar-" if ns.prefix else ""

    installed: list[str] = []
    for cmd in _command_files(dest):
        name = prefix + cmd.stem
        body = _launcher_body(name, _command_description(cmd), str(dest))
        target = skills_dir / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(body)
        installed.append(name)

    # Cleanup: drop any Bazaar-generated launcher no longer in the current set (marker-gated, so we
    # never remove a non-Bazaar skill sharing this dir).
    removed = _remove_launchers(skills_dir, keep=set(installed))

    print(json.dumps({"harness": harness.name, "skills_dir": str(skills_dir),
                      "prefix": prefix or None, "installed": installed, "removed": removed}))
    return 0


def cmd_rm_launchers(ns, plat) -> int:
    """Remove Bazaar's global launchers (uninstall). --all sweeps every known harness's skills dir
    so launchers installed to multiple hosts are all cleaned up."""
    names = [d["name"] for d in detect_all()] if ns.all else [ns.harness or None]
    out = {}
    for name in names:
        harness = get_harness(name)
        out[harness.name] = _remove_launchers(harness.skills_dir(), keep=set())
    print(json.dumps({"removed": out}))
    return 0


def cmd_supervisor(ns, plat) -> int:
    result = plat.install_supervisor(dry_run=not ns.no_dry_run)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("planned", "installed") else 3


def validate_style_file(path: Path) -> str:
    """Validate a data/style.json file against the style schema. Returns 'ok' or 'invalid: …'.

    Pure (takes an explicit path) so it is unit-testable without a platform/harness. Used by
    cmd_validate only when the file is present (a fresh temp dest has none — that's not an error)."""
    try:
        obj = json.loads(path.read_text())
    except ValueError as exc:
        return f"invalid: {exc}"
    import style  # local bin/ module — single source of truth for the schema
    errors = style.validate_style(obj)
    return "ok" if not errors else "invalid: " + "; ".join(errors)


def cmd_validate(ns, plat) -> int:
    dest = resolve_dest(ns, plat)
    harness = get_harness(ns.harness or None)
    results = {}
    for path in harness.config_files(dest):
        rel = str(path.relative_to(dest)) if dest in path.parents else str(path)
        if not path.exists():
            results[rel] = "missing"
        elif path.suffix == ".json":
            try:
                json.loads(path.read_text())
                results[rel] = "ok"
            except ValueError as exc:
                results[rel] = f"invalid: {exc}"
        else:
            results[rel] = "ok" if path.read_text() is not None else "empty"
    # The user style/persona profile, when present (committed default; absent in a bare temp dest).
    style_path = dest / "data" / "style.json"
    if style_path.exists():
        results["data/style.json"] = validate_style_file(style_path)
    ok = all(v == "ok" for v in results.values())
    print(json.dumps({"ok": ok, "harness": harness.name, "files": results}))
    return 0 if ok else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="install.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    hp = sub.add_parser("harness")  # detect-only; no dest needed
    hp.add_argument("--name", default="", help="probe one harness (claude-code|codex); "
                    "exit 0 if signed in, 3 if not")
    hp.set_defaults(func=cmd_harness)
    sp = sub.add_parser("runtime-dir")
    sp.add_argument("--dest", default="")
    sp.set_defaults(func=cmd_runtime_dir)
    # Harness-aware file generation + validation: each takes --dest and --harness.
    for name, func in (("gen-settings", cmd_gen_settings), ("gen-mcp", cmd_gen_mcp),
                       ("validate", cmd_validate)):
        sp = sub.add_parser(name)
        sp.add_argument("--dest", default="")
        sp.add_argument("--harness", default="", help="claude-code | codex (default: autodetect)")
        if name == "gen-settings":
            sp.add_argument("--autonomy", default="balanced",
                            choices=["hands-free", "balanced", "all-steps"])
        sp.set_defaults(func=func)
    gl = sub.add_parser("gen-launchers", help="install global slash-command launchers into the "
                        "harness skills dir so /bazaar, /sell, /buy work from any project")
    gl.add_argument("--dest", default="")
    gl.add_argument("--harness", default="", help="claude-code | codex (default: autodetect)")
    gl.add_argument("--prefix", action="store_true", help="prefix launcher names with 'bazaar-'")
    gl.set_defaults(func=cmd_gen_launchers)
    rl = sub.add_parser("rm-launchers", help="remove Bazaar's global launchers (uninstall)")
    rl.add_argument("--harness", default="", help="claude-code | codex (default: autodetect)")
    rl.add_argument("--all", action="store_true", help="sweep every known harness's skills dir")
    rl.set_defaults(func=cmd_rm_launchers)
    sup = sub.add_parser("supervisor")
    sup.add_argument("--dest", default="")
    sup.add_argument("--no-dry-run", action="store_true", help="actually load the supervisor")
    sup.set_defaults(func=cmd_supervisor)
    return p


def main(argv) -> int:
    try:
        ns = build_parser().parse_args(argv[1:])
    except SystemExit:
        return 2
    try:
        plat = get_platform()
    except UnsupportedPlatform as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    try:
        return ns.func(ns, plat)
    except UnknownHarness as exc:
        print(json.dumps({"error": str(exc), "hint": "pass --harness claude-code|codex, or sign "
                          "into one"}), file=sys.stderr)
        return 3
    except OSError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
