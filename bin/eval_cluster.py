#!/usr/bin/env python3
"""eval_cluster.py — fold findings into deduped, ranked improvement candidates.

Pure: (list[Finding]) -> list[ImprovementCandidate]. Findings are grouped by (category, target):
every "missed-action on skills/listing-flows/fb.md" collapses into one candidate with an
occurrence count and a few exemplars. Candidates are ranked by severity then frequency.

This JSONL is the clean hand-off seam for the (deferred) open-source contribution loop, which
will redact + seek consent before any of it ever leaves the device.
"""

from __future__ import annotations

from eval_schema import Finding, ImprovementCandidate, fingerprint, severity_rank

_MAX_EXEMPLARS = 3


def cluster(findings: list[Finding]) -> list[ImprovementCandidate]:
    groups: dict[tuple, list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.category, f.target), []).append(f)

    candidates = []
    for (category, target), group in groups.items():
        worst = max(group, key=lambda f: severity_rank(f.severity))
        exemplars = []
        for f in group:
            if f.evidence and f.evidence not in exemplars:
                exemplars.append(f.evidence)
            if len(exemplars) >= _MAX_EXEMPLARS:
                break
        candidates.append(ImprovementCandidate(
            candidate_id=fingerprint("cand", category, target),
            category=category,
            target=target,
            severity_max=worst.severity,
            occurrences=len(group),
            summary=worst.summary,
            suggestion=worst.suggestion,
            exemplars=exemplars,
            finding_ids=[f.finding_id for f in group],
        ))

    candidates.sort(key=lambda c: (severity_rank(c.severity_max), c.occurrences), reverse=True)
    return candidates
