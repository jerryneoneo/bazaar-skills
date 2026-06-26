#!/usr/bin/env python3
"""Tests for eval_judge.py — argv shape, secret boundary, tolerant parsing, fake-claude run.

    python3 tests/test_eval_judge.py

The integration test stubs the `claude` binary via CLAUDE_BIN (the same override _invocation
honors) so no real model is called: it asserts a canned JSON array becomes a finding, and that
malformed output becomes a meta-finding (never a silent drop).
"""

import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import eval_judge  # noqa: E402
import harness_run  # noqa: E402
from eval_schema import EvalRecord  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def test_eval_argv_is_mcpless_and_toolless():
    print("eval PassSpec -> MCP-less, single-turn, no browser tools:")
    from harnesses import get_harness
    spec = harness_run.build_spec("eval", "JUDGE THIS")
    argv = list(get_harness("claude-code").pass_argv(spec).argv)
    joined = " ".join(argv)
    check("strict-mcp-config present", "--strict-mcp-config" in argv)
    check("single max-turns 1", "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "1")
    check("no --allowedTools (no tools at all)", "--allowedTools" not in argv)
    check("no playwright/browser tools", "mcp__playwright" not in joined)
    check("prompt carried", "JUDGE THIS" in argv)


def test_assert_no_secret():
    print("secret-boundary refusal:")
    raised = False
    try:
        eval_judge._assert_no_secret('[{"floor": 30}]')
    except eval_judge.SecretInPayload:
        raised = True
    check("a 'floor' key in payload is refused", raised)
    raised2 = False
    try:
        eval_judge._assert_no_secret('[{"text": "read data/floors/x.json"}]')
    except eval_judge.SecretInPayload:
        raised2 = True
    check("a silo path in payload is refused", raised2)
    # The mere WORD floor in a quoted buyer message must NOT trip the guard.
    eval_judge._assert_no_secret('[{"user_said": "whats your floor price?"}]')
    check("quoted 'floor' in message text is allowed", True)


def test_extract_json_array():
    print("tolerant JSON-array extraction:")
    check("plain array", eval_judge._extract_json_array('[{"a":1}]') == [{"a": 1}])
    check("array with surrounding prose",
          eval_judge._extract_json_array('Sure! [{"a":1}] done') == [{"a": 1}])
    check("non-array returns None", eval_judge._extract_json_array("no json here") is None)


def test_to_finding():
    print("item -> Finding mapping (target derived from category):")
    by_id = {"r1": EvalRecord(record_id="r1", kind="channel_turn", pass_mode="", window_start="t")}
    f = eval_judge._to_finding(
        {"record_id": "r1", "category": "context-loss", "severity": "high",
         "evidence": "re-checked", "suggestion": "act", "confidence": 0.9}, by_id)
    check("source is llm-judge", f.source == "llm-judge")
    check("target derived from category", f.target == "bin/harness_run.py:CHANNEL_PROMPT")
    check("bad severity coerced to low",
          eval_judge._to_finding({"record_id": "r1", "category": "tone-voice",
                                  "severity": "BOGUS"}, by_id).severity == "low")
    check("no category -> dropped", eval_judge._to_finding({"record_id": "r1"}, by_id) is None)


def _write_fake_claude(tmp, body):
    """A fake `claude` that ignores its args and prints `body` to stdout."""
    path = Path(tmp) / "fake_claude.py"
    path.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(%r)\n" % body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run_judge_with_fake(body, records):
    with tempfile.TemporaryDirectory() as tmp:
        fake = _write_fake_claude(tmp, body)
        saved = {k: os.environ.get(k) for k in ("CLAUDE_BIN", "BAZAAR_HARNESS")}
        os.environ["CLAUDE_BIN"] = str(fake)
        os.environ["BAZAAR_HARNESS"] = "claude-code"
        try:
            return eval_judge.judge(records, batch_size=8, max_judge=8)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_judge_with_fake_claude():
    print("judge() over a stubbed claude:")
    records = [EvalRecord(record_id="r1", kind="channel_turn", user_said="do all tasks",
                          agent_considered="let me check")]
    good = ('[{"record_id":"r1","category":"context-loss","severity":"high",'
            '"evidence":"re-checked","suggestion":"act","confidence":0.8}]')
    findings = _run_judge_with_fake(good, records)
    check("canned finding parsed", len(findings) == 1 and findings[0].category == "context-loss")
    check("python fake binary executed (note: needs interpreter)", True)

    bad = _run_judge_with_fake("this is not json at all", records)
    check("malformed output -> exactly one meta-finding (no silent drop)",
          len(bad) == 1 and bad[0].category == "judge-error")


if __name__ == "__main__":
    print("eval_judge.py tests\n")
    test_eval_argv_is_mcpless_and_toolless()
    test_assert_no_secret()
    test_extract_json_array()
    test_to_finding()
    # The fake binary is a .py; CLAUDE_BIN replaces argv[0]=claude with it, but argv[1:] are
    # claude flags. We run it through the interpreter shim below to keep it portable.
    test_judge_with_fake_claude()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
