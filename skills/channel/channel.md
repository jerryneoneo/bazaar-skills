# SellerChannel — the channel-agnostic interaction vocabulary

All seller-facing flows (intro, onboarding, listing, notifications) are written against the
six verbs below — **never** against Telegram directly. An *adapter* binds the verbs to a
transport. This is the human-side analogue of `browser-actions.md` (which abstracts Chrome).
Same flow files run unchanged on Telegram, the Claude Code console, or Slack.

> **One seller per deployment.** Each person runs their own bot; there is exactly one
> authorized seller. The Telegram adapter captures that seller's `chat_id` on first `/start`
> and ignores everyone else.

## The six verbs

| Verb | Meaning | Returns |
|---|---|---|
| `say(text)` | Push an informational message. No reply expected. | — |
| `ask(prompt, options?)` | Ask a question. `options` (key=Label pairs) render as buttons; omit for free text. | the chosen key, or the free-text string |
| `ask_images(prompt)` | Request one or more photos. | list of local file paths (already downloaded) |
| `confirm(prompt)` | Yes/no gate before a side effect (publish, send). | `true` / `false` |
| `notify(text, actions?)` | Proactive push, optional action buttons (`accept=Accept,...`). Correlated by a `ref`. | chosen action key, or `null` if unanswered |
| `watch()` | Long-poll inbound seller events past the cursor. | ordered `{event_id, kind, text, payload, ts}` |

`kind` ∈ `command` (`/list`, `/onboard`, `/status`, `/pause`, `/resume`) · `text` (free reply) ·
`photo` (`payload.file_id`) · `action` (`payload.{ref, choice}`, answering a prior `notify`).

> **`/pause` & `/resume` are enforced deterministically, not by LLM judgment.** They write the
> single pause flag `data/control.json` (owner: `bin/control.py`). While paused, the daemon holds
> all action passes, interrupts any running pass within ~one poll cadence, and a PreToolUse hook
> blocks marketplace sends; free text you send becomes a CORRECTION applied on `/resume`
> (`skills/channel/corrections.md`). The deterministic drain `bin/channel_control.py` consumes
> `/pause`,`/resume` + captures corrections with no LLM, so a paused agent costs ~$0. A pause
> survives a daemon restart because it is a file.

## Adapter selection

Read `seller_config.json → channel.adapter`. Bind the verbs to that adapter for the whole run.
**Discovery & binding** (which adapters the seller already has, and how to connect a fresh one) is
the "probe & bind" contract in `skills/channel/adapters.md`; it runs only at onboarding / `/selly`,
never in the hot loop. The four adapters today: `console`, `telegram`, `imessage`, `whatsapp`.

### Telegram adapter (`bin/telegram.py`) — the first adapter

| Verb | How |
|---|---|
| `say` | `telegram.py send --text "..."` |
| `ask(prompt, options)` | `send --text prompt --options k=Label,...`; the answer arrives as the next `action` event (button) or `text` event (free text) from `watch()` |
| `ask_images` | `send` the prompt; collect subsequent `photo` events; for each, `telegram.py getfile --file-id <id> --dest data/photos/<item_id>/NN.jpg` → use the returned path |
| `confirm` | `ask` with `--options yes=Yes,no=No` → map to bool |
| `notify(text, actions)` | `send --text "..." --options <actions> --ref <escalation_id>`; the answer returns as an `action` event whose `payload.ref` matches |
| `watch()` | `telegram.py poll --timeout 25` → `{events, new_cursor}`; the offset cursor in `channel_state.json` makes restarts idempotent (never re-process an acked event) |

**Feel-alive rule (Telegram):** immediately before every `say`/`ask`/`notify`, fire
`telegram.py typing` so the seller sees the native "typing…" indicator while you compose (chat
rhythm, not a wall of canned text). The daemon also pulses `typing` for the whole pass to bridge
the cold start. Per `skills/voice.md` (Rule 2), you never send a fixed/templated ack: send a
short, **LLM-authored, contextual** acknowledgement before **any** slow step (not just the first
message of a pass), e.g. "📸 Got your photos, pricing now and I'll list it on your marketplaces and
message you when ready." (Name the seller's enabled marketplaces when you have them; never hardcode
a fixed pair.) And per `skills/voice.md` Rule 1, no em-dashes in any message. Per `skills/style.md`,
`data/style.json` `voice` (tone/humor/persona) may shape how these seller-facing lines read, within
the same `voice.md` invariants.

### Console adapter (Claude Code) — native streaming, no shims
The interactive Claude Code session **is** the channel; its UI streams your thinking, tool calls,
and replies, so no typing/ack plumbing is needed. Verb mapping:
- `say(text)` → just say it (a normal assistant message).
- `ask(prompt, options?)` → ask the seller in-session; their **next message** is the answer
  (offer options as a short list; free text also fine).
- `ask_images(prompt)` → ask the seller to attach or paste local image paths; use those paths.
- `confirm(prompt)` → ask, treat yes/no.
- `notify(text, actions?)` → say it inline with the choices; the seller's reply resolves it (no
  `pending[]`/ref bookkeeping — it's a live turn).
- `watch()` → the seller's messages in the session.
Selected by `seller_config.channel.adapter = "console"` (the `/sell` command forces it). Same
flows + same `bin/` engines; only the I/O surface differs.

### iMessage adapter (`bin/imessage.py`) — macOS only
`say`/`ask`/`notify` → `imessage.py send --handle <handle>` (no inline buttons, so `options` render
as a numbered text list; the flow parses the seller's number/text reply back to the option key) ·
`confirm` → `ask` yes/no · `ask_images` → `imessage.py getfile --rowid <id>` (copies the inbound
attachment) · `watch()` → `imessage.py poll --handle <handle>` → `{events, new_cursor}` (the cursor
is the max message `ROWID` in `channel_state.json → imessage.rowid`). Reading `chat.db` needs Full
Disk Access; sending needs a one-time Automation grant for Messages (both handled by `connect()` in
`adapters.md`). **No typing indicator** (the feel-alive rule is a no-op here). No token (local login).

### WhatsApp adapter (`bin/whatsapp.py`)
`say`/`ask`/`notify` → `whatsapp.py send` (≤3 `options` → interactive reply buttons; more → numbered
list) · `confirm` → `ask` yes/no · `ask_images` → media downloaded by the webhook receiver, path in
the event `payload` · `watch()` → `whatsapp.py poll` over the webhook inbox cache (cursor =
`channel_state.json → whatsapp.msg_id`; single-tenant: authorized number captured on first inbound).
Token in env (`WHATSAPP_TOKEN`/`WHATSAPP_PHONE_ID`), never written to data. **No typing indicator.**
A configured WhatsApp MCP server is used instead of the Cloud API path when present (see `adapters.md`).

### Slack adapter (future)
`say`/`ask`/`notify` → `chat.postMessage` + Block Kit buttons · `ask_images` → `files.info` →
download · `watch()` → Events API queue or `conversations.history` with a `ts` cursor.

## Correlation & idempotency
- `notify` writes a `pending[]` entry in `channel_state.json` (`{ref, kind, thread_id, actions}`)
  so a later `action` event maps back to the escalation that asked for it; clear it on resolve.
- Seller side uses the Telegram **offset** cursor; buyer side uses per-thread `last_handled_msg_id`
  cursors. Both survive kill/restart with no double-processing.

## Safety
- Adapter is transport-only; it never decides. All judgment is in the flow specs.
- The bot token is read from `$TELEGRAM_BOT_TOKEN` by `telegram.py` and is never printed,
  logged, or written to data files.
