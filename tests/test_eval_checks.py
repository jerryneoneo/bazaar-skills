#!/usr/bin/env python3
"""Headline tests for eval_checks.py — locks the two reported defects (and their inverses).

    python3 tests/test_eval_checks.py

Defect B (missed Facebook enquiries): unread on a market + no recent reply -> exactly one
missed-action; the inverse (a recent reply exists) -> none.
Defect A (context loss): "do all tasks" answered with a re-check -> context-loss; the inverse
(answered by acting) -> none.
Plus pass-failure, redundant-recheck, and secret-leak.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import eval_checks  # noqa: E402
from eval_corpus import Corpus  # noqa: E402
from eval_schema import EvalRecord  # noqa: E402

_failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _failures.append(name)


def _cats(findings):
    return [f.category for f in findings]


def test_missed_enquiry():
    print("defect B — missed buyer enquiry:")
    miss = Corpus(peek={"fb": 20}, outbound_recent={}, escalation_markets=set())
    found = eval_checks.check_missed_enquiry(miss)
    check("fb=20 unread + no reply -> one missed-action", len(found) == 1 and found[0].category == "missed-action")
    check("severity high", found[0].severity == "high")
    check("targets the fb recipe", found[0].target == "skills/listing-flows/fb.md")

    replied = Corpus(peek={"fb": 20}, outbound_recent={"fb": 3}, escalation_markets=set())
    check("inverse: a recent reply exists -> none", eval_checks.check_missed_enquiry(replied) == [])

    parked = Corpus(peek={"fb": 20}, outbound_recent={}, escalation_markets={"fb"})
    check("inverse: open escalation parks it -> none", eval_checks.check_missed_enquiry(parked) == [])


def test_context_loss():
    print("defect A — context loss:")
    bad = Corpus(channel_turns=[EvalRecord(
        record_id="r1", kind="channel_turn", user_said="do all tasks",
        agent_considered="Let me check what needs doing…", prior_tag="enumerated-tasks")])
    found = eval_checks.check_context_loss(bad)
    check('"do all tasks" + re-check -> context-loss', len(found) == 1 and found[0].category == "context-loss")

    good = Corpus(channel_turns=[EvalRecord(
        record_id="r2", kind="channel_turn", user_said="do all tasks",
        agent_considered="On it, running both — cross-listing the gashapon and starting the iPhone hunt.")])
    check("inverse: answered by acting -> none", eval_checks.check_context_loss(good) == [])

    unrelated = Corpus(channel_turns=[EvalRecord(
        record_id="r3", kind="channel_turn", user_said="sell my old bike",
        agent_considered="Let me check the comps")])
    check("non-follow-up not flagged as context-loss", eval_checks.check_context_loss(unrelated) == [])


def test_pass_failure():
    print("pass failures:")
    corpus = Corpus(pass_records=[
        EvalRecord(record_id="p1", kind="pass", pass_mode="buyer", rc=1, narrative="Error: Reached max turns (14)"),
        EvalRecord(record_id="p2", kind="pass", pass_mode="maint", rc=0, narrative="Done."),
        EvalRecord(record_id="p3", kind="pass", pass_mode="channel", rc=143, narrative="preempted"),
    ])
    found = eval_checks.check_pass_failure(corpus)
    check("rc=1 flagged, rc=0 and rc=143 not", len(found) == 1 and found[0].pass_mode == "buyer")


def test_classify_failure():
    print("failure classification (turn-exhaustion vs session-limit vs other):")
    check("'Reached max turns' -> turn-exhaustion",
          eval_checks.classify_failure("Error: Reached max turns (40)") == "turn-exhaustion")
    check("'hit your session limit' -> session-limit",
          eval_checks.classify_failure("You've hit your session limit · resets 3:10pm") == "session-limit")
    check("'usage limit' -> session-limit",
          eval_checks.classify_failure("Claude usage limit reached") == "session-limit")
    check("a plain crash -> other", eval_checks.classify_failure("Traceback: boom") == "other")
    check("empty -> other", eval_checks.classify_failure("") == "other")


def test_pass_failure_classified():
    print("pass-failure is classified + correctly targeted:")
    # A buyer turn-exhaustion must point at the buyer-pass spec (harness_run.py), NOT the recipe.
    exhausted = Corpus(pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buyer", rc=1,
                                                narrative="Error: Reached max turns (40)")])
    found = eval_checks.check_pass_failure(exhausted)
    check("buyer max-turns -> turn-exhaustion category", len(found) == 1 and found[0].category == "turn-exhaustion")
    check("targets the buyer-pass spec, not reply-pipeline",
          found[0].target == "bin/harness_run.py:BUYER_PROMPT")
    check("turn-exhaustion is high severity", found[0].severity == "high")

    # A session-limit stop is an external transient: medium, no in-repo file blamed.
    limited = Corpus(pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buy", rc=1,
                                              narrative="You've hit your session limit · resets 9:40pm")])
    found = eval_checks.check_pass_failure(limited)
    check("session-limit category", len(found) == 1 and found[0].category == "session-limit")
    check("session-limit is not blamed on a code file", found[0].target == "external:session-limit")
    check("session-limit is medium severity (transient, not a code defect)", found[0].severity == "medium")

    # An unrecognised crash keeps the generic pass-failure category.
    crash = Corpus(pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="maint", rc=2, narrative="boom")])
    check("unknown crash -> generic pass-failure",
          eval_checks.check_pass_failure(crash)[0].category == "pass-failure")


def test_silent_buyer_pass():
    print("silent buyer pass while unread waiting:")
    # A genuinely empty (non-error) buyer pass while unread waits DOES point at the recipe/logic.
    corpus = Corpus(peek={"fb": 20},
                    pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buyer", rc=0, narrative="")])
    found = eval_checks.check_silent_buyer_pass(corpus)
    check("empty narrative + unread -> missed-action", len(found) == 1 and found[0].category == "missed-action")

    # A turn-exhaustion / session-limit failure is explained by check_pass_failure — it must NOT
    # also be (mis)attributed to a missing inbox recipe here (the bug that confused the report).
    exhausted = Corpus(peek={"fb": 20},
                       pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buyer", rc=1,
                                                narrative="Error: Reached max turns (40)")])
    check("turn-exhaustion NOT double-flagged as a recipe miss",
          eval_checks.check_silent_buyer_pass(exhausted) == [])
    limited = Corpus(peek={"fb": 20},
                     pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buyer", rc=1,
                                              narrative="You've hit your session limit · resets 3:10pm")])
    check("session-limit NOT double-flagged as a recipe miss",
          eval_checks.check_silent_buyer_pass(limited) == [])

    quiet = Corpus(peek={}, pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buyer", rc=1, narrative="")])
    check("inverse: nothing unread -> none", eval_checks.check_silent_buyer_pass(quiet) == [])


def test_redundant_recheck():
    print("redundant re-check (intent pre-acks ignored):")
    corpus = Corpus(turns=[
        {"dir": "out", "kind": "say", "text": "Let me check your listings"},
        {"dir": "out", "kind": "say", "text": "Let me check what needs doing"},
    ])
    check("two considered re-checks -> flagged", len(eval_checks.check_redundant_recheck(corpus)) == 1)
    intents = Corpus(turns=[
        {"dir": "out", "kind": "intent", "text": "Let me check your listings"},
        {"dir": "out", "kind": "intent", "text": "Let me check what needs doing"},
    ])
    check("intent pre-acks are NOT redundant-recheck", eval_checks.check_redundant_recheck(intents) == [])


def test_secret_leak():
    print("secret-leak backstop:")
    leak = Corpus(outbound_texts=["honestly my floor is 285 but don't tell"])
    check("floor + number flagged critical",
          any(f.category == "secret-leak" and f.severity == "critical"
              for f in eval_checks.check_secret_leak(leak)))
    clean = Corpus(outbound_texts=["I can do 285 for you, deal!"])
    check("a plain price is NOT a leak", eval_checks.check_secret_leak(clean) == [])


def test_untracked_thread_unhandled():
    print("takeover marked managed but no thread seeded behind it:")
    orphan = Corpus(takeover_seen={"carousell:42": {"decision": "managed", "side": "buy"}},
                    tracked_thread_ids=set())
    found = eval_checks.check_untracked_thread_unhandled(orphan)
    check("orphaned managed takeover -> one takeover-orphan",
          len(found) == 1 and found[0].category == "takeover-orphan")
    check("severity high", found and found[0].severity == "high")
    check("targets the inbox-detect skill", found and found[0].target == "skills/inbox-detect.md")

    seeded = Corpus(takeover_seen={"carousell:42": {"decision": "managed", "side": "buy"}},
                    tracked_thread_ids={"carousell:42"})
    check("inverse: a tracked thread exists -> none",
          eval_checks.check_untracked_thread_unhandled(seeded) == [])

    declined = Corpus(takeover_seen={"carousell:9": {"decision": "declined", "side": "buy"}},
                      tracked_thread_ids=set())
    check("inverse: a declined chat is not an orphan -> none",
          eval_checks.check_untracked_thread_unhandled(declined) == [])


def test_forbidden_copy():
    print("banned copy — 'no meetups' / 'ship only' in buyer chat:")
    leak = Corpus(outbound_texts=["Yes it's available! I only ship items (no meetups). Let me know your area."])
    found = eval_checks.check_forbidden_copy(leak)
    check('"no meetups" outbound -> one banned-copy', len(found) == 1 and found[0].category == "banned-copy")
    check("severity high", found and found[0].severity == "high")
    check("targets reply-pipeline", found and found[0].target == "skills/reply-pipeline.md")

    ship = Corpus(outbound_texts=["Sorry, this one is ship-only."])
    check('"ship-only" also flagged', len(eval_checks.check_forbidden_copy(ship)) == 1)

    clean = Corpus(outbound_texts=["I can sort delivery and get this to you, want me to set that up?"])
    check("inverse: clean redirect copy -> none", eval_checks.check_forbidden_copy(clean) == [])


def test_meetup_not_escalated():
    print("meetup request answered with a delivery/area loop instead of a close:")
    bad = Corpus(threads=[{
        "thread_id": "fb:1", "status": "active",
        "transcript": [
            {"dir": "in", "text": "Hi, can we meet this weekend?"},
            {"dir": "out", "text": "Sure! What area are you in so I can quote delivery?"},
        ]}])
    found = eval_checks.check_meetup_not_escalated(bad)
    check("meetup + area-ask, still active -> one meetup-loop",
          len(found) == 1 and found[0].category == "meetup-loop")
    check("severity high", found and found[0].severity == "high")

    escalated = Corpus(threads=[{
        "thread_id": "fb:2", "status": "escalated",
        "transcript": [
            {"dir": "in", "text": "can we meet?"},
            {"dir": "out", "text": "let me know your area"},
        ]}])
    check("inverse: thread reached a close/escalation -> none",
          eval_checks.check_meetup_not_escalated(escalated) == [])

    good = Corpus(threads=[{
        "thread_id": "fb:3", "status": "active",
        "transcript": [
            {"dir": "in", "text": "can we meet?"},
            {"dir": "out", "text": "Let me sort the best way to get this to you, back shortly!"},
        ]}])
    check("inverse: meetup handled without an area/delivery loop -> none",
          eval_checks.check_meetup_not_escalated(good) == [])

    no_meetup = Corpus(threads=[{
        "thread_id": "fb:4", "status": "active",
        "transcript": [
            {"dir": "in", "text": "is this still available?"},
            {"dir": "out", "text": "what area are you in?"},
        ]}])
    check("inverse: no meetup request -> none",
          eval_checks.check_meetup_not_escalated(no_meetup) == [])


def test_run_aggregates():
    print("run() aggregates all checks:")
    corpus = Corpus(
        peek={"fb": 20}, outbound_recent={}, escalation_markets=set(),
        outbound_texts=["I only ship items (no meetups)."],
        threads=[{"thread_id": "fb:1", "status": "active", "transcript": [
            {"dir": "in", "text": "can we meet?"},
            {"dir": "out", "text": "what area are you in?"}]}],
        channel_turns=[EvalRecord(record_id="r", kind="channel_turn", user_said="do all tasks",
                                  agent_considered="let me check what needs doing")],
        pass_records=[EvalRecord(record_id="p", kind="pass", pass_mode="buy", rc=1, narrative="boom")])
    cats = _cats(eval_checks.run(corpus))
    check("missed-action present", "missed-action" in cats)
    check("context-loss present", "context-loss" in cats)
    check("pass-failure present", "pass-failure" in cats)
    check("banned-copy present", "banned-copy" in cats)
    check("meetup-loop present", "meetup-loop" in cats)


if __name__ == "__main__":
    print("eval_checks.py tests\n")
    test_missed_enquiry()
    test_context_loss()
    test_pass_failure()
    test_classify_failure()
    test_pass_failure_classified()
    test_silent_buyer_pass()
    test_redundant_recheck()
    test_secret_leak()
    test_forbidden_copy()
    test_meetup_not_escalated()
    test_untracked_thread_unhandled()
    test_run_aggregates()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")
