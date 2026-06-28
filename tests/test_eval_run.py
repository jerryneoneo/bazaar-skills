#!/usr/bin/env python3
"""Tests for eval_run.py — the self-evaluation orchestrator (its components are tested separately).

    python3 tests/test_eval_run.py

Covers the glue the component tests don't: the cmd_run pipeline (corpus -> checks -> cluster ->
write) with components stubbed, the --fail-on severity gate, report rendering, the judge
select/dedup helpers, and CLI input guards. Output paths are redirected to a temp dir so the
real data/eval/ is never touched.
"""

import argparse
import io
import json
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import eval_run  # noqa: E402
import eval_corpus  # noqa: E402
from eval_schema import EvalRecord, Finding  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _finding(category="pass-failure", severity="high", record_id="r1"):
    return Finding(category=category, severity=severity, source="deterministic",
                   summary="s", evidence="e", suggestion="fix", target="t",
                   record_id=record_id, pass_mode="buyer")


def _run_args(no_llm=True, fail_on=""):
    return argparse.Namespace(since="", last=None, no_llm=no_llm, max_judge=40, fail_on=fail_on)


def _redirect_outputs(tmp):
    """Point eval_run's output globals at a temp dir; return the saved originals to restore."""
    saved = (eval_run.EVAL_DIR, eval_run.FINDINGS_PATH, eval_run.IMPROVEMENTS_PATH, eval_run.REPORT_PATH)
    eval_run.EVAL_DIR = Path(tmp)
    eval_run.FINDINGS_PATH = Path(tmp) / "findings.jsonl"
    eval_run.IMPROVEMENTS_PATH = Path(tmp) / "improvements.jsonl"
    eval_run.REPORT_PATH = Path(tmp) / "report.md"
    return saved


def _restore_outputs(saved):
    eval_run.EVAL_DIR, eval_run.FINDINGS_PATH, eval_run.IMPROVEMENTS_PATH, eval_run.REPORT_PATH = saved


def _patch(mod_attr_fn):
    """Patch a list of (obj, attr, value); return restore thunk."""
    saved = [(o, a, getattr(o, a)) for (o, a, _) in mod_attr_fn]
    for (o, a, v) in mod_attr_fn:
        setattr(o, a, v)
    return lambda: [setattr(o, a, ov) for (o, a, ov) in saved]


def test_select_for_judge_skips_sure_things():
    print("_select_for_judge: records a deterministic high/critical already flagged are skipped:")
    corpus = types.SimpleNamespace(
        channel_turns=[EvalRecord(record_id="r1", kind="channel_turn")],
        pass_records=[EvalRecord(record_id="r2", kind="pass")])
    out = eval_run._select_for_judge(corpus, [_finding(severity="high", record_id="r1")])
    ids = {r.record_id for r in out}
    check("flagged r1 excluded", "r1" not in ids)
    check("unflagged r2 kept", "r2" in ids)


def test_dedup_judge_deterministic_wins():
    print("_dedup_judge: a judge finding already covered deterministically is dropped:")
    det = [_finding(category="context-loss", record_id="r1")]
    judged = [_finding(category="context-loss", severity="medium", record_id="r1"),
              _finding(category="tone", severity="low", record_id="r2")]
    out = eval_run._dedup_judge(det, judged)
    cats = {f.category for f in out}
    check("duplicate (context-loss, r1) dropped", "context-loss" not in cats)
    check("novel (tone, r2) kept", "tone" in cats)


def test_render_report_has_header_and_counts():
    print("_render_report: renders header, severity line, and a candidate block:")
    findings = [_finding(category="missed-action", severity="high", record_id="r1")]
    candidates = eval_run.eval_cluster.cluster(findings)
    text = eval_run._render_report(findings, candidates, eval_corpus.Corpus(), eval_run.datetime.now(eval_run.timezone.utc))
    check("has report header", "# Bazaar evaluation report" in text)
    check("severity counts line present", "high=1" in text)
    check("candidate section rendered", "Top improvement candidates" in text and "missed-action" in text)


def test_cmd_run_no_llm_writes_outputs():
    print("cmd_run --no-llm: runs the $0 pipeline and writes all three outputs:")
    with tempfile.TemporaryDirectory() as tmp:
        saved_out = _redirect_outputs(tmp)
        restore = _patch([
            (eval_run.eval_corpus, "build", lambda **k: eval_corpus.Corpus()),
            (eval_run.eval_checks, "run", lambda corpus: []),
        ])
        _real_style = sys.modules.get("style")
        fake_style = types.ModuleType("style")
        fake_style.proposals_from_findings = lambda findings: 0
        sys.modules["style"] = fake_style
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = eval_run.cmd_run(_run_args(no_llm=True, fail_on=""))
            summary = json.loads(buf.getvalue().strip().splitlines()[-1])
            check("rc 0 on a clean run", rc == 0)
            check("findings.jsonl written", eval_run.FINDINGS_PATH.exists())
            check("improvements.jsonl written", eval_run.IMPROVEMENTS_PATH.exists())
            check("report.md written", eval_run.REPORT_PATH.exists())
            check("summary reports zero findings", summary["findings"] == 0)
        finally:
            restore()
            if _real_style is not None:           # restore the real module — do NOT evict it
                sys.modules["style"] = _real_style
            else:
                sys.modules.pop("style", None)
            _restore_outputs(saved_out)


def test_cmd_run_fail_on_gates_exit():
    print("cmd_run --fail-on high: a high finding flips the exit code to 1:")
    with tempfile.TemporaryDirectory() as tmp:
        saved_out = _redirect_outputs(tmp)
        restore = _patch([
            (eval_run.eval_corpus, "build", lambda **k: eval_corpus.Corpus()),
            (eval_run.eval_checks, "run", lambda corpus: [_finding(severity="high")]),
        ])
        _real_style = sys.modules.get("style")
        fake_style = types.ModuleType("style")
        fake_style.proposals_from_findings = lambda findings: 0
        sys.modules["style"] = fake_style
        try:
            with redirect_stdout(io.StringIO()):
                rc = eval_run.cmd_run(_run_args(no_llm=True, fail_on="high"))
            check("rc 1 when worst finding >= --fail-on", rc == 1)
            line_count = len(eval_run.FINDINGS_PATH.read_text().splitlines())
            check("the high finding was persisted", line_count == 1)
        finally:
            restore()
            if _real_style is not None:           # restore the real module — do NOT evict it
                sys.modules["style"] = _real_style
            else:
                sys.modules.pop("style", None)
            _restore_outputs(saved_out)


def test_cmd_report_reads_findings_file():
    print("cmd_report: renders from a findings file; missing file → exit 2:")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "findings.jsonl"
        path.write_text(json.dumps(_finding().to_dict()) + "\n")
        with redirect_stdout(io.StringIO()):
            rc = eval_run.cmd_report(argparse.Namespace(infile=str(path)))
        check("rc 0 on an existing file", rc == 0)
        rc2 = eval_run.cmd_report(argparse.Namespace(infile=str(Path(tmp) / "nope.jsonl")))
        check("missing file → exit 2", rc2 == 2)


def test_main_bad_input():
    print("main: no subcommand → exit 2 (argparse SystemExit swallowed):")
    with redirect_stdout(io.StringIO()):
        try:
            rc = eval_run.main(["eval_run.py"])
        except SystemExit:
            rc = "raised"
    check("missing subcommand → exit 2", rc == 2)


if __name__ == "__main__":
    print("eval_run tests\n")
    test_select_for_judge_skips_sure_things()
    test_dedup_judge_deterministic_wins()
    test_render_report_has_header_and_counts()
    test_cmd_run_no_llm_writes_outputs()
    test_cmd_run_fail_on_gates_exit()
    test_cmd_report_reads_findings_file()
    test_main_bad_input()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
