# Always-on seller agent (local, no server)

Reliable unattended operation on your Mac via **launchd**. No hosted server. The design splits
the cheap always-on part from the expensive thinking part so idle cost is ~zero:

```
launchd (RunAtLoad + KeepAlive, survives logout/reboot/crash)
├── com.bazaarskills.chrome   → bin/chrome_debug.sh
│       one warm, logged-in Chrome (your .browser-profile) with remote-debugging :9222
└── com.bazaarskills.agent    → bin/agent_daemon.py   (no LLM)
        • long-`peek`s Telegram (non-consuming) → on pending → run_pass.sh seller
        • every buyer_poll_sec → run_pass.sh buyer
        • every update_poll_sec → throttled read-only upstream update check; on a newer release,
          ONE channel heads-up (NEVER auto-applies — you run /bazaar-upgrade). Deduped per version.
        • single-flight lock, logs, restart-on-crash
            └── run_pass.sh → harness_run.py → the active harness's headless pass (today
                 `claude -p`, scoped perms) does the real work, driving the warm Chrome over
                 CDP (Playwright --cdp-endpoint)
```

The LLM (`claude -p`) runs **only when there's work** — a channel event or a buyer-inbox tick.

> **Adapter- and OS-agnostic.** The daemon peeks whatever channel is bound
> (`seller_config.channel.adapter`): it runs `<adapter>.py peek` (`telegram.py` / `imessage.py` /
> `whatsapp.py`), not a hard-coded Telegram — only the bound adapter's cursor in
> `channel_state.json` advances (`console` has no daemon). The supervisor itself is launchd on
> macOS and **Task Scheduler on Windows** (`bin/install.py supervisor`, behind `bin/platforms/`).
> The headless runner (`bin/harness_run.py`) builds a harness-agnostic `PassSpec` and routes it
> through the active harness's `pass_argv` (`bin/harnesses/`); **Claude Code is the only verified
> runtime today** (the runner refuses an unwired harness). Setup is `./setup` / `/bazaar-install`;
> change it later with `/bazaar`, update with `/bazaar-upgrade`.

## Prerequisites
- `claude`, `npx`, `node`, `curl`, `python3` on PATH. Check: `which claude npx node curl python3`.
  - If `claude` isn't in the plist PATH, export `CLAUDE_BIN` (its full path) in the daemon's
    environment, and/or fix the `PATH` in `launchd/com.bazaarskills.agent.plist`.
- Chrome installed; the `.browser-profile` already logged in to your enabled marketplaces (FB,
  Carousell, eBay, …). The token lives in `../.claude/settings.local.json` (the daemon reads it and
  injects it into each pass).

**Diagnose:** `python3 bin/healthcheck.py` reports whether the daemon can actually run — Chrome/CDP up,
marketplaces logged in, both LaunchAgents loaded — and flags the silent blockers (read-only, no secrets).

## Install / control
```bash
cd seller-agent
bin/chrome_debug.sh &                 # 1) start warm Chrome (first time, verify login)
curl -s http://127.0.0.1:9222/json/version   # 2) confirm CDP is up
launchd/install_daemon.sh install     # 3) install + start both LaunchAgents (and at login)

launchd/install_daemon.sh status      # are they loaded?
tail -f logs/daemon.log               # daemon decisions
tail -f logs/pass.log                 # what each claude -p pass did
launchd/install_daemon.sh uninstall   # stop + remove
```
### Pause & correct (without uninstalling)
Stop the agent **mid-flight** to fix something, from any interface — all three write one flag,
`data/control.json` (owner `bin/control.py`):
- **Telegram:** send **/pause**. The daemon holds every action pass and **interrupts a pass already
  running within ~one poll cadence** (the killed step is idempotent). Then send a plain-language
  **correction** ("list it at $80 not $60", "stop replying to that buyer"); it's captured and
  acked. Send **/resume** — pending corrections are applied to the right state, then work continues.
- **Claude Code:** `/pause` and `/resume` slash commands (or in the `/bazaar` menu).
- **Terminal:** `python3 bin/control.py pause` / `resume` / `status` — a plain file write under
  `data/`, so it pauses the daemon **even mid-pass**.

While paused the agent costs ~$0 (no `claude -p`; just a deterministic channel drain). A pause
**survives a daemon restart** because it's a file. A PreToolUse hook (`bin/hooks/pause_guard.py`)
also blocks marketplace sends while paused, so even an interactive `/sell` / `/buy` session can't
act. **Gotcha:** pausing from Claude Code while the daemon runs pauses *it* (the remote-control
case); inside a foreground `/sell` session there's no background loop to halt — "pause" just means
you stop typing, but the hook still blocks sends.

## First-run verification (do this supervised before trusting it)
1. `bin/chrome_debug.sh` → a Chrome window opens on the agent profile; confirm your enabled
   marketplaces (FB, Carousell, eBay, …) are logged in (log in once if not — it persists).
2. `python3 bin/agent_daemon.py --once --dry-run` → logs should show pending Telegram + "would
   run" passes, and **must not** consume anything (offset unchanged).
3. Install for real, then send **`/list` + photos** of a test item on Telegram. Watch
   `logs/pass.log`: it should poll, run the listing flow autonomously, and publish (pausing only
   on a price/login/field anomaly).
4. Message the listing as a buyer from another account → within `buyer_poll_sec` the buyer pass
   should reply / negotiate.

## Feedback on Telegram (respond-first, then work)
On every seller message the daemon, in order: (1) fires the **native "typing…" indicator**
(instant), (2) sends a **contextual intent line** ("Let me check your listings…") via
`bin/intent.sh` — a fast, MCP-less `claude -p --model haiku` pass (~6s, **no API key**, uses the
Claude Code auth), then (3) runs the full pass, which does the work and reports
progress/results. No canned text; the typing indicator pulses throughout. If the intent step
fails for any reason it's skipped silently (you still get typing + the full pass).

## Two front-ends (Telegram + Claude Code console)
- **Telegram** (this daemon) = remote/phone, always-on.
- **`/sell`** (Claude Code console) = at-desk, native streaming visibility.
- **Don't run both against the browser/Telegram at once** (single consumer + run-lock). To drive
  interactively, `install_daemon.sh uninstall` first, use `/sell`, then `install` again.
- The interactive loop self-guards: it runs `python3 bin/daemon_conflict.py` at session start and
  warns if a loaded daemon would fight it (conflict iff daemon loaded AND channel is single-consumer
  like Telegram/WhatsApp; `console` never conflicts). Exit 1 = conflict, 0 = safe.

## Reliability properties
- **Survives** terminal close, logout, reboot (RunAtLoad), and crashes (KeepAlive + ThrottleInterval).
- **Single-flight** (default): `.daemon.runlock` ensures one pass at a time — no ledger/cursor races.
- **Idempotent**: Telegram offset + per-thread cursors → a retried pass double-does nothing.
- **Warm browser**: one CDP Chrome, logins persist, no per-pass relaunch churn.

## Concurrency — `max_concurrent_workers` (default 2)
`data/config.json → max_concurrent_workers` controls parallelism. **It now ships at `2`** (the
**concurrent supervisor**, `bin/supervisor.py`); set it to `1` to fall back to the proven
single-flight loop above (byte-identical to pre-Phase-3 behavior). With `2`+:

- **What runs in parallel:** SELL-INBOX (`buyer`) work across **different marketplaces** — one worker
  per marketplace (FB inbox ∥ Carousell inbox), each holding its own `market:<id>` lease
  (`bin/lease.py`) and driving **only its own Chrome tab** (selected by stable host via
  `bin/tab_registry.py`, never by a shifting index, never opening/closing tabs). A **scope-guard
  PreToolUse hook** (`bin/hooks/scope_guard.py`) hard-denies a worker that tries to navigate to a
  *different* marketplace — the same-account guard no longer depends on prompt compliance.
- **Conservative posture (account safety):** never two automated actions on one account — the
  `market:<id>` lease guarantees it. `channel`/`buy`/`maint` stay **exclusive** (they're unscoped or
  may publish to any market): a user message preempts the market workers (as today); `buy`/`maint`
  run only when no market worker is live.
- **One channel writer:** concurrent workers enqueue background completion notices to
  `bin/channel_outbox.py`; the supervisor drains them to the channel in FIFO order, so messages never
  interleave. The atomic per-market hourly cap (`bin/pacing_gate.py`) holds across all workers.
- **Crash recovery:** leases carry a heartbeat TTL — a worker (or supervisor) that dies has its lease
  reclaimed automatically; cursors keep every pass idempotent on restart.
- **Gains:** latency-under-load (~2–3× when several marketplaces are hot at once), not idle
  throughput. A supervised first run (watch `logs/daemon.log`) is still wise the first time.
- **Safety hardening (built):** preempt kills the worker's whole process group (no orphaned `claude`
  driving an account after its lease is freed); a single-instance lock (`.daemon.instancelock`) makes
  the heartbeat-TTL lease liveness sound; the scope-guard hook hard-blocks cross-marketplace
  navigation; the outbox drain can't crash the supervisor and dead-letters a poison notice after
  `MAX_SEND_ATTEMPTS` (no head-of-line block); a per-worker watchdog hard-caps a runaway pass.
- **Remaining boundary:** the per-send **pacing reserve** stays prompt-enforced — the atomic
  per-market cap (`bin/pacing_gate.py`) already bounds the rate whenever a reserve is called, so a
  hard hook would only catch a skill that skips the reserve entirely. Interactive escalations under
  concurrency still `notify()` directly (rare under the conservative default).

## Caveats (honest)
- **Single Telegram consumer.** While the daemon runs, don't also manually poll Telegram in an
  interactive session — two consumers fight over the offset. Stop the daemon to debug by hand.
- **Sleep/airplane.** A closed laptop / sleep pauses it (it's local). It resumes on wake; truly
  24/7 needs an always-on machine.
- **Account safety (feasibility §2.2).** An always-on browser on your real marketplace accounts raises
  ban risk. Pacing/jitter/hourly-caps stay on; the `max_actions_per_hour` cap is enforced
  deterministically by `bin/pacing_gate.py` (an atomic, `fcntl.flock`-guarded per-marketplace counter
  in `data/pacing_state.json`) — not by the model self-counting — so sell- and buy-side work on one
  account share a single hourly budget that holds even across passes. It stops-and-escalates on any
  checkpoint. Start with a conservative `buyer_poll_sec` (≥300s) and watch for friction.
- **Headless trust.** Passes run with `--permission-mode acceptEdits` + an explicit
  `--allowedTools` set (incl. the browser tools and `run_code_unsafe`, needed for upload/JS-click).
  This is the scoped auto-permission you approved — it is **not** `--dangerously-skip-permissions`.
