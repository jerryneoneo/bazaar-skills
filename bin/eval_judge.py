#!/usr/bin/env python3
"""eval_judge.py — optional LLM-as-judge over eval records (the nuanced layer above the checks).

Reuses the harness seam: it asks harness_run for an `eval` PassSpec (MCP-less, single-turn, sonnet),
translates it to argv via the active harness, and runs it like run_intent — capture_output, timeout,
no browser tools, so the judge can never act on the world. Records are batched; output must be a
strict JSON array; malformed output becomes a logged meta-finding, never a silent drop.

Secret discipline at the boundary: the payload is asserted free of any floor/budget store before it
is ever sent (records carry message text + non-secret signals only — this is belt-and-braces).
"""

from __future__ import annotations

import json
import re
import subprocess

import harness_run
from eval_schema import Finding

BATCH_SIZE = 8
MAX_JUDGE = 40
TIMEOUT_SEC = 150

# Where each judged category should be improved — keeps judge findings clusterable with the
# deterministic ones (same (category, target) grouping in eval_cluster).
TARGET_BY_CATEGORY = {
    "context-loss": "bin/harness_run.py:CHANNEL_PROMPT",
    "missed-action": "skills/reply-pipeline.md",
    "misroute": ".claude/commands/bazaar-run.md",
    "tone-voice": "skills/voice.md",
    "hallucinated-state": "skills/reply-pipeline.md",
    "unhelpful-ux": "skills/voice.md",
}
VALID_SEVERITIES = ("critical", "high", "medium", "low")

JUDGE_INSTRUCTIONS = """You are an offline evaluator (a judge) of a marketplace assistant's behavior.
You are given a JSON array of RECORDS — each is one control-channel turn (what the user said + the
agent's considered reply + the agent's prior turn) or one agent pass (mode, return code, narrative).

For each record that shows a REAL problem, emit one finding. Strongly prefer emitting nothing over
speculating; only flag what the record itself evidences.

Rubric (category : when to use it):
- context-loss : the reply ignores or contradicts the immediately prior turn (e.g. re-asks or
  re-checks state after the user already said "do all"/"yes").
- missed-action : the record shows the agent should have acted on available state but didn't.
- misroute : a buy request treated as sell, or vice-versa.
- tone-voice : rude/robotic; no friendly acknowledgement before a slow step; claims to be human when
  asked outright, or proactively announces it's a bot/assistant in chat (both violate voice.md Rule 3).
- hallucinated-state : claims an action, price, or fact not supported by the record.
- unhelpful-ux : technically correct but unhelpful or confusing for the user.

Output ONLY a JSON array (it may be empty). Each element exactly:
{"record_id": "<the record's record_id>", "category": "<one rubric key>",
 "severity": "critical|high|medium|low", "evidence": "<short quote from the record>",
 "suggestion": "<one concrete, actionable fix>", "confidence": <0.0-1.0>}
No prose, no explanation, no markdown code fences — just the JSON array."""


class SecretInPayload(Exception):
    """Raised if a judge payload would carry a secret store — refuse to send (fail loud)."""


def _assert_no_secret(payload_text: str) -> None:
    # Records have no floor/budget field by construction; refuse if a silo path or such a key
    # appears anyway. (Does NOT match the mere word "floor" in a quoted buyer message.)
    if "data/floors/" in payload_text or "data/budgets/" in payload_text:
        raise SecretInPayload("payload references a secret silo path")
    if re.search(r'"(floor|max_budget|target_price)"\s*:', payload_text):
        raise SecretInPayload("payload carries a secret-valued key")


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def build_prompt(records) -> str:
    payload = json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=0)
    return f"{JUDGE_INSTRUCTIONS}\n\nRECORDS:\n{payload}"


def _extract_json_array(text: str):
    """Tolerant: take the outermost [...] span. Returns a list, or None if unparseable."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _meta_finding(detail: str) -> Finding:
    return Finding(
        category="judge-error", severity="low", source="llm-judge",
        summary="LLM judge produced unusable output", evidence=detail[:240],
        suggestion="Inspect the judge response; this batch was skipped, not silently dropped.",
        target="bin/eval_judge.py", confidence=1.0)


def _to_finding(item: dict, by_id: dict) -> Finding | None:
    if not isinstance(item, dict):
        return None
    category = str(item.get("category", "")).strip()
    if not category:
        return None
    severity = item.get("severity", "low")
    if severity not in VALID_SEVERITIES:
        severity = "low"
    if category == "secret-leak":  # the judge may not unilaterally assert critical secret leaks
        severity = min(severity, "high", key=lambda s: VALID_SEVERITIES.index(s))
    record_id = str(item.get("record_id", ""))
    rec = by_id.get(record_id)
    return Finding(
        category=category, severity=severity, source="llm-judge",
        summary=item.get("summary") or f"judge: {category}",
        evidence=str(item.get("evidence", ""))[:240],
        suggestion=str(item.get("suggestion", "")) or "(no suggestion)",
        target=TARGET_BY_CATEGORY.get(category, f"judge:{category}"),
        record_id=record_id,
        pass_mode=rec.pass_mode if rec else "",
        window=rec.window_start if rec else "",
        confidence=float(item.get("confidence", 0.5)) if _is_number(item.get("confidence")) else 0.5,
    )


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _run_judge(prompt: str) -> tuple[int, str]:
    harness = harness_run._resolve_harness()
    argv, env = harness_run._invocation(harness, harness_run.build_spec("eval", prompt))
    out = subprocess.run(argv, cwd=str(harness_run.SELLER_DIR), env=env,
                         capture_output=True, text=True, timeout=TIMEOUT_SEC)
    return out.returncode, out.stdout


def judge(records, batch_size: int = BATCH_SIZE, max_judge: int = MAX_JUDGE) -> list[Finding]:
    """Run the judge over up to max_judge records, batched. Returns findings (+ meta-findings for
    any unparseable batch). Never raises on a model/transport hiccup — that becomes a meta-finding."""
    records = list(records)[:max_judge]
    if not records:
        return []
    by_id = {r.record_id: r for r in records}
    findings: list[Finding] = []
    for batch in _chunks(records, batch_size):
        prompt = build_prompt(batch)
        try:
            _assert_no_secret(prompt)
        except SecretInPayload as exc:
            findings.append(_meta_finding(f"refused to send batch: {exc}"))
            continue
        try:
            rc, stdout = _run_judge(prompt)
        except (subprocess.SubprocessError, SystemExit) as exc:
            findings.append(_meta_finding(f"judge invocation failed: {exc}"))
            continue
        arr = _extract_json_array(stdout)
        if arr is None:
            findings.append(_meta_finding(f"unparseable judge output (rc={rc}): {stdout[:120]}"))
            continue
        for item in arr:
            f = _to_finding(item, by_id)
            if f is not None:
                findings.append(f)
    return findings
