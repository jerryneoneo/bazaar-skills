# `data/` — runtime state & templates

This directory holds everything the agent reads and writes at runtime. A fresh clone ships only
the **committed templates** and empty directory placeholders (`.gitkeep`); all of your money,
identity, listings, and conversations are written here locally and are **gitignored** — they never
leave your machine. See the repo [`.gitignore`](../.gitignore) for the authoritative list.

## Committed (ship with the repo)

| Path | What it is |
|---|---|
| `marketplaces.json` | Static registry of supported marketplaces (region, auth type, login URLs). |
| `config.json` | Default business config — autonomy preset, approval gates, pacing/poll intervals, disclosure strings. |
| `style.json` | Default persona/voice profile (tone, humor, negotiation firmness). |
| `fixtures/` | Example data for tests and dry-runs (e.g. `scripted_inbox.json`) — no real data. |
| `items/sample-ikea-desk.json` | One sample listing used by the test suite. |
| `floors/sample-ikea-desk.json` | Sample floor for that listing (floor `65` — test data, **not** a real secret). The only floor file that ships. |
| `*/.gitkeep` | Empty placeholders so the runtime directories exist on a fresh clone. |

## Gitignored (generated locally, never committed)

Onboarding, the listing/search flows, and the reply/liaison pipelines create these as you use the
agent. They are excluded because they carry secrets, identity, or private conversations.

### Money & identity (the core invariants)
| Path | Holds | Written by |
|---|---|---|
| `seller_config.json` | Currency, region, **exact pickup address**, delivery zones, channel binding. | Onboarding (`/selly-install`); the first-run gate. |
| `buyer_config.json` | Delivery area, payment methods, markets to search. | Onboarding (buying step). |
| `floors/<item>.json` | Your **secret minimum sell price** per item. | Set when you list; read only by `bin/floor_gate.py`. |
| `budgets/<want>.json` | Your **secret maximum buy budget** per want. | Set when you search; read only by `bin/budget_gate.py`. |

### Listings, wants & photos
| Path | Holds | Written by |
|---|---|---|
| `items/<item>.json` | Your listings (title, price, description, live URLs, status). | Listing flow (`skills/listing-flows/*.md`, `bin/listing.py`). |
| `photos/<item>/*` | Item photos uploaded to marketplaces. | Listing flow (photo upload step). |
| `wants/<want>.json` | Buyer searches + ranked candidate listings. | Search flow (`skills/buying/search.md`, `bin/buyer_negotiate.py`). |

### Conversations & negotiations
| Path | Holds | Written by |
|---|---|---|
| `threads/`, `buyer_threads/` | Marketplace chat transcripts (seller-side / buyer-side). | Reply/liaison pipelines via `bin/browser_actions.py`. |
| `negotiations/`, `buyer_negotiations/` | Active price negotiations. | Negotiation engines. |
| `channel_transcript.jsonl` | The agent ↔ you control-channel log (with secret-scrubbing). | `bin/channel_log.py`. |
| `qa_bank.jsonl` | Learned answers to buyer questions. | Reply pipeline. |
| `escalations.jsonl`, `buyer_escalations.jsonl` | Pending items needing your decision. | The agent loop. |
| `checkouts/<id>.json` | Issued checkout records at close. | `bin/checkout.py`. |

### Session & daemon state (ephemeral, regenerated)
`*_session.json`, `channel_state.json`, `control.json`, `scan_state.json`, `pacing_state.json`,
`buyer_peek_state.json`, `takeover_seen.json`, `availability.json`, `eval/`, `eval_state.json`,
`ui_cache/`, and lock/tmp files. These are transient working state — safe to delete; the agent
recreates them.

## On a fresh clone

You won't have any of the gitignored files yet — that's expected. The absence of
`data/seller_config.json` is exactly what tells `./setup` you're a first-time user, so it launches
the guided onboarding that creates them. Just run `./setup` and follow the prompts.
