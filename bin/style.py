#!/usr/bin/env python3
"""style.py — the user's STYLE/PERSONA profile + firmness presets + opt-in learning proposals.

This is the single source of truth for *how the user likes to deal*: the voice/persona applied
when composing messages (read at compose time by skills/style.md), and the sell-side firmness that
derives the deterministic negotiation knobs read by bin/negotiate.py.

Two concerns, one small domain module (mirrors floor_gate.py / budget_gate.py — cohesive, testable):

  • PROFILE  data/style.json  — voice + negotiation.sell_firmness + learning mode (committed default).
  • LEARNING data/style_proposals.jsonl — append-only proposals from /pause corrections and /bazaar-eval
            tone findings. Nothing here ever rewrites the profile silently; the user applies a proposal
            via `/bazaar -> style` (or `style.py apply`). `learning` ('off'|'suggest'|'auto') is the gate.

Invariants live in skills/style.md (never leak floor/budget, never claim human, never em-dash, cheeky
not abusive). This module only stores/validates/derives — it composes nothing.

Usage:
  style.py show                       # the resolved profile (defaults backfilled)
  style.py validate [--file PATH]     # 0 ok · 3 invalid
  style.py knobs                      # the resolved sell negotiation knobs (debug / menu display)
  style.py set-firmness <level>       # soft|balanced|firm|hardline (menu)
  style.py set --field F --value V    # F = voice.persona|voice.tone|... |learning (menu)
  style.py propose --field F --value V --rationale R [--evidence E] --source correction|eval
  style.py proposals [--all]          # pending (or all) learning proposals as JSON
  style.py apply --id ID              # apply one proposal to style.json
Exit: 0 ok · 2 bad input · 3 data missing/invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STYLE_PATH = DATA_DIR / "style.json"
PROPOSALS_PATH = DATA_DIR / "style_proposals.jsonl"

# --- enums (the editable surface) -----------------------------------------------------------------
TONES = ("friendly", "warm", "neutral", "terse")
HUMORS = ("none", "light", "playful")
LOWBALL_RESPONSES = ("polite", "firm", "cheeky")  # how a deflect_lowball is WORDED (never a number)
FIRMNESS_LEVELS = ("soft", "balanced", "firm", "hardline")
LEARNING_MODES = ("off", "suggest", "auto")

# Firmness -> the three GLOBAL negotiation knobs that bin/negotiate.py reads from config (per-item
# auto_counter_step/rounds live in the floor record and are untouched here). 'balanced' is identical
# to negotiate.DEFAULTS, so the default profile reproduces today's behavior exactly.
FIRMNESS_PRESETS = {
    "soft":     {"min_offer_ratio": 0.5, "lowball_cap": 5, "max_counters": 3},
    "balanced": {"min_offer_ratio": 0.6, "lowball_cap": 3, "max_counters": 2},
    "firm":     {"min_offer_ratio": 0.7, "lowball_cap": 2, "max_counters": 1},
    "hardline": {"min_offer_ratio": 0.8, "lowball_cap": 1, "max_counters": 1},
}
NEGOTIATION_DEFAULTS = FIRMNESS_PRESETS["balanced"]

DEFAULT_STYLE = {
    "voice": {"persona": "", "tone": "friendly", "humor": "light", "lowball_response": "polite"},
    "negotiation": {"sell_firmness": "balanced"},
    "learning": "suggest",
}

# Dotted field paths the menu / proposals may set, mapped to their allowed values (None = free text).
_SETTABLE = {
    "voice.persona": None,
    "voice.tone": TONES,
    "voice.humor": HUMORS,
    "voice.lowball_response": LOWBALL_RESPONSES,
    "negotiation.sell_firmness": FIRMNESS_LEVELS,
    "learning": LEARNING_MODES,
}
# eval Finding categories that are about HOW we talk -> a voice proposal (see bin/eval_judge.py).
_STYLE_FINDING_CATEGORIES = {"tone-voice", "unhelpful-ux"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- profile load / validate ----------------------------------------------------------------------
def _merge_defaults(obj: dict) -> dict:
    """Return a NEW profile: defaults with any present keys overlaid (immutable; backfills gaps)."""
    merged = deepcopy(DEFAULT_STYLE)
    if isinstance(obj.get("voice"), dict):
        merged["voice"] = {**merged["voice"], **{k: v for k, v in obj["voice"].items()
                                                  if k in DEFAULT_STYLE["voice"]}}
    if isinstance(obj.get("negotiation"), dict):
        merged["negotiation"] = {**merged["negotiation"],
                                 **{k: v for k, v in obj["negotiation"].items()
                                    if k in DEFAULT_STYLE["negotiation"]}}
    if "learning" in obj:
        merged["learning"] = obj["learning"]
    return merged


def load_style() -> dict:
    """Read data/style.json, fail-open to DEFAULT_STYLE on missing/invalid. Always backfills gaps."""
    try:
        raw = json.loads(STYLE_PATH.read_text())
    except (OSError, ValueError):
        return deepcopy(DEFAULT_STYLE)
    if not isinstance(raw, dict):
        return deepcopy(DEFAULT_STYLE)
    return _merge_defaults(raw)


def validate_style(obj) -> list[str]:
    """Return a list of human-readable errors ([] if valid). Strict on shape + enums."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["style profile must be a JSON object"]
    voice = obj.get("voice", {})
    if not isinstance(voice, dict):
        errors.append("voice must be an object")
        voice = {}
    if not isinstance(voice.get("persona", ""), str):
        errors.append("voice.persona must be a string")
    for key, allowed in (("tone", TONES), ("humor", HUMORS),
                         ("lowball_response", LOWBALL_RESPONSES)):
        val = voice.get(key, DEFAULT_STYLE["voice"][key])
        if val not in allowed:
            errors.append(f"voice.{key} must be one of {allowed}, got {val!r}")
    neg = obj.get("negotiation", {})
    if not isinstance(neg, dict):
        errors.append("negotiation must be an object")
        neg = {}
    firm = neg.get("sell_firmness", "balanced")
    if firm not in FIRMNESS_LEVELS:
        errors.append(f"negotiation.sell_firmness must be one of {FIRMNESS_LEVELS}, got {firm!r}")
    learning = obj.get("learning", "suggest")
    if learning not in LEARNING_MODES:
        errors.append(f"learning must be one of {LEARNING_MODES}, got {learning!r}")
    return errors


# --- firmness -> knobs (read by bin/negotiate.py) -------------------------------------------------
def firmness_knobs(style: dict | None = None) -> dict:
    """The three sell knobs for the profile's firmness level. Unknown level -> balanced (safe)."""
    style = style if style is not None else load_style()
    level = style.get("negotiation", {}).get("sell_firmness", "balanced")
    return dict(FIRMNESS_PRESETS.get(level, FIRMNESS_PRESETS["balanced"]))


def resolve_knobs(config_knobs: dict, style: dict | None = None) -> dict:
    """Resolve the negotiation knobs: explicit config value > firmness-derived > hard default.

    `config_knobs` is the raw config.json dict (it may omit the firmness-controlled keys, which is
    the normal case — they were removed from the shipped default so firmness can drive them)."""
    base = dict(NEGOTIATION_DEFAULTS)
    base.update(firmness_knobs(style))
    return {k: config_knobs.get(k, base[k]) for k in NEGOTIATION_DEFAULTS}


# --- profile mutation (immutable writes) ----------------------------------------------------------
def _set_path(profile: dict, dotted: str, value) -> dict:
    """Return a NEW profile with the dotted path set (e.g. 'voice.tone'). No in-place mutation."""
    new = deepcopy(profile)
    parts = dotted.split(".")
    node = new
    for key in parts[:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value
    return new


def set_field(field: str, value) -> dict:
    """Validate + write a single field to style.json. Raises ValueError on a bad field/value."""
    if field not in _SETTABLE:
        raise ValueError(f"unknown field {field!r}; settable: {sorted(_SETTABLE)}")
    allowed = _SETTABLE[field]
    if allowed is not None and value not in allowed:
        raise ValueError(f"{field} must be one of {allowed}, got {value!r}")
    updated = _set_path(load_style(), field, value)
    problems = validate_style(updated)
    if problems:
        raise ValueError("; ".join(problems))
    _write_style(updated)
    return updated


def _write_style(profile: dict) -> None:
    STYLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STYLE_PATH.write_text(json.dumps(profile, indent=2) + "\n")


# --- learning proposals (append-only; opt-in) -----------------------------------------------------
def _proposal_id(field: str, proposed, ts: str) -> str:
    import hashlib
    return hashlib.sha256(f"{field}␟{proposed}␟{ts}".encode()).hexdigest()[:12]


def _read_proposals() -> list[dict]:
    if not PROPOSALS_PATH.exists():
        return []
    out = []
    for line in PROPOSALS_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _write_proposals(rows: list[dict]) -> None:
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def record_proposal(field: str, proposed, rationale: str, evidence: str, source: str) -> dict:
    """Append a learning proposal. Suppressed when learning=='off'. Never rewrites the profile."""
    if load_style().get("learning") == "off":
        return {"skipped": True, "reason": "learning is off"}
    ts = _now()
    current = None
    try:
        node = load_style()
        for key in field.split("."):
            node = node[key]
        current = node
    except (KeyError, TypeError):
        current = None
    proposal = {"id": _proposal_id(field, proposed, ts), "field": field, "current": current,
                "proposed": proposed, "rationale": rationale, "evidence": evidence,
                "source": source, "ts": ts, "applied": False}
    _write_proposals(_read_proposals() + [proposal])
    return proposal


def load_proposals(include_applied: bool = False) -> list[dict]:
    rows = _read_proposals()
    return rows if include_applied else [r for r in rows if not r.get("applied")]


def apply_proposal(proposal_id: str) -> dict:
    """Apply a pending proposal to style.json and mark it applied. Refuses an invalid value."""
    rows = _read_proposals()
    target = next((r for r in rows if r.get("id") == proposal_id and not r.get("applied")), None)
    if target is None:
        return {"applied": False, "error": f"no pending proposal with id {proposal_id!r}"}
    try:
        set_field(target["field"], target["proposed"])
    except ValueError as exc:
        return {"applied": False, "error": str(exc)}
    updated = [{**r, "applied": True, "applied_at": _now()} if r.get("id") == proposal_id else r
               for r in rows]
    _write_proposals(updated)
    return {"applied": True, "field": target["field"], "value": target["proposed"]}


def proposals_from_findings(findings) -> int:
    """Turn eval tone/voice findings into voice proposals (one general 'persona' nudge each).

    Conservative: the judge's free-text suggestion can't be auto-mapped to an enum, so we record a
    persona-note proposal carrying the suggestion as evidence for the user to act on. Returns count."""
    count = 0
    for f in findings:
        category = (f.get("category") if isinstance(f, dict) else getattr(f, "category", "")) or ""
        if category not in _STYLE_FINDING_CATEGORIES:
            continue
        evidence = f.get("evidence") if isinstance(f, dict) else getattr(f, "evidence", "")
        suggestion = f.get("suggestion") if isinstance(f, dict) else getattr(f, "suggestion", "")
        res = record_proposal(field="voice.persona", proposed=suggestion or "(see evidence)",
                              rationale="eval flagged a tone/voice issue", evidence=str(evidence),
                              source="eval")
        if not res.get("skipped"):
            count += 1
    return count


# --- CLI ------------------------------------------------------------------------------------------
def _cmd_show(ns) -> int:
    print(json.dumps(load_style(), indent=2))
    return 0


def _cmd_validate(ns) -> int:
    path = Path(ns.file) if ns.file else STYLE_PATH
    try:
        obj = json.loads(path.read_text())
    except OSError:
        print(json.dumps({"ok": False, "errors": [f"cannot read {path}"]}))
        return 3
    except ValueError as exc:
        print(json.dumps({"ok": False, "errors": [f"invalid JSON: {exc}"]}))
        return 3
    errors = validate_style(obj)
    print(json.dumps({"ok": not errors, "errors": errors}))
    return 0 if not errors else 3


def _cmd_knobs(ns) -> int:
    cfg = {}
    config_path = DATA_DIR / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
        except ValueError:
            cfg = {}
    print(json.dumps(resolve_knobs(cfg)))
    return 0


def _cmd_set_firmness(ns) -> int:
    try:
        set_field("negotiation.sell_firmness", ns.level)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "sell_firmness": ns.level, "knobs": firmness_knobs()}))
    return 0


def _cmd_set(ns) -> int:
    try:
        set_field(ns.field, ns.value)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "field": ns.field, "value": ns.value}))
    return 0


def _cmd_propose(ns) -> int:
    res = record_proposal(field=ns.field, proposed=ns.value, rationale=ns.rationale,
                          evidence=ns.evidence, source=ns.source)
    print(json.dumps(res))
    return 0


def _cmd_proposals(ns) -> int:
    print(json.dumps(load_proposals(include_applied=ns.all), indent=2))
    return 0


def _cmd_apply(ns) -> int:
    res = apply_proposal(ns.id)
    print(json.dumps(res))
    return 0 if res.get("applied") else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="style.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").set_defaults(func=_cmd_show)
    v = sub.add_parser("validate")
    v.add_argument("--file", default="")
    v.set_defaults(func=_cmd_validate)
    sub.add_parser("knobs").set_defaults(func=_cmd_knobs)
    sf = sub.add_parser("set-firmness")
    sf.add_argument("level", choices=FIRMNESS_LEVELS)
    sf.set_defaults(func=_cmd_set_firmness)
    st = sub.add_parser("set")
    st.add_argument("--field", required=True)
    st.add_argument("--value", required=True)
    st.set_defaults(func=_cmd_set)
    pr = sub.add_parser("propose")
    pr.add_argument("--field", required=True)
    pr.add_argument("--value", required=True)
    pr.add_argument("--rationale", required=True)
    pr.add_argument("--evidence", default="")
    pr.add_argument("--source", default="correction", choices=["correction", "eval"])
    pr.set_defaults(func=_cmd_propose)
    pl = sub.add_parser("proposals")
    pl.add_argument("--all", action="store_true")
    pl.set_defaults(func=_cmd_proposals)
    ap = sub.add_parser("apply")
    ap.add_argument("--id", required=True)
    ap.set_defaults(func=_cmd_apply)
    return p


def main(argv) -> int:
    try:
        ns = build_parser().parse_args(argv[1:])
    except SystemExit:
        return 2
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
