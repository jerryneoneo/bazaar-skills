#!/usr/bin/env python3
"""Tests for bin/install.py — harness-aware config generation, validity, and token safety.

    python3 tests/test_install.py

Generates settings + MCP config for BOTH harnesses (claude-code, codex) into a temp dir, validates
they parse, confirms the token lands in the file but is never echoed to stdout, and checks the
macOS TCC guard on runtime-dir. Uses a temp dir, never the real runtime.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "bin" / "install.py"

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def run(args, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, str(INSTALL), *args], capture_output=True, text=True, env=e)


def test_harness_detect():
    print("harness detect:")
    proc = run(["harness"])
    check("exits 0", proc.returncode == 0)
    names = {h["name"] for h in json.loads(proc.stdout)["harnesses"]}
    check("knows claude-code + codex", {"claude-code", "codex"} <= names)


def test_harness_named_check():
    print("harness --name (sign-in gate used by the shell installers):")
    for name in ("claude-code", "codex"):
        proc = run(["harness", "--name", name])
        payload = json.loads(proc.stdout)
        check(f"{name}: reports the harness name", payload.get("name") == name)
        check(f"{name}: signed_in is a bool", isinstance(payload.get("signed_in"), bool))
        # The exit code is the gate: 0 iff signed in, else 3.
        expected = 0 if payload["signed_in"] else 3
        check(f"{name}: exit code matches signed_in ({expected})", proc.returncode == expected)
    bogus = run(["harness", "--name", "bogus"])
    check("unknown harness exits 3", bogus.returncode == 3)
    check("unknown harness reports an error", "error" in json.loads(bogus.stderr))


def test_gen_and_validate_per_harness():
    for harness, settings_rel in (("claude-code", ".claude/settings.local.json"),
                                  ("codex", ".codex/.env")):
        print(f"gen + validate ({harness}):")
        with tempfile.TemporaryDirectory() as d:
            env = {"TELEGRAM_BOT_TOKEN": "SECRETTOK123"}
            gs = run(["gen-settings", "--dest", d, "--harness", harness, "--autonomy", "hands-free"],
                     env=env)
            check("gen-settings exits 0", gs.returncode == 0)
            check("token NOT echoed to stdout", "SECRETTOK123" not in gs.stdout)
            settings_file = Path(d) / settings_rel
            check("settings file written", settings_file.exists())
            check("token IS in the file", "SECRETTOK123" in settings_file.read_text())
            gm = run(["gen-mcp", "--dest", d, "--harness", harness])
            check("gen-mcp exits 0", gm.returncode == 0)
            val = run(["validate", "--dest", d, "--harness", harness])
            check("validate exits 0", val.returncode == 0)
            check("validate reports ok", json.loads(val.stdout)["ok"] is True)


def test_runtime_dir_tcc_guard():
    print("runtime-dir TCC guard (macOS):")
    if sys.platform != "darwin":
        check("skipped (not macOS)", True)
        return
    safe = run(["runtime-dir", "--dest", str(Path.home() / "bazaar-skills")])
    check("safe dir not blocked", json.loads(safe.stdout)["tcc_blocked"] is False)
    blocked = run(["runtime-dir", "--dest", str(Path.home() / "Documents" / "x")])
    check("Documents path flagged blocked", json.loads(blocked.stdout)["tcc_blocked"] is True)
    check("blocked exits 3", blocked.returncode == 3)


def test_verify_settings():
    print("verify_settings audits the effective allow-list + hooks (claude-code):")
    sys.path.insert(0, str(ROOT / "bin"))
    import install  # REQUIRED_ALLOW + the harness registry
    from harnesses import get_harness
    harness = get_harness("claude-code")
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d)
        # Fresh dest, nothing written yet -> the required rules are all missing.
        res0 = harness.verify_settings(dest, install.REQUIRED_ALLOW)
        check("applicable for claude-code", res0["applicable"] is True)
        check("nothing written -> not ok", res0["ok"] is False)
        check("reports the full required set missing", len(res0["missing"]) == len(install.REQUIRED_ALLOW))
        check("no hooks in a bare dest", res0["hooks_present"] is False)

        # Generate a full hands-free settings.local.json -> required floor satisfied.
        run(["gen-settings", "--dest", d, "--harness", "claude-code", "--autonomy", "hands-free"],
            env={"TELEGRAM_BOT_TOKEN": "SECRETTOK123"})
        res1 = harness.verify_settings(dest, install.REQUIRED_ALLOW)
        check("after gen-settings -> ok", res1["ok"] is True and res1["missing"] == [])

        # Drop a REQUIRED rule from the file -> caught as missing (pop a known-required entry, not a
        # level-specific DATA rule that isn't part of the floor).
        dropped = install.REQUIRED_ALLOW[0]
        settings = json.loads((dest / ".claude" / "settings.local.json").read_text())
        settings["permissions"]["allow"] = [a for a in settings["permissions"]["allow"] if a != dropped]
        (dest / ".claude" / "settings.local.json").write_text(json.dumps(settings))
        res2 = harness.verify_settings(dest, install.REQUIRED_ALLOW)
        check("a removed required rule is detected", dropped in res2["missing"] and res2["ok"] is False)

    # validate surfaces the permissions audit (exit 3 when the floor isn't met).
    with tempfile.TemporaryDirectory() as d:
        run(["gen-mcp", "--dest", d, "--harness", "claude-code"])
        # settings.local.json with an EMPTY allow-list -> JSON-valid but permissions incomplete.
        sp = Path(d) / ".claude" / "settings.local.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"permissions": {"allow": []}}))
        val = run(["validate", "--dest", d, "--harness", "claude-code"])
        out = json.loads(val.stdout)
        check("validate flags incomplete permissions", out["files"].get("permissions.allow", "").startswith("missing"))
        check("validate exits 3 on incomplete permissions", val.returncode == 3)


def test_verify_settings_codex_noop():
    print("verify_settings is a no-op for harnesses without an allow-list (codex):")
    sys.path.insert(0, str(ROOT / "bin"))
    import install
    from harnesses import get_harness
    with tempfile.TemporaryDirectory() as d:
        res = get_harness("codex").verify_settings(Path(d), install.REQUIRED_ALLOW)
        check("codex marks it not applicable", res["applicable"] is False)
        check("codex reports ok (nothing to fail)", res["ok"] is True)


def test_autonomy_levels_differ():
    print("autonomy level changes the allow-list size (claude-code):")
    with tempfile.TemporaryDirectory() as d:
        hf = run(["gen-settings", "--dest", d, "--harness", "claude-code", "--autonomy", "hands-free"])
        n_hf = json.loads(hf.stdout)["permissions"]
        allsteps = run(["gen-settings", "--dest", d, "--harness", "claude-code",
                        "--autonomy", "all-steps"])
        n_as = json.loads(allsteps.stdout)["permissions"]
        check("hands-free grants at least as many tools as all-steps", n_hf >= n_as)


if __name__ == "__main__":
    print("install.py tests\n")
    test_harness_detect()
    test_harness_named_check()
    test_gen_and_validate_per_harness()
    test_verify_settings()
    test_verify_settings_codex_noop()
    test_runtime_dir_tcc_guard()
    test_autonomy_levels_differ()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
