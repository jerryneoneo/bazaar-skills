#!/usr/bin/env python3
"""Tests for style.py — the user style/persona profile + firmness presets + learning proposals.

    python3 tests/test_style.py

Same standalone idiom as test_negotiate.py (check() + a __main__ that exits non-zero on failure),
so it gates in CI without depending on pytest collection.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import style  # noqa: E402
import negotiate  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _isolate(tmp):
    """Point style + negotiate at temp data files so tests never touch real state."""
    style.STYLE_PATH = tmp / "style.json"
    style.PROPOSALS_PATH = tmp / "style_proposals.jsonl"
    negotiate.CONFIG_PATH = tmp / "config.json"


def test_defaults_balanced_equals_negotiate_defaults():
    print("default firmness 'balanced' reproduces today's hard knobs (backward compatible):")
    check("balanced preset == negotiate DEFAULTS",
          style.FIRMNESS_PRESETS["balanced"] == negotiate.DEFAULTS)
    check("NEGOTIATION_DEFAULTS == balanced preset",
          style.NEGOTIATION_DEFAULTS == style.FIRMNESS_PRESETS["balanced"])


def test_load_style_fail_open():
    print("load_style fails open to defaults when the file is missing or junk:")
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        check("missing file -> defaults", style.load_style() == style.DEFAULT_STYLE)
        style.STYLE_PATH.write_text("{ not json")
        check("malformed file -> defaults (no raise)", style.load_style() == style.DEFAULT_STYLE)
        # partial file: missing keys are backfilled from defaults
        style.STYLE_PATH.write_text(json.dumps({"voice": {"tone": "terse"}}))
        loaded = style.load_style()
        check("partial file backfills missing voice keys", loaded["voice"]["humor"] == "light")
        check("partial file keeps the set value", loaded["voice"]["tone"] == "terse")
        check("partial file backfills negotiation", loaded["negotiation"]["sell_firmness"] == "balanced")


def test_validate_style():
    print("validate_style accepts the default and rejects bad enums/shape:")
    check("default is valid", style.validate_style(style.DEFAULT_STYLE) == [])
    bad_tone = {"voice": {"persona": "", "tone": "spicy", "humor": "light",
                          "lowball_response": "polite"},
                "negotiation": {"sell_firmness": "balanced"}, "learning": "suggest"}
    check("bad tone rejected", style.validate_style(bad_tone) != [])
    bad_firm = json.loads(json.dumps(style.DEFAULT_STYLE))
    bad_firm["negotiation"]["sell_firmness"] = "savage"
    check("bad firmness rejected", style.validate_style(bad_firm) != [])
    bad_learn = json.loads(json.dumps(style.DEFAULT_STYLE))
    bad_learn["learning"] = "always"
    check("bad learning mode rejected", style.validate_style(bad_learn) != [])
    check("non-dict rejected", style.validate_style([]) != [])
    check("non-string persona rejected",
          style.validate_style({"voice": {"persona": 5, "tone": "friendly", "humor": "light",
                                           "lowball_response": "polite"},
                                "negotiation": {"sell_firmness": "balanced"},
                                "learning": "suggest"}) != [])


def test_firmness_knobs():
    print("each firmness level maps to the expected sell knobs:")
    for level in style.FIRMNESS_LEVELS:
        knobs = style.firmness_knobs({"negotiation": {"sell_firmness": level}})
        check(f"{level} -> exactly the 3 negotiation knobs",
              set(knobs) == set(style.NEGOTIATION_DEFAULTS))
    check("hardline holds harder than soft (higher floor ratio)",
          style.firmness_knobs({"negotiation": {"sell_firmness": "hardline"}})["min_offer_ratio"]
          > style.firmness_knobs({"negotiation": {"sell_firmness": "soft"}})["min_offer_ratio"])
    check("hardline disengages on lowballs faster (lower cap)",
          style.firmness_knobs({"negotiation": {"sell_firmness": "hardline"}})["lowball_cap"]
          < style.firmness_knobs({"negotiation": {"sell_firmness": "soft"}})["lowball_cap"])
    check("unknown level falls back to balanced",
          style.firmness_knobs({"negotiation": {"sell_firmness": "???"}})
          == style.FIRMNESS_PRESETS["balanced"])


def test_resolve_knobs_precedence():
    print("resolve order: explicit config knob > firmness-derived > defaults:")
    firm = {"negotiation": {"sell_firmness": "firm"}}
    # no explicit config -> firmness drives
    r = style.resolve_knobs({}, firm)
    check("firmness drives when config is silent",
          r["min_offer_ratio"] == style.FIRMNESS_PRESETS["firm"]["min_offer_ratio"])
    # explicit config knob overrides firmness (power-user escape hatch)
    r2 = style.resolve_knobs({"min_offer_ratio": 0.42}, firm)
    check("explicit config overrides firmness", r2["min_offer_ratio"] == 0.42)
    check("untouched knob still from firmness",
          r2["lowball_cap"] == style.FIRMNESS_PRESETS["firm"]["lowball_cap"])


def test_negotiate_cfg_uses_firmness():
    print("negotiate._cfg() honors firmness when config omits the knobs (the live wiring):")
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        negotiate.CONFIG_PATH.write_text(json.dumps({"max_actions_per_hour": 12}))  # no firmness knobs
        style.STYLE_PATH.write_text(json.dumps({**style.DEFAULT_STYLE,
                                                "negotiation": {"sell_firmness": "hardline"}}))
        cfg = negotiate._cfg()
        check("_cfg picks up hardline min_offer_ratio",
              cfg["min_offer_ratio"] == style.FIRMNESS_PRESETS["hardline"]["min_offer_ratio"])
        # explicit config knob still wins
        negotiate.CONFIG_PATH.write_text(json.dumps({"min_offer_ratio": 0.55}))
        check("_cfg lets explicit config win", negotiate._cfg()["min_offer_ratio"] == 0.55)
        check("_cfg still has all 3 keys", set(negotiate._cfg()) == set(negotiate.DEFAULTS))


def test_proposals_roundtrip():
    print("proposals: append (does NOT mutate style.json) -> list -> apply (writes style.json):")
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        style.STYLE_PATH.write_text(json.dumps(style.DEFAULT_STYLE))
        prop = style.record_proposal(field="voice.lowball_response", proposed="cheeky",
                                     rationale="user said give lowballers a hard time",
                                     evidence="be more savage to lowballers", source="correction")
        check("proposal returned with an id", bool(prop.get("id")))
        check("appending a proposal did NOT change style.json",
              style.load_style()["voice"]["lowball_response"] == "polite")
        pending = style.load_proposals()
        check("one pending proposal listed", len(pending) == 1)
        applied = style.apply_proposal(prop["id"])
        check("apply succeeded", applied.get("applied") is True)
        check("style.json now reflects the change",
              style.load_style()["voice"]["lowball_response"] == "cheeky")
        check("no pending proposals remain", style.load_proposals() == [])
        # applying an invalid value is refused and does not corrupt style.json
        bad = style.record_proposal(field="negotiation.sell_firmness", proposed="savage",
                                    rationale="x", evidence="y", source="eval")
        res = style.apply_proposal(bad["id"])
        check("invalid proposal rejected", res.get("applied") is not True)
        check("style.json unchanged after rejected apply",
              style.load_style()["negotiation"]["sell_firmness"] == "balanced")


def test_learning_off_skips_proposals():
    print("learning='off' suppresses proposal capture from corrections/eval:")
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        style.STYLE_PATH.write_text(json.dumps({**style.DEFAULT_STYLE, "learning": "off"}))
        prop = style.record_proposal(field="voice.tone", proposed="terse", rationale="r",
                                     evidence="e", source="correction")
        check("record_proposal skipped when learning off", prop.get("skipped") is True)
        check("nothing written", style.load_proposals() == [])


def test_proposals_from_findings():
    print("eval tone/voice findings become style proposals; unrelated findings do not:")
    with tempfile.TemporaryDirectory() as d:
        _isolate(Path(d))
        style.STYLE_PATH.write_text(json.dumps(style.DEFAULT_STYLE))
        findings = [
            {"category": "tone-voice", "evidence": "reply was robotic and cold",
             "suggestion": "warmer tone", "severity": "medium"},
            {"category": "missed-action", "evidence": "ignored a buyer", "suggestion": "reply",
             "severity": "high"},
        ]
        n = style.proposals_from_findings(findings)
        check("exactly one style proposal emitted (the tone-voice one)", n == 1)
        pending = style.load_proposals()
        check("the proposal is sourced from eval", pending and pending[0]["source"] == "eval")


def test_install_validate_style_file():
    print("install.validate_style_file accepts a good profile and rejects a malformed one:")
    import install  # noqa: E402  local bin/ module
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "style.json"
        good.write_text(json.dumps(style.DEFAULT_STYLE))
        check("valid profile -> ok", install.validate_style_file(good) == "ok")
        bad = Path(d) / "bad.json"
        bad.write_text(json.dumps({**style.DEFAULT_STYLE, "learning": "always"}))
        check("bad enum -> invalid", install.validate_style_file(bad).startswith("invalid"))
        junk = Path(d) / "junk.json"
        junk.write_text("{ not json")
        check("malformed JSON -> invalid", install.validate_style_file(junk).startswith("invalid"))


if __name__ == "__main__":
    print("style tests\n")
    test_defaults_balanced_equals_negotiate_defaults()
    test_load_style_fail_open()
    test_validate_style()
    test_firmness_knobs()
    test_resolve_knobs_precedence()
    test_negotiate_cfg_uses_firmness()
    test_proposals_roundtrip()
    test_learning_off_skips_proposals()
    test_proposals_from_findings()
    test_install_validate_style_file()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
