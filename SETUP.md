# SELLY Skills — Setup runbook (manual install, as it stands today)

This is the **zero-to-running** install record — every step, dependency, secret, and gotcha we hit
standing the agent up on a real Mac. It documents *reality*, not an idealized flow.

> **Most users won't run these steps by hand** — `./setup` automates the §11 friction inventory.
> To install (one paste, gstack-style):
> `git clone https://github.com/jerryneoneo/selly-agent.git ~/selly-agent && cd ~/selly-agent && ./setup`.
> `./setup` is **idempotent**: prereqs → pick runtime (Claude Code) → sign-in gate → install global
> launchers (so `/selly`, `/sell`, `/buy` work from any project) → first-run handoff to
> `.claude/commands/selly-install.md` (with the verified choice in `$SELLY_HARNESS`), which drives
> the rest via `bin/preflight.py`, `bin/install.py`, `bin/platforms/`, and `bin/harnesses/`. Re-run
> `./setup` any time (e.g. after `git pull`) — it refreshes launchers/config + runs migrations and
> **skips onboarding** once configured. Flags: `--host`, `--autonomy`, `--yes`, `--prefix`.
> Prefer a local copy? Put the repo at `~/selly-agent` and run `./setup` directly (no clone needed).
> If you self-host the bootstrap, a curl one-liner (`curl -fsSL https://<your-host>/install.sh | bash`,
> or `iwr -useb https://<your-host>/install.ps1 | iex` on Windows) is optional; `install.sh` only
> clones, then hands off to `./setup`. Lifecycle: `/selly-upgrade`, `bin/selly-uninstall`,
> `bin/selly-config`. This file
> remains the **reference** for what those scripts do and how to do it by hand.

**Why this file exists:** it's the basis for the **installer UX** (now built — see above). Every step
tagged **🔧 MANUAL** or **⚠️ GOTCHA** maps to an automated step in `selly-install.md` — see
[§11 Friction inventory](#11-friction-inventory--automation-candidates).

> Companion docs: **README.md** = overview & install · **DAEMON.md** = day-to-day operations · this file
> = install. Read order for a newcomer: README → SETUP → DAEMON.

---

## 1. What you're installing
An always-on personal seller agent: you chat with it on **Telegram** (or the `/sell` Claude Code
console); it lists items on **your enabled marketplaces** (FB Marketplace, Carousell, eBay, … —
chosen at onboarding per your region) through a real logged-in Chrome and replies to buyers. It runs as a **macOS launchd daemon** that invokes headless `claude -p` only
when there's work, driving a warm Chrome over CDP.

**Two locations (important):**
- **Dev source** — where the code is edited (this repo, e.g. `…/SELLY Skills/seller-agent`).
- **Live runtime** — `~/selly-agent` (must be outside `~/Documents`; see §3). The daemon runs
  from here. Changes are pushed dev → runtime with `rsync` (see §10 / DAEMON.md).

---

## 2. Prerequisites
| Need | Why | Check |
|---|---|---|
| macOS | launchd supervises the daemon + Chrome | `uname` |
| **Claude Code CLI, logged in** | headless `claude -p` runs every pass — **reuses this auth, no API key** | `which claude` · `claude -p "hi"` |
| Node + npx | runs the Playwright MCP browser tool | `which npx node` |
| Python 3 | all `bin/*.py` (stdlib only — no pip installs) | `which python3` |
| Google Chrome | the real browser the agent drives | `ls "/Applications/Google Chrome.app"` |
| Telegram account | to talk to your bot | — |

One-liner preflight:
```bash
which claude npx node python3 && ls "/Applications/Google Chrome.app" >/dev/null && echo "prereqs ok"
```
On macOS, `./setup` offers to `brew install` any missing Node/Chrome for you (pass `--no-install` to skip).
Note where `claude`/`npx`/`node` live — launchd needs them on PATH (§8). On this machine:
`claude` → `~/.local/bin`, `npx`/`node` → `~/.nvm/versions/node/<ver>/bin`.

---

## 3. ⚠️ GOTCHA — put the runtime OUTSIDE `~/Documents`
macOS **TCC privacy** blocks launchd-spawned processes from reading `~/Documents`, `~/Desktop`,
`~/Downloads` → you'll see `Operation not permitted` and the daemon flaps. Keep the live runtime
at **`~/selly-agent`** (home root is fine).

```bash
rsync -a --exclude 'logs/' --exclude '.daemon.runlock' "<dev>/seller-agent/" "$HOME/selly-agent/"
```

---

## 4. 🔧 MANUAL — create the Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → pick a name/username → copy the **token**.
2. Open your bot and tap **Start** (`/start`). The agent captures your `chat_id` automatically on
   its first poll (stored in `data/channel_state.json`) — single-tenant: it ignores all other chats.
3. Verify the bind: `python3 bin/telegram.py verify` → `token_valid:true` + `chat_bound:true`
   (exit 0). Exit 3 = token missing/malformed/rejected (re-copy it); exit 1 = token good but no
   `/start` yet (tap Start, then re-run). Onboarding runs this gate for you and never binds a bad
   token or a null `chat_id`.

---

## 5. 🔧 MANUAL — secrets + permissions
Create **`~/selly-agent/.claude/settings.local.json`** (gitignored):
```json
{
  "env": { "TELEGRAM_BOT_TOKEN": "123456:PASTE-YOUR-BOTFATHER-TOKEN" },
  "permissions": {
    "allow": [
      "mcp__playwright__browser_navigate", "mcp__playwright__browser_click",
      "mcp__playwright__browser_type", "mcp__playwright__browser_fill_form",
      "mcp__playwright__browser_file_upload", "mcp__playwright__browser_snapshot",
      "mcp__playwright__browser_take_screenshot", "mcp__playwright__browser_select_option",
      "mcp__playwright__browser_wait_for", "mcp__playwright__browser_press_key",
      "mcp__playwright__browser_tabs", "mcp__playwright__browser_evaluate",
      "mcp__playwright__browser_run_code_unsafe"
    ]
  }
}
```
- `permissions.allow` is for **interactive** sessions; the daemon's headless passes pass the same
  tools via `run_pass.sh --allowedTools` + `--permission-mode acceptEdits` (not `--dangerously-skip-permissions`).
- **Don't hand-maintain this** — `python3 bin/install.py gen-settings --harness claude-code --autonomy <level>`
  writes it for you (merges, preserves the token, sets the right allow-list). `./setup` does this automatically.
- ⚠️ GOTCHA: it must be **valid JSON** — we broke it with a missing comma/brace. Verify the file AND
  that the autonomous-run essentials are actually granted: `python3 bin/install.py validate --harness
  claude-code` (checks JSON **and** that the effective allow-list — committed `settings.json` ∪ this
  file — covers the required browser + `bin/` tools). `healthcheck.py` reports the same as a warning.
- The token is read by `bin/telegram.py` from `$TELEGRAM_BOT_TOKEN`; never printed or committed.

---

## 6. Browser tool (Playwright MCP) + warm logged-in Chrome
- **`.mcp.json`** registers Playwright MCP and **attaches** to a running Chrome (doesn't relaunch):
  ```json
  { "mcpServers": { "playwright": { "command": "npx",
      "args": ["-y","@playwright/mcp@0.0.76","--cdp-endpoint","http://127.0.0.1:9222"] } } }
  ```
  > The MCP version is **pinned** (`@0.0.76`, not `@latest`) on purpose — a floating tag can pull a
  > breaking release mid-session. Bump it deliberately, then re-run the suite.
- **`bin/chrome_debug.sh`** launches real Chrome on the persistent profile `.browser-profile` with
  `--remote-debugging-port=9222`. launchd keeps it alive (§8).
- **🔧 MANUAL — log in once:** with that Chrome open, sign into **each marketplace you enabled**
  (e.g. FB Marketplace, Carousell, eBay). Logins persist in `.browser-profile`, so the agent acts
  as you (account-safety thesis).
- Verify Chrome/CDP is actually serving (not just launched): `python3 bin/wait_cdp.py` → `ready:true`
  (polls `/json/version` until up or times out — replaces the one-shot `curl` that could race a
  slow-starting Chrome).
- Verify each marketplace login is **real**, not just assumed: `python3 bin/login_check.py market <id>`
  → `logged_in` (exit 0) / `logged_out` (exit 1) / `unknown` (exit 3 — no tab open / can't tell).
  Onboarding runs this so a confirmed marketplace is one it actually saw you signed into.

---

## 7. 🔧 MANUAL — onboarding (writes `seller_config.json`)
Drive it conversationally — Telegram `/start` → **Set up** (or `/sell` in a Claude Code session):
currency, region/timezone, marketplaces, **exact pickup address** (private — used only for the
delivery-fee calc), **P2P delivery zones**, availability (**connect calendar / manual / skip**),
confirm you're logged into the marketplaces. This produces `data/seller_config.json`.

---

## 8. Install the always-on daemon (launchd)
```bash
cd ~/selly-agent
bin/chrome_debug.sh &                         # warm Chrome (first time, confirm logins)
launchd/install_daemon.sh install             # loads chrome + agent LaunchAgents (RunAtLoad+KeepAlive)
```
- **plist PATH (auto-derived):** the committed plists are TEMPLATES — `launchd/install_daemon.sh`
  substitutes `__RUNTIME__` (this checkout), `__PATH__` (the dirs where your `claude`/`npx`/`node`
  live, detected via `which` at install), and `__PYTHON__` (a stable FDA-grantable interpreter,
  preferring Homebrew python over the bare `/usr/bin/python3` shim) before copying them into
  `~/Library/LaunchAgents`. You only need to hand-edit `__PATH__` if detection misses a dir
  (launchd jobs otherwise get a minimal PATH and can't find `claude`/`npx`).
- The two jobs: `com.selly.chrome` (warm browser) + `com.selly.agent`
  (`bin/agent_daemon.py`). They start at login and restart on crash.

---

## 9. Verify
```bash
cd ~/selly-agent
for t in floor_gate shipping telegram negotiate; do python3 tests/test_$t.py | tail -1; done  # ALL PASS x4
python3 bin/install.py validate --harness claude-code # config JSON valid + permission floor granted
python3 bin/healthcheck.py                           # runnable state: CDP, logins, permissions, daemon
python3 bin/wait_cdp.py                              # CDP up (polls, ready:true)
python3 bin/telegram.py verify                       # token_valid + chat_bound (the channel works)
launchd/install_daemon.sh status                    # both loaded
tail -f logs/daemon.log                              # watch
```
Then **send the bot a message** → expect: native **typing…** (instant) → a **contextual intent
line** (~6s, e.g. "Let me check your listings…", from `bin/intent.sh`) → the worked result.
`daemon.log` should show `… pending → typing + intent + seller pass`.

---

## 10. Operate
- Logs: `logs/daemon.log` (decisions) · `logs/pass.log` (each `claude -p` pass).
- Pause sending: `/pause` on Telegram (`/resume` to continue).
- `launchd/install_daemon.sh status | uninstall`.
- **Two front-ends, never both at once** (single Telegram consumer + the run-lock): the **Telegram
  daemon** vs the **`/sell` console** (at-desk, native streaming). To use `/sell`: `uninstall` the
  daemon first, then re-`install`. The interactive loop runs `python3 bin/daemon_conflict.py` at
  session start and warns you if a loaded daemon would fight it (conflict → exit 1).
- Push code changes: `rsync` dev → `~/selly-agent` (exclude `launchd/`, `.claude/settings.local.json`,
  `.browser-profile/`, `logs/`), then `install_daemon.sh install` to restart.

---

## 11. Friction inventory → automation candidates
What the future installer should do for each manual/gotcha step:

| Step | Today (manual/gotcha) | Installer should… |
|---|---|---|
| Select harness + sign in (§2) | autodetect, fail later if not logged in | **menu** to pick Claude Code or Codex → **gate on sign-in** (instruct + wait + re-check via `install.py harness --name`) → pass the choice to Stage 2 as `$SELLY_HARNESS` |
| Prereqs (§2) | check `which` by hand | **preflight** node/python/chrome + verify `claude` is logged in; offer to fix |
| Location (§3) | know about TCC, copy to `~/selly-agent` | pick a **non-TCC dir** automatically; do the copy |
| Bot token (§4) | BotFather, copy token | **guided** BotFather walkthrough + paste field; `telegram.py verify` gates a bad/malformed token |
| chat_id (§4) | `/start`, captured on poll | detect first `/start`, confirm via `telegram.py verify` (`chat_bound`) — never bind a null chat_id |
| Secrets/perms (§5) | hand-edit JSON (we broke it) | **generate** `settings.local.json` + `install.py validate` (JSON **and** permission floor) |
| Browser/CDP (§6) | `.mcp.json` + `chrome_debug.sh` | generate `.mcp.json`; launch warm Chrome; `wait_cdp.py` blocks until it's serving |
| Marketplace login (§6) | log into your marketplaces manually | open Chrome to each enabled marketplace; `login_check.py` **probes** the live DOM (earned, not assumed) |
| Onboarding (§7) | conversational | in-app **wizard** (currency/address/zones/availability) |
| Daemon + PATH (§8) | edit plist PATH to match `which` | **generate plists with detected paths**; one-click load |
| Verify (§9) | run tests/curl/tail | built-in **health check** + first-message smoke test |

---

## 12. Known gotchas / operational notes
- **Auto update-check**: SELLY checks for a newer version on its own and OFFERS to update — it never
  auto-applies (you run `/selly-upgrade`). Three surfaces, all sharing one throttle + per-version
  dedupe (`bin/update_check.py`): the global launchers check when you run `/selly` / `/sell` / `/buy`;
  a SessionStart hook checks when you open a session in the runtime dir; and the always-on daemon
  sends a one-line Telegram heads-up. Cadence + behavior: `config.json` →
  `update_check_interval_hours` (default 24, 0 disables), `update_snooze_days`, `update_poll_sec`.
  The check is a read-only `git fetch` and fail-open (no network → no nag).
  - The SessionStart hook lives in `bin/hooks/update_notice.py`; wire it once in
    `.claude/settings.json` under `hooks.SessionStart` (matcher `startup|resume`). It NO-OPs for the
    daemon's headless `-p` passes (they set `SELLY_DAEMON_PASS=1`).
- **TCC**: runtime must be outside `~/Documents`/`Desktop`/`Downloads` (§3).
- **Single Telegram consumer**: don't run the daemon and a manual poll / `/sell` session at the
  same time — they fight over the `getUpdates` offset + browser.
- **No API key**: headless `claude -p` (passes *and* `intent.sh`'s haiku line) reuses the Claude
  Code login. `intent.sh` is MCP-less so it returns in ~6s; the full pass loads Playwright (~15-20s).
- **Network/DNS drop** (e.g. laptop asleep): peeks fail gracefully (`pending:0`), no crash; it
  resumes when connectivity returns.
- **FB account-safety/ban risk**: real session + jitter/hourly-cap pacing; stops-and-escalates on
  a checkpoint. Treat unattended FB automation as the riskiest part (feasibility §2.2).
- **nvm PATH**: the agent plist's `PATH` hardcodes the current nvm node version dir — update it if
  you change Node versions.
- **Dev ≠ runtime**: edits in the dev source don't take effect until `rsync`'d to `~/selly-agent`
  and the daemon is reinstalled.
- **No secrets in git**: `.gitignore` excludes `settings.local.json`, `.browser-profile/`, `logs/`,
  `data/{channel_state,threads,negotiations,escalations,listing_session}` and photos.
