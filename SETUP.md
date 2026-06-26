# Bazaar Skills — Setup runbook (manual install, as it stands today)

This is the **zero-to-running** install record — every step, dependency, secret, and gotcha we hit
standing the agent up on a real Mac. It documents *reality*, not an idealized flow.

> **Most users won't run these steps by hand** — `./setup` automates the §11 friction inventory.
> To install (one paste, gstack-style):
> `git clone https://github.com/jerryneoneo/bazaar-skills.git ~/bazaar-skills && cd ~/bazaar-skills && ./setup`.
> `./setup` is **idempotent**: prereqs → pick runtime (Claude Code) → sign-in gate → install global
> launchers (so `/bazaar`, `/sell`, `/buy` work from any project) → first-run handoff to
> `.claude/commands/bazaar-install.md` (with the verified choice in `$BAZAAR_HARNESS`), which drives
> the rest via `bin/preflight.py`, `bin/install.py`, `bin/platforms/`, and `bin/harnesses/`. Re-run
> `./setup` any time (e.g. after `git pull`) — it refreshes launchers/config + runs migrations and
> **skips onboarding** once configured. Flags: `--host`, `--autonomy`, `--yes`, `--prefix`.
> Prefer a local copy? Put the repo at `~/bazaar-skills` and run `./setup` directly (no clone needed).
> If you self-host the bootstrap, a curl one-liner (`curl -fsSL https://<your-host>/install.sh | bash`,
> or `iwr -useb https://<your-host>/install.ps1 | iex` on Windows) is optional; `install.sh` only
> clones, then hands off to `./setup`. Lifecycle: `/bazaar-upgrade`, `bin/bazaar-uninstall`,
> `bin/bazaar-config`. This file
> remains the **reference** for what those scripts do and how to do it by hand.

**Why this file exists:** it's the basis for the **installer UX** (now built — see above). Every step
tagged **🔧 MANUAL** or **⚠️ GOTCHA** maps to an automated step in `bazaar-install.md` — see
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
- **Dev source** — where the code is edited (this repo, e.g. `…/Bazaar Skills/seller-agent`).
- **Live runtime** — `~/bazaar-skills` (must be outside `~/Documents`; see §3). The daemon runs
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
Note where `claude`/`npx`/`node` live — launchd needs them on PATH (§8). On this machine:
`claude` → `~/.local/bin`, `npx`/`node` → `~/.nvm/versions/node/<ver>/bin`.

---

## 3. ⚠️ GOTCHA — put the runtime OUTSIDE `~/Documents`
macOS **TCC privacy** blocks launchd-spawned processes from reading `~/Documents`, `~/Desktop`,
`~/Downloads` → you'll see `Operation not permitted` and the daemon flaps. Keep the live runtime
at **`~/bazaar-skills`** (home root is fine).

```bash
rsync -a --exclude 'logs/' --exclude '.daemon.runlock' "<dev>/seller-agent/" "$HOME/bazaar-skills/"
```

---

## 4. 🔧 MANUAL — create the Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → pick a name/username → copy the **token**.
2. Open your bot and tap **Start** (`/start`). The agent captures your `chat_id` automatically on
   its first poll (stored in `data/channel_state.json`) — single-tenant: it ignores all other chats.

---

## 5. 🔧 MANUAL — secrets + permissions
Create **`~/bazaar-skills/.claude/settings.local.json`** (gitignored):
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
- ⚠️ GOTCHA: it must be **valid JSON** — we broke it with a missing comma/brace; verify:
  `python3 -c "import json;json.load(open('.claude/settings.local.json'));print('ok')"`
- The token is read by `bin/telegram.py` from `$TELEGRAM_BOT_TOKEN`; never printed or committed.

---

## 6. Browser tool (Playwright MCP) + warm logged-in Chrome
- **`.mcp.json`** registers Playwright MCP and **attaches** to a running Chrome (doesn't relaunch):
  ```json
  { "mcpServers": { "playwright": { "command": "npx",
      "args": ["-y","@playwright/mcp@latest","--cdp-endpoint","http://127.0.0.1:9222"] } } }
  ```
- **`bin/chrome_debug.sh`** launches real Chrome on the persistent profile `.browser-profile` with
  `--remote-debugging-port=9222`. launchd keeps it alive (§8).
- **🔧 MANUAL — log in once:** with that Chrome open, sign into **each marketplace you enabled**
  (e.g. FB Marketplace, Carousell, eBay). Logins persist in `.browser-profile`, so the agent acts
  as you (account-safety thesis).
  Verify CDP: `curl -s http://127.0.0.1:9222/json/version`.

---

## 7. 🔧 MANUAL — onboarding (writes `seller_config.json`)
Drive it conversationally — Telegram `/start` → **Set up** (or `/sell` in a Claude Code session):
currency, region/timezone, marketplaces, **exact pickup address** (private — used only for the
delivery-fee calc), **P2P delivery zones**, availability (**connect calendar / manual / skip**),
confirm you're logged into the marketplaces. This produces `data/seller_config.json`.

---

## 8. Install the always-on daemon (launchd)
```bash
cd ~/bazaar-skills
bin/chrome_debug.sh &                         # warm Chrome (first time, confirm logins)
launchd/install_daemon.sh install             # loads chrome + agent LaunchAgents (RunAtLoad+KeepAlive)
```
- ⚠️ GOTCHA — **plist PATH**: `launchd/com.bazaarskills.agent.plist` must list the dirs where
  `claude`/`npx`/`node` live (nvm bin + `~/.local/bin`). launchd has a minimal PATH otherwise →
  passes can't find `claude`/`npx`. Derive from your `which` output (§2).
- The two jobs: `com.bazaarskills.chrome` (warm browser) + `com.bazaarskills.agent`
  (`bin/agent_daemon.py`). They start at login and restart on crash.

---

## 9. Verify
```bash
cd ~/bazaar-skills
for t in floor_gate shipping telegram negotiate; do python3 tests/test_$t.py | tail -1; done  # ALL PASS x4
curl -s http://127.0.0.1:9222/json/version          # CDP up
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
  daemon first, then re-`install`.
- Push code changes: `rsync` dev → `~/bazaar-skills` (exclude `launchd/`, `.claude/settings.local.json`,
  `.browser-profile/`, `logs/`), then `install_daemon.sh install` to restart.

---

## 11. Friction inventory → automation candidates
What the future installer should do for each manual/gotcha step:

| Step | Today (manual/gotcha) | Installer should… |
|---|---|---|
| Select harness + sign in (§2) | autodetect, fail later if not logged in | **menu** to pick Claude Code or Codex → **gate on sign-in** (instruct + wait + re-check via `install.py harness --name`) → pass the choice to Stage 2 as `$BAZAAR_HARNESS` |
| Prereqs (§2) | check `which` by hand | **preflight** node/python/chrome + verify `claude` is logged in; offer to fix |
| Location (§3) | know about TCC, copy to `~/bazaar-skills` | pick a **non-TCC dir** automatically; do the copy |
| Bot token (§4) | BotFather, copy token | **guided** BotFather walkthrough + paste field |
| chat_id (§4) | `/start`, captured on poll | detect first `/start`, confirm "connected as @you" |
| Secrets/perms (§5) | hand-edit JSON (we broke it) | **generate** `settings.local.json` + validate JSON |
| Browser/CDP (§6) | `.mcp.json` + `chrome_debug.sh` | generate `.mcp.json`; launch warm Chrome |
| Marketplace login (§6) | log into your marketplaces manually | **open Chrome to each enabled marketplace, wait for login**, confirm |
| Onboarding (§7) | conversational | in-app **wizard** (currency/address/zones/availability) |
| Daemon + PATH (§8) | edit plist PATH to match `which` | **generate plists with detected paths**; one-click load |
| Verify (§9) | run tests/curl/tail | built-in **health check** + first-message smoke test |

---

## 12. Known gotchas / operational notes
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
- **Dev ≠ runtime**: edits in the dev source don't take effect until `rsync`'d to `~/bazaar-skills`
  and the daemon is reinstalled.
- **No secrets in git**: `.gitignore` excludes `settings.local.json`, `.browser-profile/`, `logs/`,
  `data/{channel_state,threads,negotiations,escalations,listing_session}` and photos.
