#!/usr/bin/env python3
"""Regression tests for the listing state-machine step rename (awaiting_listing_inputs).

Context: the three sub-steps awaiting_price -> awaiting_floor -> awaiting_details were collapsed
into a single `awaiting_listing_inputs` step. A live data/listing_session.json persisted on an OLD
step name was then stranded: skills/channel/listing.md no longer had a handler for it, so every
channel pass loaded the session, found no matching step, and could not advance (the "listing is
stuck" bug). These tests lock in the fix:

  (1) the combined step exists and the old per-step handlers are gone,
  (2) the routing carries a legacy-step compat shim so a session persisted by an older version (or
      a pass killed mid-rename) resumes instead of stranding,
  (3) the combined handler resolves a bare-number reply by the field still MISSING (so re-asking the
      floor on a session that already has a list price does not overwrite the price).

    python3 tests/test_listing_step_migration.py   # or: pytest tests/test_listing_step_migration.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LISTING_MD = ROOT / "skills" / "channel" / "listing.md"
LEGACY_STEPS = ("awaiting_price", "awaiting_floor", "awaiting_details")


def _listing_text() -> str:
    return LISTING_MD.read_text()


def test_combined_step_exists():
    """The collapsed step is the canonical one the flow persists and handles."""
    text = _listing_text()
    assert "awaiting_listing_inputs" in text
    assert "### awaiting_listing_inputs" in text  # a real handler section, not just a mention


def test_no_legacy_step_handlers():
    """The old per-step handler sections are gone (collapsed into awaiting_listing_inputs)."""
    text = _listing_text()
    for legacy in LEGACY_STEPS:
        assert f"### {legacy}" not in text, (
            f"{legacy} should be collapsed into awaiting_listing_inputs, not a standalone handler"
        )


def test_legacy_step_compat_shim_present():
    """Renaming a persisted step must not strand in-flight sessions. The routing must explicitly
    map every legacy step name to awaiting_listing_inputs so a stranded session self-heals.
    This FAILS if the compat shim is removed (the exact 'listing is stuck' regression)."""
    text = _listing_text()
    assert "Legacy step compat" in text, "routing is missing the legacy-step migration shim"
    for legacy in LEGACY_STEPS:
        assert legacy in text, f"legacy step {legacy!r} not acknowledged by the compat shim"


def test_resume_uses_missing_field_for_bare_number():
    """On a resumed/migrated session (e.g. list_price already set, floor missing) a bare-number
    reply must fill the MISSING field, not blindly become the list price. Guards the second bug:
    re-asking the floor would otherwise overwrite the captured list price."""
    text = _listing_text()
    assert "RESUMING a partial session" in text, "parse rules missing the resume/partial-session case"
    # The resume rule must be explicit that a bare number maps to the floor when the price is set.
    assert "a bare number is the FLOOR" in text


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures.append(name)
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
