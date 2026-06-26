#!/usr/bin/env python3
"""eval_schema.py — record + finding types for the conversation evaluation engine.

Pure data, no I/O. An EvalRecord is one unit of evaluable behaviour (a channel turn the user
drove, or one agent pass). A Finding is one thing that went wrong, with quoted evidence and a
concrete suggestion + a `target` file/area used to cluster findings into improvement candidates.

Nothing here ever holds a secret: records carry message text + non-secret signals (counts, ids,
statuses, rc), never a floor/budget/address value (those are siloed and never enter the corpus).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

SEVERITIES = ("critical", "high", "medium", "low")
_SEVERITY_RANK = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # low=0 … critical=3


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 0)


def fingerprint(*parts) -> str:
    """Stable short id over the given parts (order-sensitive)."""
    joined = "␟".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class EvalRecord:
    record_id: str
    kind: str                      # "channel_turn" | "pass"
    pass_mode: str = ""
    window_start: str = ""
    window_end: str = ""
    rc: int | None = None
    user_said: str = ""            # channel_turn: the user's message
    agent_considered: str = ""     # channel_turn: the agent's considered reply(ies) (say/ask, not intent)
    prior_agent: str = ""          # channel_turn: the agent's preceding [out] turn (context)
    prior_tag: str = ""            # channel_turn: that turn's tag (e.g. enumerated-tasks)
    narrative: str = ""            # pass: the pass stdout narrative

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(obj: dict) -> "EvalRecord":
        fields = {f for f in EvalRecord.__dataclass_fields__}
        return EvalRecord(**{k: v for k, v in obj.items() if k in fields})


@dataclass(frozen=True)
class Finding:
    category: str                  # context-loss | missed-action | redundant-recheck | …
    severity: str                  # one of SEVERITIES
    source: str                    # "deterministic" | "llm-judge"
    summary: str
    evidence: str
    suggestion: str
    target: str                    # file/area to improve (clustering key)
    record_id: str = ""
    pass_mode: str = ""
    window: str = ""
    confidence: float = 1.0

    @property
    def finding_id(self) -> str:
        return fingerprint(self.category, self.target, self.record_id, self.evidence)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["finding_id"] = self.finding_id
        return data

    @staticmethod
    def from_dict(obj: dict) -> "Finding":
        fields = {f for f in Finding.__dataclass_fields__}
        return Finding(**{k: v for k, v in obj.items() if k in fields})


@dataclass
class ImprovementCandidate:
    candidate_id: str
    category: str
    target: str
    severity_max: str
    occurrences: int
    summary: str
    suggestion: str
    exemplars: list = field(default_factory=list)   # up to a few evidence strings
    finding_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
