#!/usr/bin/env python3
"""eval_checks.py — deterministic ($0, no-LLM) evaluation checks over the corpus.

Each check is a pure function (Corpus -> list[Finding]) with confidence 1.0. The full set
(see ALL_CHECKS) catches these defect classes without any model cost:
  • missed-action      — unread buyer messages on a market with no recent reply (the FB-unread bug)
  • context-loss       — a follow-up ("do all tasks") answered with a re-check instead of acting
  • redundant-recheck  — repeated "let me check" acks with no action between
  • pass-failure       — a pass exited non-zero (rc∉{0,143}) from an unrecognised crash
  • turn-exhaustion    — a pass spent its whole --max-turns cap and was killed (a design issue)
  • session-limit      — a pass was cut off by Claude's account usage cap (external transient)
  • silent-buyer-pass  — a buyer pass produced no narrative while unread messages waited

  pass-failure / turn-exhaustion / session-limit are split deliberately: each needs a different
  fix and a different `target`, so they must not collapse into one undifferentiated bucket.
  • secret-leak        — a buyer-facing message structurally reveals a floor/budget figure
  • banned-copy        — an outbound refuses with "no meetups"/"ship only" (reply-pipeline §4)
  • meetup-loop        — a meetup request answered with a delivery/area loop, not a close escalation
  • untracked-unhandled — an inbox-sweep takeover marked `managed` with no thread seeded behind it
"""

from __future__ import annotations

import re

from eval_schema import Finding

OK_RETURN_CODES = (0, 143)   # 0 = done; 143 = SIGTERM preempt (a deliberate, clean stop)
_CLIP = 240

# A failed pass fails in one of a few distinct ways, and they need DIFFERENT fixes. Conflating
# them (e.g. blaming a turn-exhaustion on a missing inbox recipe) sends the operator down the
# wrong path — exactly what the first eval report did. Classify the narrative so each failure
# routes to its real cause.
MAX_TURNS_RE = re.compile(r"reached max turns", re.IGNORECASE)
SESSION_LIMIT_RE = re.compile(r"\b(session|usage)\s+limit\b|hit your\b[^.\n]{0,24}\blimit\b", re.IGNORECASE)

RECHECK_RE = re.compile(
    r"\b(let me check|let me take a look|let me look into|let me pull up|let me see what|"
    r"what needs doing|checking what)\b", re.IGNORECASE)

_FOLLOWUP_RES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^\s*do (all|them all|everything|all tasks|all of (them|it))\b",
        r"^\s*take over all\b",
        r"^\s*(both|all)\s*$",
        r"^\s*(yes|yep|yeah|ok(ay)?|sure|go ahead|proceed)[\s.!,]*$",
        r"^\s*auto\b",
        r"^\s*(the )?(first|second|third|last) one\b",
        r"^\s*#?\d+\s*$",
        r"^\s*that one\b",
    )
]

SECRET_LEAK_RES = [
    re.compile(r"\b(floor|reserve price|my (lowest|minimum)|absolute minimum)\b[^.\n]{0,18}\$?\b\d{2,}\b",
               re.IGNORECASE),
    re.compile(r"\bmax(imum)?\s+budget\b[^.\n]{0,18}\$?\b\d{2,}\b", re.IGNORECASE),
]

# Buyer-facing copy the seller agent must never send: a meetup request escalates the close
# (reply-pipeline.md §3 → §3b), it is never refused with a ship-only line (§4 INVARIANT).
FORBIDDEN_COPY_RE = re.compile(r"\bno\s*meet[\s-]?ups?\b|\bship[\s-]?only\b", re.IGNORECASE)

# A buyer asking to meet / self-collect / pay cash on pickup. Must route to the close choice.
MEETUP_REQUEST_RE = re.compile(
    r"\b(can\s+(?:we\s+|i\s+|u\s+|you\s+)?(meet|collect)|meet\s*up|meetup|"
    r"meet\s+(at|me|mrt|here|there)|self[\s-]?collect|self[\s-]?pickup|pick\s*up|pickup|"
    r"collect\s+(it|in\s+person|myself)|cash\s+on)\b",
    re.IGNORECASE)

# The old loop: asking the buyer for their area / nearest station, or quoting a manual delivery
# fee, after a meetup request, instead of escalating the close.
AREA_GRIND_RE = re.compile(
    r"\b(what\s+area|which\s+area|your\s+area|let\s+me\s+know\s+(your|where)|where\s+are\s+you|"
    r"nearest\s+mrt|delivery\s+(fee|runs)|how\s+much\s+for\s+delivery|deliver\s+to\s+your\s+area)\b",
    re.IGNORECASE)

# A thread in any of these final states reached a close or escalation, so it is not stuck looping.
CLOSED_STATUSES = frozenset({"escalated", "agreed", "handover", "lost", "held"})


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _CLIP else text[:_CLIP] + "…"


def _is_followup(text: str) -> bool:
    return any(rx.search(text or "") for rx in _FOLLOWUP_RES)


def classify_failure(narrative: str) -> str:
    """Classify a failed pass's narrative into its real cause:
      'session-limit'   — Claude account usage/session cap hit (EXTERNAL transient, not a code bug)
      'turn-exhaustion' — the pass spent its whole --max-turns cap and was killed before finishing
      'other'           — an unrecognised crash; the narrative is the lead
    Session-limit is checked first: a quota stop can also mention turns, but the quota is the cause."""
    text = narrative or ""
    if SESSION_LIMIT_RE.search(text):
        return "session-limit"
    if MAX_TURNS_RE.search(text):
        return "turn-exhaustion"
    return "other"


def check_missed_enquiry(corpus) -> list[Finding]:
    """Unread buyer messages on a market, but no recent reply and no open escalation → the agent
    should be replying and isn't (the reported Facebook-enquiries-missed defect)."""
    findings = []
    for market, count in sorted(corpus.peek.items()):
        if not count or count <= 0:
            continue
        if corpus.outbound_recent.get(market, 0) > 0 or market in corpus.escalation_markets:
            continue
        findings.append(Finding(
            category="missed-action", severity="high", source="deterministic",
            summary=f"{count} unread buyer message(s) on {market} with no recent reply",
            evidence=(f"buyer_peek_state[{market}]={count}; 0 outbound to {market} in the last "
                      f"{corpus.unanswered_hours:g}h; no open escalation for {market}"),
            suggestion=(f"The buyer pass isn't replying on {market}. Confirm the inbox read-recipe "
                        f"in skills/listing-flows/{market}.md actually opens the inbox and reads "
                        f"threads (a missing recipe makes the pass fail silently)."),
            target=f"skills/listing-flows/{market}.md"))
    return findings


def check_context_loss(corpus) -> list[Finding]:
    """A no-signal follow-up answered with a re-check ack instead of acting on what was offered."""
    findings = []
    for r in corpus.channel_turns:
        if not _is_followup(r.user_said) or not r.agent_considered:
            continue
        if RECHECK_RE.search(r.agent_considered):
            findings.append(Finding(
                category="context-loss", severity="high", source="deterministic",
                summary="Follow-up answered with a re-check instead of acting",
                evidence=(f'user: "{_clip(r.user_said)}" → agent: "{_clip(r.agent_considered)}"' +
                          (f' (prior [out]: "{_clip(r.prior_agent)}")' if r.prior_agent else "")),
                suggestion=("Resolve no-signal follow-ups against the RECENT CONVERSATION block "
                            "(bazaar-run.md FOLLOW-UP precedence-0): act on the enumerated tasks "
                            "instead of re-deriving state."),
                target="bin/harness_run.py:CHANNEL_PROMPT", record_id=r.record_id, window=r.window_start))
    return findings


def check_redundant_recheck(corpus) -> list[Finding]:
    """>= 2 considered (non-intent) 'let me check' acks with no substantive inbound between them."""
    findings = []
    streak, first_text = 0, ""
    for turn in corpus.turns:
        direction, kind = turn.get("dir"), turn.get("kind")
        if direction == "in":
            streak = 0  # a real inbound message breaks the streak
            continue
        if direction == "out" and kind != "intent":
            if RECHECK_RE.search(turn.get("text", "")):
                streak += 1
                if streak == 1:
                    first_text = turn.get("text", "")
                elif streak == 2:
                    findings.append(Finding(
                        category="redundant-recheck", severity="medium", source="deterministic",
                        summary="Repeated 'let me check' acks with no action between them",
                        evidence=f'e.g. "{_clip(first_text)}" then "{_clip(turn.get("text", ""))}"',
                        suggestion=("Collapse consecutive status re-checks into one action; the "
                                    "daemon already sends a one-line intent pre-ack each pass."),
                        target="bin/harness_run.py:CHANNEL_PROMPT"))
            else:
                streak = 0
    return findings


def check_pass_failure(corpus) -> list[Finding]:
    findings = []
    for pr in corpus.pass_records:
        if pr.rc is None or pr.rc in OK_RETURN_CODES:
            continue
        evidence = _clip(pr.narrative) or f"{pr.pass_mode} pass rc={pr.rc} ({pr.window_start})"
        kind = classify_failure(pr.narrative)
        if kind == "session-limit":
            findings.append(Finding(
                category="session-limit", severity="medium", source="deterministic",
                summary=f"{pr.pass_mode} pass stopped: Claude usage/session limit reached",
                evidence=evidence,
                suggestion=("EXTERNAL transient, not a code defect — the pass was cut off by Claude's "
                            "account usage cap and resumes after the reset. If this recurs, space "
                            "passes out or use lighter models/turn caps to stay under the limit."),
                target="external:session-limit", pass_mode=pr.pass_mode, window=pr.window_start))
        elif kind == "turn-exhaustion":
            buyer = pr.pass_mode == "buyer"
            findings.append(Finding(
                category="turn-exhaustion", severity="high", source="deterministic",
                summary=f"{pr.pass_mode} pass hit its max-turns cap before finishing",
                evidence=evidence,
                suggestion=("The pass spent its whole turn cap and was killed (rc=1) before writing a "
                            "summary. Raising the cap won't fix it — give the pass a turn-budget "
                            "governor (reserve the last turn to summarise and STOP) and bound "
                            "discovery to the peek-hinted market/threads instead of sweeping every "
                            "inbox. This is a buyer-pass design issue, not a missing recipe."),
                target="bin/harness_run.py:BUYER_PROMPT" if buyer else f"pass:{pr.pass_mode}",
                pass_mode=pr.pass_mode, window=pr.window_start))
        else:
            findings.append(Finding(
                category="pass-failure", severity="high", source="deterministic",
                summary=f"{pr.pass_mode} pass exited rc={pr.rc}",
                evidence=evidence,
                suggestion="Investigate the failing pass; the narrative shows where it stopped.",
                target=f"pass:{pr.pass_mode}", pass_mode=pr.pass_mode, window=pr.window_start))
    return findings


def check_silent_buyer_pass(corpus) -> list[Finding]:
    """A buyer pass that produced no real narrative while unread messages were waiting."""
    findings = []
    unread_waiting = any(c and c > 0 for c in corpus.peek.values())
    if not unread_waiting:
        return findings
    for pr in corpus.pass_records:
        if pr.pass_mode != "buyer":
            continue
        # A turn-exhaustion / session-limit stop is already explained by check_pass_failure and
        # routed to its real cause. Re-flagging it here as a missing inbox recipe (reply-pipeline.md)
        # is the misdiagnosis that muddied the first report, so skip those: this check is ONLY for a
        # buyer pass that ended without a known-limit error yet still produced no replies.
        if classify_failure(pr.narrative) != "other":
            continue
        body = (pr.narrative or "").strip()
        if not body or body.lower().startswith("error"):
            findings.append(Finding(
                category="missed-action", severity="high", source="deterministic",
                summary="Buyer pass produced no replies while unread messages waited",
                evidence=f'buyer pass {pr.window_start} narrative: "{_clip(body) or "(empty)"}"',
                suggestion=("Buyer pass ended without acting and without a turn/session-limit error. "
                            "Confirm the per-market inbox read-recipe actually opens the inbox and "
                            "reads threads past their cursor (a missing recipe fails silently)."),
                target="skills/reply-pipeline.md", pass_mode="buyer", window=pr.window_start))
    return findings


def check_secret_leak(corpus) -> list[Finding]:
    """Structural scan for a floor/budget figure surfacing in an outbound message. Heuristic — the
    real guarantee is architectural (gates never emit the value); this is the backstop."""
    findings = []
    for text in corpus.outbound_texts:
        for rx in SECRET_LEAK_RES:
            if rx.search(text or ""):
                findings.append(Finding(
                    category="secret-leak", severity="critical", source="deterministic",
                    summary="Outbound message may reveal a floor/budget figure",
                    evidence=_clip(text),
                    suggestion=("A buyer-facing message structurally references a floor/budget + a "
                                "number. Confirm no secret leaked; tighten skills/voice.md wording."),
                    target="skills/voice.md"))
                break
    return findings


def check_forbidden_copy(corpus) -> list[Finding]:
    """A buyer-facing outbound that says "no meetups"/"ship only" — banned by reply-pipeline.md §4:
    a meetup request escalates the close (§3b), it is never refused with a ship-only line."""
    findings = []
    for text in corpus.outbound_texts:
        if FORBIDDEN_COPY_RE.search(text or ""):
            findings.append(Finding(
                category="banned-copy", severity="high", source="deterministic",
                summary='Outbound message refuses with "no meetups"/"ship only"',
                evidence=_clip(text),
                suggestion=("reply-pipeline.md §3 routes a meetup request to the close (§3b: checkout "
                            "link or handover), never a ship-only refusal. Drop the phrase; do not "
                            "explain ship-only or ask for an area in chat (§4 INVARIANT)."),
                target="skills/reply-pipeline.md"))
    return findings


def check_meetup_not_escalated(corpus) -> list[Finding]:
    """A buyer meetup/self-collect request answered with an area-ask or delivery quote (the old loop)
    while the thread never reached a close. It should route straight to §3b Close instead."""
    findings = []
    for thread in corpus.threads:
        if str(thread.get("status", "")).lower() in CLOSED_STATUSES:
            continue  # the thread did reach a close/escalation, not stuck
        seen_meetup = False
        for msg in thread.get("transcript") or []:
            text = msg.get("text", "")
            direction = msg.get("dir")
            if direction == "in" and MEETUP_REQUEST_RE.search(text or ""):
                seen_meetup = True
            elif direction == "out" and seen_meetup and AREA_GRIND_RE.search(text or ""):
                findings.append(Finding(
                    category="meetup-loop", severity="high", source="deterministic",
                    summary="Meetup request handled with a delivery/area loop, no close escalation",
                    evidence=f'thread {thread.get("thread_id", "?")}: "{_clip(text)}"',
                    suggestion=("Route a meetup request to §3b Close (surface checkout link vs "
                                "handover) instead of asking for the buyer's area or quoting "
                                "delivery. See reply-pipeline.md meetup_request route."),
                    target="skills/reply-pipeline.md"))
                break  # one finding per thread is enough
    return findings


def check_untracked_thread_unhandled(corpus) -> list[Finding]:
    """A chat the inbox sweep recorded as a MANAGED takeover (data/takeover_seen.json, decision=
    'managed') with no managed thread behind it — the TAKEOVER step marked it seen without seeding
    the thread/want. The hot loop only processes tracked threads, so the chat now goes unhandled."""
    findings = []
    for tid, entry in sorted((corpus.takeover_seen or {}).items()):
        if not isinstance(entry, dict) or entry.get("decision") != "managed":
            continue
        if tid in corpus.tracked_thread_ids:
            continue
        side = entry.get("side", "?")
        findings.append(Finding(
            category="takeover-orphan", severity="high", source="deterministic",
            summary=f"Takeover marked managed for {tid} but no managed thread exists",
            evidence=(f"takeover_seen[{tid}]=managed (side={side}); no data/buyer_threads/{tid}.json "
                      f"or data/threads/{tid}.json, and no want references it"),
            suggestion=("inbox-detect TAKEOVER recorded the chat as managed without seeding it. Seed "
                        "the thread/want (and the negotiation ledger) in the SAME step that marks it "
                        "seen, so a killed pass re-offers the chat rather than orphaning it."),
            target="skills/inbox-detect.md"))
    return findings


ALL_CHECKS = (
    check_missed_enquiry,
    check_context_loss,
    check_redundant_recheck,
    check_pass_failure,
    check_silent_buyer_pass,
    check_secret_leak,
    check_forbidden_copy,
    check_meetup_not_escalated,
    check_untracked_thread_unhandled,
)


def run(corpus) -> list[Finding]:
    findings: list[Finding] = []
    for check in ALL_CHECKS:
        findings.extend(check(corpus))
    return findings
