#!/usr/bin/env python3
"""Tests for eval_cluster.py — dedup + rank findings into improvement candidates.

    python3 tests/test_eval_cluster.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import eval_cluster  # noqa: E402
from eval_schema import Finding  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _finding(category, target, severity="high", evidence="e"):
    return Finding(category=category, severity=severity, source="deterministic",
                   summary=f"{category} on {target}", evidence=evidence,
                   suggestion="fix it", target=target)


def test_dedup_and_count():
    print("dedup by (category, target) with occurrence count:")
    findings = [
        _finding("missed-action", "skills/listing-flows/fb.md", evidence="fb=20"),
        _finding("missed-action", "skills/listing-flows/fb.md", evidence="fb=18"),
        _finding("context-loss", "bin/harness_run.py:CHANNEL_PROMPT"),
    ]
    cands = eval_cluster.cluster(findings)
    check("two candidates (fb collapsed)", len(cands) == 2)
    fb = next(c for c in cands if c.target == "skills/listing-flows/fb.md")
    check("fb candidate counts 2 occurrences", fb.occurrences == 2)
    check("fb candidate keeps distinct exemplars", set(fb.exemplars) == {"fb=20", "fb=18"})


def test_ranking():
    print("ranking by severity then frequency:")
    findings = [
        _finding("redundant-recheck", "a", severity="medium"),
        _finding("redundant-recheck", "a", severity="medium"),  # 2x medium
        _finding("secret-leak", "skills/voice.md", severity="critical"),
    ]
    cands = eval_cluster.cluster(findings)
    check("critical ranks first despite lower frequency", cands[0].category == "secret-leak")


def test_fb_targets_recipe():
    print("FB missed-action candidate targets the fb recipe (the deferred contribution seam):")
    cands = eval_cluster.cluster([_finding("missed-action", "skills/listing-flows/fb.md")])
    check("target preserved", cands[0].target == "skills/listing-flows/fb.md")
    check("candidate is JSON-serializable", isinstance(cands[0].to_dict(), dict))


if __name__ == "__main__":
    print("eval_cluster.py tests\n")
    test_dedup_and_count()
    test_ranking()
    test_fb_targets_recipe()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
