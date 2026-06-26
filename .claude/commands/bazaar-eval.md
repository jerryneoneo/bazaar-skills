---
description: Bazaar Skills — evaluate recent conversations & passes, surface UX/behavior issues to fix
---

# /bazaar-eval — grade the agent's own behavior

A self-evaluation pass over recent activity. It joins the control-channel transcript
(`data/channel_transcript.jsonl`), the pass log (`logs/pass.log`), and live state
(`data/buyer_peek_state.json`, `data/threads/`, `data/escalations.jsonl`), then flags responses that
don't make sense or hurt UX — and clusters them into concrete, deduped improvement candidates.

Two layers:
- **Deterministic checks** (`--no-llm`, zero cost) catch the unambiguous cases: unread buyer messages
  a market never replied to (the missed-enquiry bug), a follow-up like "do all tasks" answered with a
  re-check (context loss), failed/silent passes, repeated re-checks, floor/budget leaks, and an inbox
  takeover marked managed with no thread seeded behind it (an orphaned takeover from the inbox sweep).
- **LLM judge** (default, on-demand) adds nuance: misroutes, hallucinated state, tone/voice, low-UX.

## Run it

```
python3 bin/eval_run.py run                 # deterministic + LLM judge over the last 24h
python3 bin/eval_run.py run --no-llm        # zero-cost deterministic only (what the nightly check runs)
python3 bin/eval_run.py run --last 50       # last 50 passes regardless of age
python3 bin/eval_run.py run --fail-on high  # exit 1 if any HIGH+ finding (CI gate)
python3 bin/eval_run.py report              # re-render the report from saved findings
```

Then read `data/eval/report.md` and summarize for the user: the top improvement candidates
(ranked by severity × frequency), what each one means, and the one concrete fix per candidate. Call
out the two known defect classes explicitly if present (missed buyer enquiries on a marketplace;
context-loss on follow-ups). Outputs are local-only (gitignored): `data/eval/findings.jsonl`,
`data/eval/improvements.jsonl`, `data/eval/report.md`.

> The improvement candidates are also the hand-off seam for a future opt-in, anonymized
> upstream-contribution loop — that loop is **not built yet**; nothing here leaves the device.
