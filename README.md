```
██████╗  █████╗ ███████╗ █████╗  █████╗ ██████╗
██╔══██╗██╔══██╗╚══███╔╝██╔══██╗██╔══██╗██╔══██╗
██████╔╝███████║  ███╔╝ ███████║███████║██████╔╝
██╔══██╗██╔══██║ ███╔╝  ██╔══██║██╔══██║██╔══██╗
██████╔╝██║  ██║███████╗██║  ██║██║  ██║██║  ██║
╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝
   s k i l l s · your personal P2P marketplace agent
```

# Bazaar Skills

_An open-source project by [Carousell](https://carousell.com)._

**A personal agent that sells _and_ buys for you across informal P2P marketplaces — running inside your own Claude Code session, driving your real logged-in Chrome.**

You chat with it on Telegram or the console. It lists your items on the marketplaces that fit your region, replies to buyers and sellers in a natural voice, and negotiates within limits you set — your lowest sell price and highest buy budget stay secret. Everything ships **P2P**, so every deal has one clear total: price + delivery.

### Supported today (`v0.1.0`)

| | Now | On the roadmap |
|---|---|---|
| **Channels** | Telegram · console | iMessage · WhatsApp |
| **OS** | macOS | Linux · Windows |
| **Harness** | Claude Code | Codex · others |

The architecture is channel-, OS-, and harness-agnostic by design (adapter seams throughout), so the roadmap items slot in without reworking the core. They're just not wired for runtime yet.

---

## Quickstart

```bash
git clone https://github.com/jerryneoneo/bazaar-skills.git ~/bazaar-skills && cd ~/bazaar-skills && ./setup
```

`./setup` is an **idempotent installer**: it checks prerequisites, gates on Claude Code being signed in, installs global slash-command launchers (so `/bazaar`, `/sell`, `/buy` work from any project), **sets up the permissions that let the agent run autonomously without per-tool prompts**, and — on first run only — hands off to a guided, conversational onboarding. Re-run it any time (e.g. after `git pull`); it refreshes launchers, config, and permissions, then runs migrations.

> **Install location matters on macOS:** keep the runtime outside `~/Documents`, `~/Desktop`, and `~/Downloads` — macOS privacy (TCC) blocks background processes from reading those. `~/bazaar-skills` is the safe default, and `./setup` refuses a blocked path.

Onboarding then walks you through: pick your **interface** (Telegram or console) → choose **marketplaces** for your region and log in (in your own Chrome — the agent never logs in for you) → set your **autonomy** level → optionally enable **buying**. When it finishes:

- **Sell:** send `/sell-list` with a few photos. Vision IDs the item, pulls comps, drafts the listing; you set a public price and a **private floor**; it publishes to your enabled marketplaces and answers buyers from there.
- **Buy:** send `/buy-search` and what you want. It searches your marketplaces, shortlists the best matches, then negotiates within your secret budget and coordinates the handover.
- **Check in anytime:** run **`/bazaar-catchup`** for a full sweep of your listings, marketplaces, and setup — it reports everything waiting on you (unread buyers, decisions, drafts, logged-out marketplaces) and offers to handle each.
- **Anything later:** open the **`/bazaar`** menu to change your interface, marketplaces, buying, autonomy, or persona.

---

## Prerequisites

| Need | Why |
|---|---|
| **macOS** | the always-on daemon runs on launchd (interactive mode also works here) |
| **Claude Code CLI, signed in** | the agent runtime — headless passes reuse this auth (no separate API key) |
| **Python 3** | all of `bin/*.py` (standard library only — no `pip install`) |
| **Node + npx** | runs the Playwright MCP browser tool |
| **Google Chrome** | the real, logged-in browser the agent drives over CDP |
| A Telegram bot token | optional — the console interface needs none |

On macOS, `./setup` offers to install missing **Node** and **Google Chrome** for you via Homebrew (installing Homebrew first if needed); pass `--no-install` to skip. You install + sign in to the Claude Code CLI once yourself.

---

## How it works

Three layers, deliberately simple:

- **Channel** — how you talk to the agent. Adapter-agnostic flows (`skills/channel/*.md`) are written against a small set of abstract verbs, so Telegram and the console behave identically (and new channels drop in the same way).
- **Browser** — how it acts on marketplaces. A shared action vocabulary (`skills/browser-actions.md`) plus per-site recipes (`skills/listing-flows/*.md`) drive your real Chrome session, so the agent acts as you with your existing logins.
- **Deterministic money code** — the only logic that touches secrets is plain, tested Python with JSON in/out: `bin/floor_gate.py` (accept/counter/reject against your hidden floor) and `bin/shipping.py` (delivery fee + buyer total from your zone table). **Your floor and exact address never enter a prompt, reply, listing, or transcript** — buyers only ever see a delivery *fee*.

You choose how hands-off it is. Onboarding sets an autonomy preset — `hands-free`, `balanced`, or `all-steps` — wiring both the business approval gates *and* the harness permission layer together, so an unattended run isn't blocked by per-tool prompts. An above-list or bidding close **always** asks you first.

---

## The seller journey

1. **List in one message.** Send `/sell-list` with a few photos. Vision identifies the item, pulls recent comps, and drafts the title and description.
2. **Set your numbers.** You confirm a public **list price** and a private **floor** — your secret minimum, which never leaves `bin/floor_gate.py`. Confirm the delivery size; shipping fees come from your zone table.
3. **Publish.** The agent posts to your enabled marketplaces and works buyers from there — auto-answering from a learned Q&A bank and negotiating within your floor at the autonomy level you chose. An above-list or bidding offer always checks with you first.
4. **Close.** When a deal is agreed, the agent issues a checkout link, coordinates delivery, and marks the item sold across every marketplace it was listed on.


### Listing to carousell.ai — distribution

Alongside the marketplaces you post to by hand, Bazaar can publish your item to **carousell.ai**, a GEO-optimized, agent-discoverable storefront.

- **More reach, faster sale** — your listing is found by buyers *and* buyer agents browsing carousell.ai, not just the few sites you cross-list to manually.
- **One canonical page** that the agent keeps in sync as price and status change.

*Skip it* and you only reach the marketplaces you cross-post to yourself.

> **Status:** carousell.ai publishing runs through the hosted rail (a separate workstream) and is **not yet wired in this build** — today you reach buyers on the marketplaces you cross-post to yourself.

### Checking out via carousell.ai — vs. manual

At close, the recommended path is a **carousell.ai checkout link** instead of arranging payment and delivery yourself.

- **Escrow + buyer protection** — funds are held until delivery, so fewer no-shows and scams for both sides.
- **Tracked shipping handled for you** — a label is generated; no haggling over logistics.
- **One clear total** (price + delivery), zero seller fees, and agent involvement disclosed cleanly on the checkout page.

*Skip it* and you coordinate payment and handover manually (e.g. bank transfer + your own shipping) — more work, less protection.

> **Status:** the carousell.ai checkout rail (escrow, tracked label, funds-hold) is a separate hosted workstream and is **not yet wired in this build** — today the checkout link is a stub (`bin/checkout.py`), so the agent closes via **manual handover**. The escrow / tracked-shipping / buyer-protection benefits above describe the rail once it lands, not what runs today.
>
> You stay in control regardless: carousell.ai listing and checkout are defaults *because they're better*, never mandatory.

---

## Skills

Bazaar is a suite of Claude Code slash-command skills. After install they work from any project.

**Setup & control**
| Skill | What it does |
|---|---|
| `/bazaar` | Settings & capabilities menu — view/change interface, marketplaces, buying, autonomy, persona; check what needs you (`/bazaar-catchup`) or run a health check |
| `/bazaar-install` | Guided onboarding (Stage 2; the installer hands off here on first run) |
| `/bazaar-upgrade` | Update to the latest version (git pull → re-run setup → restart daemon) |
| `/pause` | Pause the agent mid-flight (stop acting; queue corrections) |
| `/resume` | Resume the agent, applying any corrections left while paused |

**Agent loops**
| Skill | What it does |
|---|---|
| `/bazaar-run` | The unified agent loop — control channel + sell inboxes + buy threads in one pass |
| `/sell-run` | Seller-scoped loop (channel + buyer inboxes) |
| `/buy-run` | Buyer-scoped loop (channel + seller-reply threads) |
| `/sell-watch` | Buyer-side inbox watch loop across marketplaces |

**Selling**
| Skill | What it does |
|---|---|
| `/sell` | Drive the seller agent interactively in this session |
| `/sell-list` | List an item: photos → vision → comps → price → floor → shipping → publish |
| `/sell-detect` | Detect existing listings on connected marketplaces, manage + cross-list them |
| `/sell-resolve` | Answer escalated buyer questions, grow the Q&A bank, resume the thread |

**Buying**
| Skill | What it does |
|---|---|
| `/buy` | Drive the buyer agent interactively in this session |
| `/buy-search` | Search connected marketplaces: need → search → rank → recommend → confirm |
| `/buy-detect` | Review inboxes for purchase chats you started and offer to take them over |

**Cross-cutting**
| Skill | What it does |
|---|---|
| `/bazaar-catchup` | Deep sweep of every listing, marketplace, and setup surface — reports what needs you and proposes the work (acts on nothing itself) |
| `/inbox-detect` | Review every marketplace inbox and offer to take over chats you started on your own |
| `/bazaar-eval` | Evaluate recent conversations & passes, surface UX/behavior issues to fix |

Under the hood these are backed by a library of skill modules in [`skills/`](skills/) — channel flows (`channel/`), browser actions, the reply/liaison negotiation pipelines, per-marketplace listing recipes (`listing-flows/`), the marketplace registry, and the voice/persona rules.

---

## Running it

- **Always-on (recommended, macOS):** a launchd daemon wakes on a schedule and runs background passes (poll inboxes, reply, negotiate) with no session open. See **[DAEMON.md](DAEMON.md)** for day-to-day operations.
- **Interactive:** keep a `/bazaar-run` session open (`/sell-run` or `/buy-run` for one side only). It runs the same loop in-session and acts while the session is alive.

Full manual runbook — every prerequisite, secret, and gotcha — is in **[SETUP.md](SETUP.md)**.

---

## Trust & safety

- **Floor stays local:** read only by `bin/floor_gate.py` from `data/floors/<item>.json` — never in a prompt, reply, listing, or transcript.
- **Address stays local:** your exact pickup address lives only in `data/seller_config.json`, read only by `bin/shipping.py` for the distance calc; buyers see a delivery fee, not your address.
- **Your real session:** the agent drives your logged-in Chrome with jittered, rate-capped pacing and stops to re-authenticate on checkpoints — it never logs in for you.
- **P2P only:** no offline meetups; one clear total of price + delivery.
- **Secrets in env, gitignored, never printed.** Nothing under [data/](data/) that carries money, identity, or conversation content is committed — see [data/README.md](data/README.md) for exactly what stays local and how it's generated.

---

## Verify

```bash
python3 tests/test_floor_gate.py   # floor never leaks; a counter is always >= floor
python3 tests/test_shipping.py     # buyer_total = price + fee; unserviceable -> decline; no address leak
python3 tests/test_telegram.py     # keyboard/normalize/single-tenant; token safety
python3 tests/test_triage.py       # the /bazaar-catchup sweep aggregates pending tasks; never reads a floor/budget
```

Check the install is actually runnable any time — Chrome/CDP, marketplace logins, daemon (read-only, no secrets):

```bash
python3 bin/healthcheck.py
```

---

## Project layout

```
.claude/commands/  the slash-command skills above
skills/            channel flows · browser actions · reply/liaison pipelines · listing recipes
bin/               deterministic engines (floor_gate, shipping, …) + installer + daemon
data/              committed templates + gitignored runtime state (see data/README.md)
tests/             plain-python adversarial tests for every engine
```

---

## Contributing

Contributions welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for dev setup, running the tests, and PR conventions.

## License

[MIT](LICENSE).
