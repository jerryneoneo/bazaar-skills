#!/usr/bin/env python3
"""Tests for the harness seam (Harness.pass_argv) + harness_run spec building. Plain python:

    python3 tests/test_harness_argv.py

Focus invariants:
  (1) the claude-code adapter builds the right argv for seller / buyer / intent passes,
  (2) NO secret (the bot token) ever appears in a pass argv or env value,
  (3) the codex stub drops Claude-only flags (no --append-system-prompt / prompt cache),
  (4) the runner refuses an unverified harness rather than launching a broken pass.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import harness_run  # noqa: E402
from harnesses import get_harness  # noqa: E402
from harnesses.base import PassSpec  # noqa: E402

_failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures.append(name)
    print(f"  [{status}] {name}")


def claude_argv(mode, msg=""):
    spec = harness_run.build_spec(mode, msg)
    return get_harness("claude-code").pass_argv(spec)


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def test_seller_argv():
    print("claude-code seller pass:")
    inv = claude_argv("seller")
    a = inv.argv
    check("starts with `claude -p`", a[:2] == ["claude", "-p"])
    check("permission-mode acceptEdits", _flag_value(a, "--permission-mode") == "acceptEdits")
    check("has --append-system-prompt (core skills cached)", "--append-system-prompt" in a)
    check("prompt cache 1h env set", inv.env.get("ENABLE_PROMPT_CACHING_1H") == "1")
    check("has --allowedTools", "--allowedTools" in a)
    # --allowedTools greedily consumes following args, so nothing after it may start with '--'.
    tail = a[a.index("--allowedTools") + 1:]
    check("--allowedTools is the LAST flag", all(not t.startswith("--") for t in tail))
    check("includes deterministic + browser tools",
          "Bash(python3:*)" in a and "mcp__playwright__browser_file_upload" in a)


def test_buyer_argv():
    print("claude-code buyer pass:")
    a = claude_argv("buyer").argv
    prompt = a[2] if len(a) > 2 else ""
    check("model sonnet", _flag_value(a, "--model") == "sonnet")
    check("max-turns 40", _flag_value(a, "--max-turns") == "40")
    check("includes browser_tabs (multi-inbox navigation)", "mcp__playwright__browser_tabs" in a)
    check("smaller browser set (no run_code_unsafe)", "mcp__playwright__browser_run_code_unsafe" not in a)
    check("smaller browser set (no take_screenshot)", "mcp__playwright__browser_take_screenshot" not in a)
    # The fix for the 82%-rc=1 turn-exhaustion: a self-governor so the pass can't run to the cap.
    check("has a TURN BUDGET governor", "TURN BUDGET" in prompt)
    check("bounds discovery to the peek-hinted market", "Handle ONLY the\nmarketplace" in prompt
          or "Handle ONLY the marketplace" in " ".join(prompt.split()))
    check("never loops on a stuck step (one retry then escalate)",
          "ONE retry" in prompt and "NEVER loop" in prompt)


def test_intent_argv():
    print("claude-code intent line:")
    inv = claude_argv("intent", "what are my listings?")
    a = inv.argv
    check("model haiku", _flag_value(a, "--model") == "haiku")
    check("max-turns 1", _flag_value(a, "--max-turns") == "1")
    check("strict MCP (no browser)", "--strict-mcp-config" in a and "--mcp-config" in a)
    check("no allowed-tools loop", "--allowedTools" not in a)
    check("no permission-mode", "--permission-mode" not in a)
    check("no prompt-cache env", inv.env == {})


def test_channel_argv():
    print("channel pass (both sides):")
    a = claude_argv("channel").argv
    prompt = a[2] if len(a) > 2 else ""
    check("`seller` is an alias of `channel`", claude_argv("seller").argv == a)
    check("reads all three sessions", "buy_session.json" in prompt and "distribution_session.json" in prompt)
    check("routes a buy answer (writes the budget)", "budgets/<want_id>.json" in prompt)
    check("full seller browser set", "mcp__playwright__browser_file_upload" in a)


def test_buy_argv():
    print("buy pass (acquire a want):")
    a = claude_argv("buy").argv
    prompt = a[2] if len(a) > 2 else ""
    check("model sonnet", _flag_value(a, "--model") == "sonnet")
    check("max-turns 28", _flag_value(a, "--max-turns") == "28")
    check("buyer browser set incl tabs", "mcp__playwright__browser_tabs" in a)
    check("smaller set (no run_code_unsafe)", "mcp__playwright__browser_run_code_unsafe" not in a)
    check("drives search + liaison", "search.md" in prompt and "liaison-pipeline.md" in prompt)
    check("max budget stays secret-only", "budgets/<want_id>.json" in prompt)


def test_maint_argv():
    print("maint pass (cross-listing §2b):")
    a = claude_argv("maint").argv
    prompt = a[2] if len(a) > 2 else ""
    # Tier 1d: maint is mechanical cross-listing, so it is right-sized to sonnet by default
    # (was the strong DEFAULT model). Revertible via BAZAAR_MAINT_MODEL (see below).
    check("right-sized to sonnet by default", _flag_value(a, "--model") == "sonnet")
    check("full seller browser set", "mcp__playwright__browser_file_upload" in a)
    check("scan cadence + distribution drain", "inbox_detect.py due" in prompt and "distribution_session" in prompt)
    check("never interrupts a listing", "listing_session.json" in prompt)


def test_maint_model_env_override():
    print("maint model override via BAZAAR_MAINT_MODEL:")
    prev = os.environ.get("BAZAAR_MAINT_MODEL")
    try:
        os.environ["BAZAAR_MAINT_MODEL"] = "opus"
        check("env overrides the maint model", _flag_value(claude_argv("maint").argv, "--model") == "opus")
        os.environ["BAZAAR_MAINT_MODEL"] = ""  # empty reverts to the strong DEFAULT (no --model flag)
        check("empty value reverts to strong default", "--model" not in claude_argv("maint").argv)
    finally:
        if prev is None:
            os.environ.pop("BAZAAR_MAINT_MODEL", None)
        else:
            os.environ["BAZAAR_MAINT_MODEL"] = prev


def test_resource_scoping():
    print("Phase-3 --resource scoping (per-marketplace worker):")
    scoped = harness_run.build_spec("buyer", resource="carousell")
    unscoped = harness_run.build_spec("buyer")
    check("scoped prompt names the one marketplace", "ONLY the marketplace 'carousell'" in scoped.prompt)
    check("scoped prompt pins the tab via tab_registry",
          "tab_registry.py resolve --market carousell" in scoped.prompt)
    check("unscoped prompt is byte-identical to legacy (no SCOPE)", "SCOPE —" not in unscoped.prompt)
    check("buy + maint also scope", "SCOPE —" in harness_run.build_spec("buy", resource="fb").prompt
          and "SCOPE —" in harness_run.build_spec("maint", resource="ebay").prompt)
    check("channel pass ignores resource (not market-scoped)",
          "SCOPE —" not in harness_run.build_spec("channel", resource="carousell").prompt)
    # cached skills prefix must stay byte-stable regardless of resource (scope lives in -p prompt only)
    check("cached system prompt unchanged by resource",
          scoped.system_prompt_append == unscoped.system_prompt_append)


def test_daemon_pass_env_marker():
    print("daemon-pass env marker (so the SessionStart update hook no-ops in headless passes):")
    _argv, env = harness_run._invocation(get_harness("claude-code"), harness_run.build_spec("seller"))
    check("BAZAAR_DAEMON_PASS=1 set on every headless pass", env.get("BAZAAR_DAEMON_PASS") == "1")


def test_no_secret_in_argv():
    print("secrecy — token never in argv/env:")
    token = get_harness("claude-code").load_env(ROOT).get("TELEGRAM_BOT_TOKEN")
    if not token:
        check("no token configured → trivially safe (skipped)", True)
        return
    leaked = False
    for mode in ("channel", "seller", "buyer", "buy", "maint", "intent"):
        inv = claude_argv(mode, "msg")
        if any(token in str(part) for part in inv.argv):
            leaked = True
        if any(token in v for v in inv.env.values()):
            leaked = True
    check("bot token absent from every pass argv + env", not leaked)


def test_eval_argv():
    print("eval pass (offline LLM judge):")
    a = claude_argv("eval", "JUDGE THESE RECORDS").argv
    check("model sonnet", _flag_value(a, "--model") == "sonnet")
    check("single max-turns 1", _flag_value(a, "--max-turns") == "1")
    check("strict MCP (no browser/tools)", "--strict-mcp-config" in a)
    check("no allowed-tools loop", "--allowedTools" not in a)
    check("no playwright tools at all", not any("mcp__playwright" in str(p) for p in a))
    check("judge prompt carried", any("JUDGE THESE RECORDS" in str(p) for p in a))


def test_codex_stub():
    print("codex stub drops Claude-only flags:")
    inv = get_harness("codex").pass_argv(
        PassSpec(prompt="x", model="sonnet", permission_mode="acceptEdits",
                 system_prompt_append="SKILLS", prompt_cache_1h=True,
                 allowed_tools=("Bash(python3:*)",)))
    a = inv.argv
    check("uses `codex exec`", a[:2] == ["codex", "exec"])
    check("maps model via -m", "-m" in a and a[a.index("-m") + 1] == "sonnet")
    check("no --append-system-prompt (no Codex equivalent)", "--append-system-prompt" not in a)
    check("no prompt-cache env (no Codex equivalent)", "ENABLE_PROMPT_CACHING_1H" not in inv.env)


def test_runner_refuses_unverified_harness():
    print("runner refuses unverified runtime:")
    prev = os.environ.get("BAZAAR_HARNESS")
    os.environ["BAZAAR_HARNESS"] = "codex"
    refused = False
    try:
        harness_run._resolve_harness()
    except SystemExit as exc:
        refused = exc.code == 3
    finally:
        if prev is None:
            os.environ.pop("BAZAAR_HARNESS", None)
        else:
            os.environ["BAZAAR_HARNESS"] = prev
    check("codex runtime refused with exit 3", refused)


if __name__ == "__main__":
    test_seller_argv()
    test_channel_argv()
    test_buyer_argv()
    test_buy_argv()
    test_maint_argv()
    test_maint_model_env_override()
    test_intent_argv()
    test_eval_argv()
    test_resource_scoping()
    test_daemon_pass_env_marker()
    test_no_secret_in_argv()
    test_codex_stub()
    test_runner_refuses_unverified_harness()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("All harness argv tests passed.")
