#!/usr/bin/env python3
"""eval_run.py — run the conversation evaluation and write findings + a report.

Pipeline: build the corpus (eval_corpus) -> deterministic checks (eval_checks, $0) -> optional
LLM judge (eval_judge) over the not-yet-flagged records -> cluster into improvement candidates
(eval_cluster) -> persist. The deterministic checks alone catch the two reported defects, so
`--no-llm` is a complete, zero-cost run (CI gate, and the nightly daemon when
config.eval_judge_nightly is false). By default the nightly daemon runs WITH the judge.

Outputs (all local, gitignored — they quote conversation text):
    data/eval/findings.jsonl       every finding
    data/eval/improvements.jsonl   deduped, ranked improvement candidates
    data/eval/report.md            human-readable report

Usage:
    eval_run.py run  [--since ISO | --last N] [--no-llm] [--max-judge N] [--fail-on SEVERITY]
    eval_run.py report [--in data/eval/findings.jsonl]

Exit: 0 ok · 1 findings at/above --fail-on · 2 bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import eval_checks
import eval_cluster
import eval_corpus
from eval_schema import SEVERITIES, Finding, severity_rank
from scan_state import parse_iso

EVAL_DIR = Path(__file__).resolve().parent.parent / "data" / "eval"
_MAX_PER_SEVERITY = 8  # cap the detail listing; the clustered candidates carry the full count
FINDINGS_PATH = EVAL_DIR / "findings.jsonl"
IMPROVEMENTS_PATH = EVAL_DIR / "improvements.jsonl"
REPORT_PATH = EVAL_DIR / "report.md"


def _select_for_judge(corpus, deterministic):
    """Records the judge should look at: every channel turn + pass, minus those a deterministic
    check already flagged high/critical (no point paying the model to re-find a sure thing)."""
    flagged = {f.record_id for f in deterministic
               if f.record_id and f.severity in ("high", "critical")}
    return [r for r in (corpus.channel_turns + corpus.pass_records) if r.record_id not in flagged]


def _dedup_judge(deterministic, judged):
    """Drop judge findings already covered by a deterministic finding (deterministic wins)."""
    seen = {(f.category, f.record_id) for f in deterministic}
    return [f for f in judged if (f.category, f.record_id) not in seen]


def _write_outputs(findings, candidates, report_text):
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    FINDINGS_PATH.write_text("".join(json.dumps(f.to_dict(), ensure_ascii=False) + "\n"
                                     for f in findings))
    IMPROVEMENTS_PATH.write_text("".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n"
                                         for c in candidates))
    REPORT_PATH.write_text(report_text)


def _render_report(findings, candidates, corpus, now) -> str:
    by_sev = {s: [f for f in findings if f.severity == s] for s in SEVERITIES}
    lines = [
        "# Bazaar evaluation report", "",
        f"- generated: {now.isoformat()}",
        f"- passes in scope: {len(corpus.pass_records)} · channel turns: {len(corpus.channel_turns)}",
        "- findings: " + ", ".join(f"{s}={len(by_sev[s])}" for s in SEVERITIES),
        "",
        "## Top improvement candidates", "",
    ]
    if not candidates:
        lines.append("_None — no issues found in scope._")
    for c in candidates[:10]:
        lines.append(f"### [{c.severity_max.upper()}] {c.category} — `{c.target}` (×{c.occurrences})")
        lines.append(f"- {c.summary}")
        lines.append(f"- **fix:** {c.suggestion}")
        for ex in c.exemplars:
            lines.append(f"  - evidence: {ex}")
        lines.append("")

    lines += ["## Findings by severity", "",
              "_Detail view; recurring findings are collapsed in the candidates above._", ""]
    any_finding = False
    for sev in SEVERITIES:
        fs = by_sev[sev]
        if not fs:
            continue
        any_finding = True
        lines.append(f"### {sev.upper()} ({len(fs)})")
        for f in fs[:_MAX_PER_SEVERITY]:
            lines.append(f"- **{f.category}** · {f.source} (conf {f.confidence:g}) — {f.summary}")
            lines.append(f"  - evidence: {f.evidence}")
            lines.append(f"  - fix: {f.suggestion}  ·  target: `{f.target}`")
        if len(fs) > _MAX_PER_SEVERITY:
            lines.append(f"- … and {len(fs) - _MAX_PER_SEVERITY} more (see `data/eval/findings.jsonl`)")
        lines.append("")
    if not any_finding:
        lines.append("_No findings._")
        lines.append("")

    lines += ["## Pass health", "", "| window | mode | rc | finding? |", "|---|---|---|---|"]
    flagged_modes = {f.pass_mode for f in findings if f.pass_mode}
    for pr in corpus.pass_records:
        mark = "⚠️" if pr.pass_mode in flagged_modes else "ok"
        lines.append(f"| {pr.window_start} | {pr.pass_mode} | {pr.rc} | {mark} |")
    return "\n".join(lines) + "\n"


def cmd_run(args) -> int:
    now = datetime.now(timezone.utc)
    since = parse_iso(args.since) if args.since else None
    corpus = eval_corpus.build(now=now, since=since, last=args.last)

    findings = eval_checks.run(corpus)
    if not args.no_llm:
        import eval_judge  # imported lazily so --no-llm has zero judge dependency
        judged = eval_judge.judge(_select_for_judge(corpus, findings), max_judge=args.max_judge)
        findings += _dedup_judge(findings, judged)

    candidates = eval_cluster.cluster(findings)
    report = _render_report(findings, candidates, corpus, now)
    _write_outputs(findings, candidates, report)

    # Learning loop (opt-in): tone/voice findings become reviewable style proposals. Fail-open and
    # gated by data/style.json `learning` (style.record_proposal skips when it is "off"). The user
    # reviews + applies these via `/bazaar -> style`; nothing rewrites the persona silently here.
    style_proposals = 0
    try:
        import style  # local bin/ module
        style_proposals = style.proposals_from_findings([f.to_dict() for f in findings])
    except Exception:  # noqa: BLE001 — a style hiccup must never fail the eval run
        style_proposals = 0

    worst = max((severity_rank(f.severity) for f in findings), default=-1)
    summary = {"findings": len(findings), "candidates": len(candidates),
               "passes": len(corpus.pass_records), "style_proposals": style_proposals,
               "report": str(REPORT_PATH)}
    print(json.dumps(summary))
    if args.fail_on and worst >= severity_rank(args.fail_on):
        return 1
    return 0


def cmd_report(args) -> int:
    path = Path(args.infile)
    if not path.exists():
        print(json.dumps({"error": f"no findings at {path}"}), file=sys.stderr)
        return 2
    findings = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            findings.append(Finding.from_dict(json.loads(line)))
    candidates = eval_cluster.cluster(findings)
    corpus = eval_corpus.Corpus()  # report-only: no fresh corpus, pass-health table is empty
    print(_render_report(findings, candidates, corpus, datetime.now(timezone.utc)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval_run.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--since", default="")
    r.add_argument("--last", type=int, default=None)
    r.add_argument("--no-llm", action="store_true", dest="no_llm")
    r.add_argument("--max-judge", type=int, default=40, dest="max_judge")
    r.add_argument("--fail-on", default="", choices=["", *SEVERITIES], dest="fail_on")
    r.set_defaults(func=cmd_run)
    rep = sub.add_parser("report")
    rep.add_argument("--in", default=str(FINDINGS_PATH), dest="infile")
    rep.set_defaults(func=cmd_report)
    return p


def main(argv) -> int:
    try:
        ns = build_parser().parse_args(argv[1:])
    except SystemExit:
        return 2
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
