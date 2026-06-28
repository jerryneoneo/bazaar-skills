#!/usr/bin/env python3
"""healthcheck.py — is this Bazaar install actually runnable?

Where preflight.py checks that dependencies are *present* (a static, pre-onboarding check),
this checks that the install is *live*: the browser is reachable, you're onboarded, your
marketplaces are logged in, and (if you chose always-on) the daemon is loaded. It surfaces the
"silent" blockers that let an unattended pass start but quietly do nothing.

Read-only. Never installs, never writes, never prints a secret (floor / budget / token / address).
The data dir is relocatable via BAZAAR_DATA_DIR (tests + isolation), matching the rest of bin/.

Run:
  healthcheck.py            -> human summary + exit 0 unless a hard FAIL
  healthcheck.py --json     -> structured JSON
  healthcheck.py --quiet    -> no output; exit 0 unless a hard FAIL

Levels: fail (blocks runnability) · warn (a silent always-on blocker) · ok.
Exit: 0 no FAIL · 1 one or more FAIL · 3 platform unsupported.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight  # noqa: E402  (reuse the presence checks)
from platforms import UnsupportedPlatform  # noqa: E402

CDP_URL = "http://127.0.0.1:9222/json/version"
DAEMON_LABELS = ("com.bazaarskills.chrome", "com.bazaarskills.agent")
AGENT_LABEL = "com.bazaarskills.agent"
HEARTBEAT_PATH = Path(__file__).resolve().parent.parent / ".daemon.heartbeat"
# Normal cadence is ~15s idle / ~4s mid-pass (run_pass ticks the heartbeat), so a 300s gap means the
# loop is genuinely wedged (e.g. a hung subprocess), not merely busy in a long pass.
HEARTBEAT_STALE_SEC = 300

FAIL, WARN, OK = "fail", "warn", "ok"


def _data_dir() -> Path:
    """The data dir — relocatable via BAZAAR_DATA_DIR (used by tests for isolation)."""
    env = os.environ.get("BAZAAR_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "data"


def _check(name: str, level: str, detail: str, fix_hint: str = "") -> dict:
    return {"name": name, "level": level, "ok": level != FAIL, "detail": detail, "fix_hint": fix_hint}


def presence_checks(harness_name: str | None) -> list[dict]:
    """Reuse preflight's dependency presence/auth checks; a missing dependency is a hard FAIL."""
    out = []
    for c in preflight.run_checks(harness_name)["checks"]:
        out.append(_check(c["name"], OK if c["ok"] else FAIL, c["detail"], c.get("fix_hint", "")))
    return out


def cdp_check(url: str = CDP_URL) -> dict:
    """Is the warm Chrome reachable over CDP? WARN (not fail): the daemon starts it on its own."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # nosec B310 — fixed localhost URL
            reachable = resp.status == 200
    except Exception:
        reachable = False
    if reachable:
        return _check("chrome-cdp", OK, "Chrome reachable on 127.0.0.1:9222")
    return _check("chrome-cdp", WARN, "no CDP on 127.0.0.1:9222",
                  "start the warm browser: bin/chrome_debug.sh (the always-on daemon does this for you)")


def onboarded_check(data_dir: Path) -> dict:
    """seller_config.json is the onboarding gate; without it the agent has nothing to act on."""
    if (data_dir / "seller_config.json").exists():
        return _check("onboarded", OK, "seller_config.json present")
    return _check("onboarded", WARN, "not onboarded yet",
                  "run onboarding: ./setup (first run) or /bazaar-install")


def marketplace_checks(data_dir: Path) -> list[dict]:
    """For each ENABLED marketplace, WARN if it isn't confirmed-logged-in: a daemon driving an
    unauthenticated Chrome can't list or reply. Reads only non-secret status fields."""
    cfg_path = data_dir / "seller_config.json"
    if not cfg_path.exists():
        return []
    try:
        markets = (json.loads(cfg_path.read_text()).get("marketplaces") or {})
    except (OSError, ValueError):
        return [_check("marketplace-logins", WARN, "seller_config.json unreadable")]
    if not isinstance(markets, dict):
        return []
    enabled = {mid: m for mid, m in markets.items()
               if isinstance(m, dict) and m.get("enabled")}
    if not enabled:
        return [_check("marketplace-logins", WARN, "no marketplaces enabled",
                       "enable one via /bazaar -> marketplaces")]
    not_ready = sorted(mid for mid, m in enabled.items() if m.get("auth") != "confirmed")
    if not_ready:
        return [_check("marketplace-logins", WARN,
                       f"{len(not_ready)} of {len(enabled)} not logged in: {', '.join(not_ready)}",
                       "open each in the warm Chrome and sign in (/bazaar -> marketplaces)")]
    return [_check("marketplace-logins", OK, f"all {len(enabled)} enabled markets logged in")]


def _confirmed_enabled_markets(data_dir: Path) -> list[str]:
    """Markets marked enabled + auth:'confirmed' in seller_config (the set worth live-probing)."""
    try:
        markets = (json.loads((data_dir / "seller_config.json").read_text()).get("marketplaces") or {})
    except (OSError, ValueError):
        return []
    if not isinstance(markets, dict):
        return []
    return sorted(mid for mid, m in markets.items()
                  if isinstance(m, dict) and m.get("enabled") and m.get("auth") == "confirmed")


def login_liveness_status(probe_results: dict) -> list[str]:
    """PURE: markets a live probe reports as logged_out. logged_in/unknown are NOT flagged, so an
    ambiguous page or a market with no open tab never raises a false alarm."""
    return sorted(m for m, st in probe_results.items() if st == "logged_out")


def login_liveness_checks(data_dir: Path, cdp_reachable: bool) -> list[dict]:
    """Additive advisory probe over the warm Chrome: WARN only when a CONFIRMED market is *now*
    positively logged out (e.g. the session expired since onboarding). Stays silent (returns []) when
    CDP is down, nothing is confirmed, or the probe can't tell — so it only ever adds a true signal.
    Fail-open on any error (a broken probe must never make a healthy install look unrunnable)."""
    markets = _confirmed_enabled_markets(data_dir)
    if not markets or not cdp_reachable:
        return []
    try:
        import login_check  # local bin/ module — reuses buyer_peek's CDP transport
        results = {m: login_check.check_market(m).get("status", "unknown") for m in markets}
    except Exception:  # noqa: BLE001 — advisory only; never break the health check
        return []
    logged_out = login_liveness_status(results)
    if not logged_out:
        return []
    return [_check("marketplace-live-login", WARN,
                   f"{', '.join(logged_out)} look logged out now (was confirmed at setup)",
                   "re-log in in the warm Chrome (/bazaar -> marketplaces)")]


def permissions_check(harness_name: str | None) -> list[dict]:
    """WARN if the autonomous-run allow-list is incomplete or the PreToolUse safety hooks are
    missing — both are silent always-on blockers (the daemon's tools would prompt/deny mid-pass, or
    the pause/scope guards wouldn't fire). Reads only config (no secret values). Fail-open: any error
    -> no check, so a probe problem never makes a healthy install look broken."""
    try:
        from harnesses import get_harness
        import install  # REQUIRED_ALLOW is the single source of truth for the floor
        dest = Path(__file__).resolve().parent.parent
        res = get_harness(harness_name).verify_settings(dest, install.REQUIRED_ALLOW)
    except Exception:  # noqa: BLE001 — advisory; never break the health summary
        return []
    if not res.get("applicable", False):
        return []  # harness has no allow-list to audit (e.g. approval-mode runtime)
    out = []
    if res.get("missing"):
        out.append(_check("permissions", WARN,
                          f"{len(res['missing'])} autonomous-run tool rule(s) missing",
                          "re-run: python3 bin/install.py gen-settings --autonomy <level>"))
    if res.get("hooks_present") is False:
        out.append(_check("permission-hooks", WARN,
                          "PreToolUse safety hooks (pause/scope guard) not configured",
                          "restore .claude/settings.json from the repo"))
    if not out:
        out.append(_check("permissions", OK,
                          f"autonomous-run allow-list complete ({res['allow_count']} rules)"))
    return out


def _launchctl_loaded(label: str) -> bool | None:
    """True/False if launchctl can be queried, else None (can't tell)."""
    if not shutil.which("launchctl"):
        return None
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return label in out.stdout


def daemon_checks() -> list[dict]:
    """If the always-on daemon was installed (its plists exist in ~/Library/LaunchAgents), WARN when
    a job isn't actually loaded. No plists -> interactive mode, which is fine (OK)."""
    la = Path.home() / "Library" / "LaunchAgents"
    installed = [lbl for lbl in DAEMON_LABELS if (la / f"{lbl}.plist").exists()]
    if not installed:
        return [_check("daemon", OK,
                       "no always-on daemon installed (interactive mode — keep a /bazaar-run session open)")]
    results = []
    for lbl in installed:
        loaded = _launchctl_loaded(lbl)
        if loaded is None:
            results.append(_check(f"daemon:{lbl}", OK, "installed (could not query launchctl)"))
        elif loaded:
            results.append(_check(f"daemon:{lbl}", OK, "loaded"))
        else:
            results.append(_check(f"daemon:{lbl}", WARN, "installed but not loaded",
                                  "launchd/install_daemon.sh install"))
    return results


def heartbeat_status(raw: str | None, now: float, stale_sec: int = HEARTBEAT_STALE_SEC):
    """PURE: classify a heartbeat file's content. Returns (level, age_seconds|None).
    level: "ok" (ticking) | "stale" (loaded but loop wedged) | "missing" (no/invalid heartbeat)."""
    if raw is None:
        return ("missing", None)
    try:
        ts = float(json.loads(raw).get("ts"))
    except (ValueError, TypeError, AttributeError):
        return ("missing", None)
    age = now - ts
    return (("stale" if age > stale_sec else "ok"), age)


def heartbeat_check() -> list[dict]:
    """If the always-on AGENT daemon is LOADED, WARN when its loop heartbeat is missing or stale —
    launchd thinks the job is up but the loop isn't ticking (wedged on a hung subprocess). The
    "installed but not loaded" case is already covered by daemon_checks, so skip it here."""
    la = Path.home() / "Library" / "LaunchAgents"
    if not (la / f"{AGENT_LABEL}.plist").exists():
        return []  # interactive mode — no daemon loop expected
    if _launchctl_loaded(AGENT_LABEL) is not True:
        return []
    try:
        raw = HEARTBEAT_PATH.read_text()  # no exists() pre-check → no TOCTOU on a vanishing file
    except OSError:
        raw = None
    level, age = heartbeat_status(raw, time.time())
    if level == "ok":
        return [_check("daemon-heartbeat", OK, f"loop ticking ({int(age)}s ago)")]
    if level == "stale":
        return [_check("daemon-heartbeat", WARN,
                       f"daemon loaded but loop hasn't ticked in {int(age)}s (wedged?)",
                       "check logs/daemon.log; restart via launchd/install_daemon.sh install")]
    return [_check("daemon-heartbeat", WARN, "daemon loaded but no heartbeat yet",
                   "loop may be starting or wedged — check logs/daemon.log")]


def run_checks(harness_name: str | None = None) -> dict:
    data_dir = _data_dir()
    cdp = cdp_check()
    checks = [
        *presence_checks(harness_name),
        cdp,
        onboarded_check(data_dir),
        *marketplace_checks(data_dir),
        *login_liveness_checks(data_dir, cdp["level"] == OK),
        *permissions_check(harness_name),
        *daemon_checks(),
        *heartbeat_check(),
    ]
    return {
        "ok": all(c["level"] != FAIL for c in checks),
        "fails": [c["name"] for c in checks if c["level"] == FAIL],
        "warns": [c["name"] for c in checks if c["level"] == WARN],
        "checks": checks,
    }


def render(result: dict) -> str:
    icon = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}
    lines = ["Bazaar health check:"]
    for c in result["checks"]:
        line = f"  {icon[c['level']]} {c['name']}: {c['detail']}"
        if c["level"] != OK and c["fix_hint"]:
            line += f"\n       → {c['fix_hint']}"
        lines.append(line)
    if result["fails"]:
        lines.append(f"\nNOT RUNNABLE — fix: {', '.join(result['fails'])}")
    elif result["warns"]:
        lines.append(f"\nRunnable, with {len(result['warns'])} warning(s) to check before unattended use.")
    else:
        lines.append("\nAll good — ready to run.")
    return "\n".join(lines)


def main(argv) -> int:
    p = argparse.ArgumentParser(prog="healthcheck.py")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a human summary")
    p.add_argument("--quiet", action="store_true", help="no output; exit code only")
    p.add_argument("--harness", default="",
                   help="claude-code | codex (default: $BAZAAR_HARNESS or autodetect)")
    ns = p.parse_args(argv[1:])
    harness_name = ns.harness or os.environ.get("BAZAAR_HARNESS") or None
    try:
        result = run_checks(harness_name)
    except UnsupportedPlatform as exc:
        if not ns.quiet:
            print(json.dumps({"ok": False, "error": str(exc)}) if ns.json else f"unsupported platform: {exc}")
        return 3
    if not ns.quiet:
        print(json.dumps(result, indent=2) if ns.json else render(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
