# Channel adapter registry — detect & bind (probe & bind)

`skills/channel/channel.md` defines the six verbs every adapter must implement. **This file** is the
*binding* contract: how onboarding / `/bazaar` discover which chat interfaces the seller already has
("probe & bind") and how they connect a fresh one. Detection and binding happen **only at onboarding
/ `/bazaar`** — never in the hot loop, which just binds the already-chosen
`seller_config.channel.adapter` for the whole run (preserving the single-consumer invariant).

This is the chat-channel analogue of `skills/marketplaces.md` (which catalogs marketplaces).

## The contract

Each adapter declares two operations, used only by onboarding / the menu:

- **`detect()` → `{available, evidence, hint}`** — cheap, read-only probe. Does an env var / MCP
  server / OS resource exist? Never sends a message. `hint` is an actionable next step when
  `available` is false.
- **`connect()`** — the guided fresh-setup sub-flow: gather what's needed, write the binding, verify
  with one round-trip. Idempotent. Driven conversationally via the `channel.md` verbs of whatever
  adapter the seller is currently reachable on (usually `console` during install).

The binding is recorded in `seller_config.json` (secrets excluded):

```json
"channel": { "adapter": "telegram", "bound_at": "<iso>", "detail": { /* non-secret ids only */ } }
```

Secrets stay where they already live: tokens in `.claude/settings.local.json → env`; MCP creds in
the harness / `.mcp.json`. `channel.detail` holds only non-secret identifiers (iMessage `handle`,
WhatsApp `phone_id`).

## Registry

| id | shim | `detect()` probe | connector | secrets | non-secret `detail` |
|---|---|---|---|---|---|
| `console` | — (native) | always `available` | native | — | — |
| `telegram` | `bin/telegram.py` | `TELEGRAM_BOT_TOKEN` set **and** `channel_state.chat_id` captured | bot api | `settings.local.json.env` | — (chat_id in channel_state) |
| `imessage` | `bin/imessage.py` | iMessage MCP configured, else `imessage.py detect` (chat.db readable) | mcp \| chat.db+AppleScript | none (local login) | `handle` |
| `whatsapp` | `bin/whatsapp.py` | WhatsApp MCP configured, else `whatsapp.py detect` (`WHATSAPP_TOKEN`+`WHATSAPP_PHONE_ID`) | mcp \| cloud api | `settings.local.json.env` | `phone_id` |

All four satisfy the six verbs by construction; the shims are dumb pipes (transport only, no
judgment), exactly like `telegram.py`.

## Per-adapter detect()/connect()

### console
`detect()` → always `{available:true}`. `connect()` → write `adapter:"console"`; no-op otherwise.
The universal fallback — works in any harness (Claude Code `/sell`, Codex, Terminal).

### telegram  ← recommended; default-suggested at onboarding
`detect()` → token present (env or `settings.local.json.env`) AND `channel_state.chat_id` set →
*"token present + chat bound."*
`connect()` → the guided **BotFather walkthrough**, taught step by step (the agent drives it from
whatever adapter you're reachable on now, usually the console):
  1. Open Telegram and message **@BotFather** (the official, verified bot-maker — blue checkmark).
  2. Send `/newbot`, then follow its two prompts: a **display name** (e.g. "My Bazaar") and a
     **username** that must end in `bot` (e.g. `my_bazaar_bot`).
  3. BotFather replies with an **HTTP API token** that looks like `123456789:AAE…`. Copy it.
  4. Paste that token here when the agent asks. It is written to `.claude/settings.local.json → env`
     as `TELEGRAM_BOT_TOKEN` — never printed, logged, or committed (gitignored).
  5. Open your new bot via the `t.me/<username>` link BotFather gives you and tap **Start** (or send
     `/start`). The agent runs `telegram.py poll` until it captures your `chat_id` (single-tenant —
     it then ignores every other chat) and confirms *"connected as @you."*
  6. The agent runs `python3 bin/telegram.py setcommands` once so your everyday commands (`/status`,
     `/list`, `/search`, `/delist`, `/detect`, `/pause`, `/resume`) show up in Telegram's `/`
     autocomplete menu right away (the daemon also re-registers this idempotently on each restart).

### imessage (macOS only)
`detect()` priority: (1) an iMessage MCP server registered in the harness → bind that; (2) else
`python3 bin/imessage.py detect` — `available:true` if `chat.db` is readable. **A present-but-
unreadable chat.db (`Operation not permitted`) returns `available:false` with a Full-Disk-Access
hint** — never a silent "no iMessage." `connect()`: MCP path binds; chat.db path guides the FDA
grant (System Settings > Privacy & Security > Full Disk Access for the host app), waits, re-probes,
then asks which contact is the seller's control thread → store as `channel.detail.handle`. AppleScript
sending also needs a one-time Automation grant for Messages, which `connect()` triggers and verifies.
Verbs: `say/ask/notify` → `imessage.py send` (options render as a numbered list); `watch()` →
`imessage.py poll --handle <handle>` (cursor = max message `ROWID`); `ask_images` → `imessage.py
getfile`; `confirm` → `ask` yes/no.

### whatsapp
`detect()` priority: (1) a WhatsApp MCP server → bind that; (2) else `python3 bin/whatsapp.py detect`
(`WHATSAPP_TOKEN` + `WHATSAPP_PHONE_ID` in env). `connect()`: MCP path binds; Cloud-API path guides
Meta-app + system-user token creation, pastes `WHATSAPP_TOKEN`/`WHATSAPP_PHONE_ID` into `env`, and
stores the non-secret `phone_id` in `channel.detail`. Single-tenant: the authorized counterparty
number is captured on first inbound (mirrors `telegram.py`'s chat_id capture). Verbs: `say/ask/notify`
→ `whatsapp.py send` (≤3 options → interactive reply buttons, else numbered list); `watch()` →
`whatsapp.py poll` over the webhook inbox cache (cursor = last message id); `confirm` → `ask` yes/no.

## Per-adapter cursors & the single-consumer invariant

`data/channel_state.json` holds **one cursor per adapter**, but only the **bound** adapter's cursor
advances:

```json
{
  "adapter": "telegram",
  "chat_id": 188452196, "update_offset": 808217216,   // telegram (flat, legacy)
  "imessage": { "rowid": 0, "handle": "+6591234567" }, // imessage section
  "whatsapp": { "msg_id": null, "to": "6591234567" },  // whatsapp section
  "pending": [ /* notify refs awaiting seller action — adapter-independent */ ]
}
```

The daemon's non-consuming `peek` is dispatched by adapter: `agent_daemon.py` runs
`<bound-adapter>.py peek` (`telegram.py peek` / `imessage.py peek --handle …` / `whatsapp.py peek`),
not a hard-coded `telegram.py`. The single-flight run-lock (`.daemon.runlock`) is transport-
independent and unchanged. `console` has no daemon (it's the interactive `/sell` surface).

## Switching adapters while the daemon runs
`/bazaar interface` must not leave the daemon polling the old channel: if `install_daemon.sh status`
shows a loaded daemon, do **uninstall → rewrite `channel.adapter` → reinstall** (the same dance
`DAEMON.md` mandates for switching to `/sell`).

## Safety
- Adapters are transport-only; they never decide. All judgment stays in the flow specs.
- Tokens are read from env by the shims and never printed, logged, or written to data files
  (the `ShimError` pattern strips any token echo). iMessage uses no token (local macOS login).
