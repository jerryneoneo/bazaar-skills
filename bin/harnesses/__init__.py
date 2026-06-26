"""Harness abstraction — detect/select the agent runtime (Claude Code, Codex, …).

`detect_all()` probes every known harness (for the install-time "which are you signed into?" step).
`get_harness(name)` returns a specific harness; with name=None it autodetects, preferring a
signed-in one. Callers never branch on the harness directly — they go through the Harness interface.

This is the agent-runtime analogue of bin/platforms/ (which abstracts the OS).
"""

from __future__ import annotations

from .base import Harness, UnknownHarness

_REGISTRY = {}


def _load():
    if _REGISTRY:
        return _REGISTRY
    from .claude_code import ClaudeCodeHarness
    from .codex import CodexHarness
    for cls in (ClaudeCodeHarness, CodexHarness):
        _REGISTRY[cls.name] = cls
    return _REGISTRY


def detect_all() -> list[dict]:
    """Detection result for every known harness (cheap probes; the install step shows these)."""
    return [cls().detect() for cls in _load().values()]


def get_harness(name: str | None = None) -> Harness:
    reg = _load()
    if name:
        if name not in reg:
            raise UnknownHarness(f"unknown harness {name!r} (known: {', '.join(reg)})")
        return reg[name]()
    # Autodetect: prefer a signed-in harness, then any present CLI, else error (caller should ask).
    detected = detect_all()
    signed = [d for d in detected if d["signed_in"]]
    present = [d for d in detected if d["cli_present"]]
    pick = (signed or present)
    if not pick:
        raise UnknownHarness("no agent harness detected (install Claude Code or Codex)")
    return reg[pick[0]["name"]]()


__all__ = ["Harness", "UnknownHarness", "detect_all", "get_harness"]
