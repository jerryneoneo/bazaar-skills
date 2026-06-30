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
    # Fix C: the HARD backstop is raised 40 -> 50 (a cushion ABOVE the soft budget; not a workload
    # bump — raising the cap alone failed ~82%). Real governance is the soft budget below.
    check("max-turns 50 (hard backstop above the soft budget)", _flag_value(a, "--max-turns") == "50")
    check("includes browser_tabs (multi-inbox navigation)", "mcp__playwright__browser_tabs" in a)
    check("smaller browser set (no run_code_unsafe)", "mcp__playwright__browser_run_code_unsafe" not in a)
    check("smaller browser set (no take_screenshot)", "mcp__playwright__browser_take_screenshot" not in a)
    # The fix for the 82%-rc=1 turn-exhaustion: a self-governor so the pass can't run to the cap.
    check("has a TURN BUDGET governor", "TURN BUDGET" in prompt)
    check("bounds discovery to the peek-hinted market", "Handle ONLY the\nmarketplace" in prompt
          or "Handle ONLY the marketplace" in " ".join(prompt.split()))
    check("never loops on a stuck step (one retry then escalate)",
          "ONE retry" in prompt and "NEVER loop" in prompt)
    # Fix C: the soft turn budget — the prompt references the env knob so a cap-hit stays RARE.
    check("references the soft-turn budget env var", "$BAZAAR_BUYER_SOFT_TURNS" in prompt)
    check("soft budget tells it to stop opening NEW threads + journal + summarise",
          "stop opening NEW threads" in " ".join(prompt.split()))
    # Bug C4: the stop trigger must be phrased RELATIVE to the variable, not a hardcoded turn number
    # — an operator override of $BAZAAR_BUYER_SOFT_TURNS must stay consistent with the trigger.
    check("no hardcoded 'around turn ~25' (use the variable so an override stays consistent)",
          "turn ~25" not in prompt and "around turn 25" not in prompt)
    check("the stop trigger is phrased relative to the soft-turn budget var",
          "approach $BAZAAR_BUYER_SOFT_TURNS" in " ".join(prompt.split()))
    # Fix C: the SCOPE priority hint — prioritise the peek-named thread, but only as a HINT.
    check("SCOPE clause references the peek-thread env var", "$BAZAAR_BUYER_PEEK_THREAD" in prompt)
    check("peek-thread is a PRIORITY hint, handled first",
          "PRIORITISE that thread first" in prompt)
    check("peek-thread is NOT a hard 'only that thread' restriction (mis-route is worst)",
          "PRIORITY HINT" in prompt and "not a hard" in prompt.lower())
    # Fix A regression guard (the contended edit): the per-send commit rule MUST survive — never
    # weaken "never end with an un-committed send", and keep the new mark-sent step (Track A2) that
    # lets recovery tell a never-fired send from a sent-but-unjournaled one.
    check("Fix A's per-send commit rule still present",
          "un-committed send" in prompt and "journal_send.py commit" in prompt)
    check("Track A2 mark-sent step present in the bracket", "journal_send.py mark-sent" in prompt)
    check("Fix A's journal_reconcile first-step still present", "journal_reconcile.py" in prompt)


def test_buyer_soft_turns_env_default():
    print("buyer soft-turn budget env (default injected so $BAZAAR_BUYER_SOFT_TURNS resolves):")
    _argv, env = harness_run._invocation(get_harness("claude-code"), harness_run.build_spec("buyer"))
    check("BAZAAR_BUYER_SOFT_TURNS defaulted to 30", env.get("BAZAAR_BUYER_SOFT_TURNS") == "30")
    # An operator/caller value must win over the default (revertible knob).
    prev = os.environ.get("BAZAAR_BUYER_SOFT_TURNS")
    os.environ["BAZAAR_BUYER_SOFT_TURNS"] = "20"
    try:
        _a2, env2 = harness_run._invocation(get_harness("claude-code"), harness_run.build_spec("buyer"))
        check("explicit env wins over the default", env2.get("BAZAAR_BUYER_SOFT_TURNS") == "20")
    finally:
        if prev is None:
            os.environ.pop("BAZAAR_BUYER_SOFT_TURNS", None)
        else:
            os.environ["BAZAAR_BUYER_SOFT_TURNS"] = prev


def test_buyer_cap_hit_signal_mapping():
    print("run_pass cap-hit detection: rc!=0 + 'Reached max turns' marker -> signal 42 + breadcrumb:")
    import tempfile
    import json as _json
    saved_dir = harness_run.SELLER_DIR
    saved_log = harness_run.LOG
    saved_resolve = harness_run._resolve_harness
    saved_invocation = harness_run._invocation
    saved_run = harness_run.subprocess.run
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        harness_run.SELLER_DIR = root
        harness_run.LOG = root / "logs" / "pass.log"
        harness_run._resolve_harness = lambda: get_harness("claude-code")
        harness_run._invocation = lambda h, s: (["true"], {})
        # Isolate the outbox the completion gate (Track A3) peeks, so it sees an EMPTY outbox here
        # (this test exercises only cap-hit classification, not the never-fired-send gate).
        os.environ["BAZAAR_DATA_DIR"] = str(root / "data")

        class FakeRun:
            def __init__(self, rc):
                self.returncode = rc

        def make_run(rc, marker):
            def _run(argv, **kw):
                log = kw.get("stdout")
                if log is not None and marker:
                    log.write("Error: Reached max turns (50)\n")
                    log.flush()
                return FakeRun(rc)
            return _run
        try:
            # 1) rc!=0 AND the marker present -> the distinct cap-hit signal + a breadcrumb.
            harness_run.subprocess.run = make_run(1, True)
            rc = harness_run.run_pass("buyer", "carousell")
            check("cap-hit returns the distinct signal 42", rc == harness_run.CAP_HIT_SIGNAL)
            crumb = root / "data" / "pass_state" / "buyer:carousell.json"
            check("breadcrumb written at data/pass_state/<mode>:<resource>.json", crumb.exists())
            if crumb.exists():
                rec = _json.loads(crumb.read_text())
                check("breadcrumb records capped + resource", rec.get("capped") is True
                      and rec.get("resource") == "carousell" and rec.get("ts"))
            # 2) rc!=0 but NO marker -> a generic failure, NOT the cap-hit signal (key on BOTH).
            harness_run.subprocess.run = make_run(1, False)
            rc2 = harness_run.run_pass("buyer", "fb")
            check("generic rc=1 (no marker) is NOT the cap-hit signal", rc2 == 1)
            check("no breadcrumb for a non-cap failure",
                  not (root / "data" / "pass_state" / "buyer:fb.json").exists())
            # 3) rc=0 with the marker somehow present -> success, no cap-hit (key on rc TOO).
            harness_run.subprocess.run = make_run(0, True)
            rc3 = harness_run.run_pass("buyer", "ebay")
            check("rc=0 is success even if a marker is in the log", rc3 == 0)
        finally:
            harness_run.SELLER_DIR = saved_dir
            harness_run.LOG = saved_log
            harness_run._resolve_harness = saved_resolve
            harness_run._invocation = saved_invocation
            harness_run.subprocess.run = saved_run
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_caphit_per_pass_isolation_no_crosstalk():
    print("Bug C1: cap detection scans ONLY this pass's OWN output — a CONCURRENT worker's 'Reached"
          " max turns' line landing in the SHARED logs/pass.log must NOT misclassify this pass:")
    import tempfile
    saved_dir = harness_run.SELLER_DIR
    saved_log = harness_run.LOG
    saved_resolve = harness_run._resolve_harness
    saved_invocation = harness_run._invocation
    saved_run = harness_run.subprocess.run
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        harness_run.SELLER_DIR = root
        harness_run.LOG = root / "logs" / "pass.log"
        harness_run._resolve_harness = lambda: get_harness("claude-code")
        harness_run._invocation = lambda h, s: (["true"], {})
        # Isolate the outbox the completion gate (Track A3) peeks (empty here — this test exercises
        # only cap-hit per-pass isolation, not the never-fired-send gate).
        os.environ["BAZAAR_DATA_DIR"] = str(root / "data")

        class FakeRun:
            def __init__(self, rc):
                self.returncode = rc

        def make_run(rc, own_marker, foreign_marker_in_shared):
            """rc + whether THIS pass writes the marker to its OWN stdout, and (separately) whether a
            CONCURRENT worker's marker is injected into the SHARED logs/pass.log mid-run."""
            def _run(argv, **kw):
                # Simulate a concurrent worker appending its kill marker to the SHARED log while THIS
                # pass runs — the exact interleaving the offset slice mis-attributed.
                if foreign_marker_in_shared:
                    with harness_run.LOG.open("a") as shared:
                        shared.write("Error: Reached max turns (50)  [from a CONCURRENT worker]\n")
                        shared.flush()
                out = kw.get("stdout")  # THIS pass's own (per-pass) sink
                if out is not None and own_marker:
                    out.write("Error: Reached max turns (50)\n")
                    out.flush()
                return FakeRun(rc)
            return _run
        try:
            # Pass B: rc=1, did NOT itself hit the cap, but a concurrent worker's marker IS in the
            # shared log. With per-pass isolation it must be a GENERIC failure, not a cap-hit.
            harness_run.subprocess.run = make_run(1, own_marker=False, foreign_marker_in_shared=True)
            rc_b = harness_run.run_pass("buyer", "fb")
            check("a foreign marker in the shared log does NOT make this pass a cap-hit",
                  rc_b == 1)
            check("no spurious cap-hit breadcrumb for the misattributed pass",
                  not (root / "data" / "pass_state" / "buyer:fb.json").exists())
            # Pass A: rc=1 AND it really hit the cap (own marker) → cap-hit, even though the shared log
            # also carries other passes' lines.
            harness_run.subprocess.run = make_run(1, own_marker=True, foreign_marker_in_shared=True)
            rc_a = harness_run.run_pass("buyer", "carousell")
            check("a pass that REALLY capped is still classified cap-hit",
                  rc_a == harness_run.CAP_HIT_SIGNAL)
            check("the real cap-hit wrote its breadcrumb",
                  (root / "data" / "pass_state" / "buyer:carousell.json").exists())
            # The shared human log still captured BOTH passes' output (for tailing) + the headers.
            shared_text = harness_run.LOG.read_text()
            check("shared log preserves the per-pass header", "carousell pass" in shared_text)
            check("shared log preserves the pass-done footer with rc", "pass done rc=" in shared_text)
            check("shared log still contains the pass output (folded back in for human tailing)",
                  "Reached max turns" in shared_text)
        finally:
            harness_run.SELLER_DIR = saved_dir
            harness_run.LOG = saved_log
            harness_run._resolve_harness = saved_resolve
            harness_run._invocation = saved_invocation
            harness_run.subprocess.run = saved_run
            os.environ.pop("BAZAAR_DATA_DIR", None)


def test_caphit_log_sweep_removes_stale_only():
    print("Bug C6: sweep_stale_pass_logs removes a STALE leaked pass-*.log (forced kills skip the"
          " run_pass finally), but never a FRESH one nor the human-readable logs/pass.log:")
    import tempfile
    import time as _time
    saved_dir = harness_run.SELLER_DIR
    saved_log = harness_run.LOG
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        harness_run.SELLER_DIR = root
        harness_run.LOG = root / "logs" / "pass.log"
        try:
            logs = root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            # A leaked per-pass file orphaned by a SIGTERM/SIGKILL that skipped run_pass's finally.
            stale = logs / "pass-buyer-fb-12345.log"
            stale.write_text("Error: Reached max turns (50)\n")
            fresh = logs / "pass-buyer-carousell-67890.log"
            fresh.write_text("in progress\n")
            human = logs / "pass.log"
            human.write_text("=== shared human-readable log ===\n")
            # Age the stale file well past the cutoff; leave fresh + human current.
            old = _time.time() - 60 * 60
            os.utime(stale, (old, old))
            removed = harness_run.sweep_stale_pass_logs(max_age_min=30)
            check("the stale leaked pass-*.log is swept", not stale.exists())
            check("a fresh per-pass log is NOT swept (a live pass owns it)", fresh.exists())
            check("the human-readable logs/pass.log is NEVER swept", human.exists())
            check("the sweep reports the file(s) it removed", stale.name in removed or 1 == len(removed))
            # Idempotent: a second sweep with nothing stale removes nothing and never raises.
            again = harness_run.sweep_stale_pass_logs(max_age_min=30)
            check("a second sweep removes nothing (idempotent)", again == [])
            # Fail-open: a missing logs dir must not raise.
            harness_run.SELLER_DIR = root / "does_not_exist"
            harness_run.LOG = harness_run.SELLER_DIR / "logs" / "pass.log"
            check("missing logs dir → no crash, empty result",
                  harness_run.sweep_stale_pass_logs(max_age_min=30) == [])
        finally:
            harness_run.SELLER_DIR = saved_dir
            harness_run.LOG = saved_log


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
    # The catchup stand-down survives, but only for a RECENTLY-UPDATED sweep: the daemon's deterministic
    # reconciler clears a stale/orphaned one before this pass runs (so it can't freeze the lane forever).
    check("still defers to an active catch-up sweep", "catchup_session.json" in prompt)
    check("stand-down scoped to a fresh sweep", "recently updated" in prompt)


def test_followup_branch_in_buyer_and_buy():
    print("follow-up branch rides the buyer/buy prompts, gated on $BAZAAR_FOLLOWUP:")
    buyer = harness_run.build_spec("buyer").prompt
    check("buyer prompt has FOLLOW-UP MODE", "FOLLOW-UP MODE" in buyer)
    check("buyer follow-up gated on the env flag", "$BAZAAR_FOLLOWUP=1" in buyer)
    check("buyer follow-up marks the ledger", "followup_state.py mark-nudge" in buyer
          and "--side sell" in buyer)
    check("buyer follow-up re-reads the tail (skip if they replied)",
          "RE-READ its tail" in buyer)
    buy = harness_run.build_spec("buy").prompt
    check("buy prompt has FOLLOW-UP MODE", "FOLLOW-UP MODE" in buy)
    check("buy follow-up uses --side buy", "mark-nudge --thread <thread_id> --side buy" in buy)


def test_maint_listing_health_step():
    print("maint pass gains the stale-listing suggestion step + voice/skill in the cached prefix:")
    spec = harness_run.build_spec("maint")
    prompt = spec.prompt
    check("maint prompt references listing_health_session", "listing_health_session.json" in prompt)
    check("maint prompt marks the ledger after suggesting", "listing_health.py mark" in prompt)
    check("maint prompt voids episode if no longer live", 'status=="live"' in prompt)
    check("maint core skills now include style.md (free-form copy)",
          "skills/style.md" in harness_run.CORE_SKILLS["maint"])
    check("maint core skills include the listing-health skill",
          "skills/channel/listing-health.md" in harness_run.CORE_SKILLS["maint"])


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


def test_research_argv():
    print("research pass (detached BACKGROUND worker — browser-free + channel-free):")
    spec = harness_run.build_spec("research")
    a = claude_argv("research").argv
    check("model sonnet (vision quality)", _flag_value(a, "--model") == "sonnet")
    check("bounded turns", _flag_value(a, "--max-turns") == "8")
    check("strict MCP (no browser MCP)", "--strict-mcp-config" in a)
    check("NO playwright/browser tools at all", not any("mcp__playwright" in str(p) for p in a))
    check("NO general bash (cannot call telegram/other tools)",
          "Bash(python3:*)" not in spec.allowed_tools)
    check("ONLY the result-writer bash is allowed",
          "Bash(python3 bin/research_result.py:*)" in spec.allowed_tools)
    check("can read photos + search comps", "Read" in spec.allowed_tools and "WebSearch" in spec.allowed_tools)
    check("research is a dispatchable pass mode", "research" in harness_run.PASS_MODES)


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


def test_no_pause_line_narration():
    print("regression: PAUSE_LINE removed — no 'holding here' narration in any background pass prompt"
          " (the unbounded duplicate-ack source; pause is enforced by the hook + preemption + drain):")
    check("harness_run no longer defines PAUSE_LINE", not hasattr(harness_run, "PAUSE_LINE"))
    for mode in ("buyer", "buy", "maint"):
        prompt = harness_run.build_spec(mode).prompt
        check(f"{mode} prompt carries no 'holding here' narration", "holding here" not in prompt.lower())


if __name__ == "__main__":
    test_seller_argv()
    test_channel_argv()
    test_buyer_argv()
    test_buyer_soft_turns_env_default()
    test_buyer_cap_hit_signal_mapping()
    test_caphit_per_pass_isolation_no_crosstalk()
    test_caphit_log_sweep_removes_stale_only()
    test_buy_argv()
    test_maint_argv()
    test_followup_branch_in_buyer_and_buy()
    test_maint_listing_health_step()
    test_maint_model_env_override()
    test_intent_argv()
    test_eval_argv()
    test_research_argv()
    test_resource_scoping()
    test_daemon_pass_env_marker()
    test_no_secret_in_argv()
    test_no_pause_line_narration()
    test_codex_stub()
    test_runner_refuses_unverified_harness()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("All harness argv tests passed.")
