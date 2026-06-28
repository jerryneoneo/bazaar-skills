# Changelog

All notable changes to Bazaar Skills are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com); versions track the root `VERSION` file.

## [Unreleased]

### Added
- **Stale-chat follow-ups, then "not interested".** When OUR last message in a thread goes
  unanswered, the agent now sends up to 2 gentle nudges on a gentle-escalation cadence (~1d, then
  ~3d) and marks the counterpart **not interested** ~3d after the last nudge, on **both** sides
  (quiet buyers and quiet sellers). New deterministic engine `bin/followup_state.py` derives the
  nudge count from the transcript tail (never a stored counter, so it is crash-safe and needs no
  migration); the ledger `data/followup_state.json` (gitignored) holds only the disposition + a
  cache. Nudges are auto-sent through the normal `journal_send` bracket (a `$BAZAAR_FOLLOWUP=1`
  branch on the buyer/buy passes), respecting pacing caps, quiet hours, and `/pause`; the drop is
  $0 deterministic and posts ONE batched channel notice. Open-escalation and terminal threads are
  excluded. Config: `followup_enabled` (default on), `followup_nudge_intervals_days [1,3]`,
  `followup_drop_after_days 3`, `followup_max_nudges 2`, `followup_poll_sec`. Tests:
  `tests/test_followup_state.py`.
- **Stale-listing health suggestions.** A LIVE listing with no buyer interest for 7+ days now gets
  CONCRETE improvement suggestions (price vs comps, photos, title/description, reach, bump/relist)
  on your channel. New deterministic detector `bin/listing_health.py` (clock = last buyer inbound,
  with a `published_at`/`imported_at`/mtime fallback chain so it works on existing inventory with no
  migration); the MAINT pass composes + sends the suggestions one item per pass via the
  `data/listing_health_session.json` baton, deduped by `data/listing_health_state.json` so you are
  not nagged. New skill `skills/channel/listing-health.md`; `published_at` is now stamped at first
  publish/cross-list. Config: `listing_health_enabled` (default on), `stale_days 7`,
  `listing_health_interval_hours 24`, `rewarn_days 14`. Tests: `tests/test_listing_health.py`.
- **Catch-up surfaces both.** `bin/triage.py` (so `/status` + `/bazaar-catchup`) now reports
  `Follow-ups due` and `Stale listings` alongside the existing "awaiting you" signals.

### Changed
- **Precise, per-thread inbox peeks (cut wasted LLM passes).** The cheap non-LLM gates now read the
  Carousell inbox **per conversation row** (`bin/inbox_scan.py`, reusing `buyer_peek`'s stdlib CDP
  transport) and classify each by counterparty handle into buy / sell / promo, instead of gating on
  the aggregate unread badge. `buy_peek` fires a `liaising`/`agreed` want's pass **only on a fresh
  seller reply** (was: every cycle for any open want); `buyer_peek` and the forced-sweep
  `buyer_recheck` no longer fire a sell pass on promos or buy-side rows (FB/eBay keep the aggregate
  fallback; everything fail-open, with the 2h floor sweep as the backstop). New per-row memos
  `data/inbox_buy_state.json` / `data/inbox_sell_state.json` (gitignored). Tests:
  `tests/test_inbox_scan.py`, `tests/test_buy_peek.py`, `tests/test_buyer_peek.py`, plus precision
  cases in `tests/test_buyer_recheck.py`.

## [0.2.0] - 2026-06-28

### Added
- **Public `ROADMAP.md` + `ARCHITECTURE-OVERVIEW.md`.** A directional, tiered roadmap (shipped /
  next / later / hosted-rail-dependent) and a public system-architecture overview (layers,
  deterministic engines, trust invariants, a Mermaid diagram, and built-vs-planned), both linked
  from the README. The internal as-built `ARCHITECTURE.md` stays gitignored; the public overview
  uses a distinct filename so it actually ships.
- **`/bazaar-catchup` deep catch-up sweep.** One command that sweeps everything and tells you what
  needs you: it checks the interface and health (channel bound, browser reachable, each marketplace
  logged in, daemon loaded, paused or not), reads every local "awaiting you" signal, and does a deep
  per-marketplace reconciliation (unmanaged or undistributed listings and sold/removed drift via the
  distribution SCAN read, plus chats you started solo via the inbox SWEEP read). It reports one
  grouped digest ordered by urgency, then offers to act by handing off to the skill that already owns
  each fix (`/sell-resolve`, `/sell-run`, `/buy-run`, `/sell-detect`, `/inbox-detect`, `/resume`),
  each keeping its own approval gate. Acts on nothing during the sweep, never reads a secret,
  turn-based and resumable (`data/catchup_session.json`), `--dry-run` aware. On-demand only; wrap with
  `/loop /bazaar-catchup` for a periodic digest. Surfaced from the `/bazaar` menu ("What needs me") and
  the `/status` summary.
- **`bin/triage.py` (read-only "awaiting you" aggregator).** The file-state core of `/bazaar-catchup`:
  consolidates open escalations (both sides), unread managed threads (both sides, using a cursor-walk
  that correctly ignores threads already replied to), draft/undistributed listings, open checkouts,
  open wants, and overdue scan/eval cadence into one JSON digest. Standard library only,
  `BAZAAR_DATA_DIR`-relocatable, never opens `data/floors` or `data/budgets`. Replaces the ad-hoc
  `find_unread.py` / `find_unhandled.py` prototypes (sell-side unread only), now removed. Tested by
  `tests/test_triage.py`.
- **Telegram "/" command menu.** The bot now registers its everyday commands (`/status`, `/list`,
  `/search`, `/delist`, `/detect`, `/pause`, `/resume`) via the Bot API `setMyCommands`, so typing
  `/` in the chat shows a tappable menu with descriptions instead of nothing. New
  `telegram.py setcommands [--force]` subcommand (idempotent via a content hash in
  `channel_state`); the daemon re-registers it best-effort on each startup and onboarding's
  Telegram connect step seeds it on first install.
- **Instant wake mode (push-notification trigger path).** FB/IG can now reply the moment a
  buyer messages, often answering straight from the OS notification, instead of waiting for the
  next poll cycle. Built on a per-platform resolver so each marketplace uses the cheapest trigger
  actually available on this machine, with polling as the safe default.
- **Per-platform trigger resolver** (`bin/trigger_resolver.py`): `resolve(platform)` returns
  `"notification"` or `"poll"`, empirically (a platform is on the notification path only if a
  readable OS notification from its origin actually arrived in a window), poll-default and
  fail-open.
- **Notification-path plumbing.** `bin/notify_db.py` (read-only macOS Notification Center reader;
  Chrome web-push carries the source domain in the subtitle; needs Full Disk Access, fails open
  to `[]`), `bin/notify_watch.py` (the OS-notification counterpart of `buyer_peek`, idempotent
  past a per-market cursor), and `bin/tab_park.py` (keeps Meta tabs backgrounded in a dedicated
  warm Chrome so they keep firing readable push; a focused Meta tab delivers in-app with no push).
  Wired into `agent_daemon` + `supervisor` as a ~0-token per-loop check.
- **Instant-mode setup in onboarding + `/bazaar`.** `bin/notify_setup.py` (status / open-fda /
  grant-chrome; read-only, fail-open, macOS only) plus a new `WAKE_SPEED` onboarding anchor that
  offers Instant with pros-only copy, guides the Full Disk Access + Chrome notification grants,
  verifies via status, and falls back to Standard. Standard stays the default and never blocks
  onboarding.
- **Startup wake-mode self-check.** Daemon + supervisor log a one-line banner at startup
  (`⚡ wake mode: INSTANT` when Notification Center is readable, else `🛡️ wake mode: STANDARD
  polling`), run inside the daemon process so it reflects the daemon's own Full Disk Access after
  a restart.
- **Turnkey install on macOS.** `setup` offers to `brew install` missing Node + Chrome (installs
  Homebrew first if needed); consent-gated, `--yes` auto-accepts, `--no-install` skips, no TTY
  skips.
- **Runtime health check** (`bin/healthcheck.py`): read-only check that deps are present,
  Chrome/CDP is reachable, the install is onboarded, marketplace logins are confirmed, and the
  daemon is loaded, with fail/warn/ok levels. Never prints a secret; honors `BAZAAR_DATA_DIR`.
  Wired into `./setup` (returning-user path), onboarding verify, and a new `/bazaar health`
  option.
- **Mid-onboarding channel switch to Telegram.** After binding the interface the agent now says
  it can be changed anytime ("switch to Telegram" or `/bazaar` → interface), nudges console users
  toward Telegram for phone notifications, and supports a switch on request with no restart.

### Changed
- **Cut daemon session sprawl (Tier 1 + 2a) for fewer and cheaper passes.** Pinned the Playwright
  MCP to `@0.0.76` (skips the cold-start dist-tag lookup), stopped forced empty buyer sweeps
  (`force_buyer_pass_every` 2→0 with a 2h absolute floor as the strand backstop), switched the
  supervisor's forced sweep to one round-robin market instead of fanning out, right-sized the
  maint pass via `BAZAAR_MAINT_MODEL`, and added a `BAZAAR_MAX_WORKERS` kill-switch. New
  `bin/buyer_recheck.py` does a ~0-token CDP re-probe so the forced buyer pass fires only on real
  unread. Per-thread cursor idempotency, the pacing gate as sole send authority, fail-open probes,
  and the byte-stable 1h cache prefix are all preserved.
- **Sanitized config for public distribution.** launchd plists are now templates
  (`__RUNTIME__`/`__PATH__`) that `install_daemon.sh` substitutes at install time, and
  `.claude/settings.json` is trimmed to the project hooks plus a generic allow-list (removed
  accumulated per-session approvals, absolute paths, and the private docs reference).

### Fixed
- **Notification trigger no longer starves the poll fallback.** The notification trigger used to
  reset the shared poll timers (`last_buyer` / `last_buyer_pass`), which drive the aggregate poll
  gate and strand-floor for every market; a per-market FB notification would starve the poll path
  that backstops Carousell. The poll now runs independently on its own cadence as the fail-open
  fallback for all markets.

## [0.1.0] - 2026-06-27

### Added
- **Initial public release** — open-sourced under the MIT license; one-paste clone-and-`./setup`
  install, a `README` quickstart, a `CONTRIBUTING` guide, and a documented `data/` layout.
- **gstack-style install lifecycle.** One-paste install
  (`git clone … ~/bazaar-skills && cd ~/bazaar-skills && ./setup`), an idempotent re-runnable
  `setup`, and lifecycle verbs: `/bazaar-upgrade`, `bin/bazaar-uninstall`, `bin/bazaar-config`,
  `VERSION`, this `CHANGELOG`, and a `migrations/` runner.
- **Global slash-command launchers.** `bin/install.py gen-launchers` installs thin launchers into
  the harness's global skills dir (`~/.claude/skills/`) so `/bazaar`, `/sell`, `/buy`, … work from
  any project while execution still happens in the `~/bazaar-skills` runtime dir.
- **Harness-agnostic runtime seam.** `Harness.pass_argv(PassSpec)` replaces the Claude-only
  `headless_cmd`; the headless runner (`bin/harness_run.py`) builds a harness-agnostic spec and
  routes it through the active harness. `run_pass.sh` / `intent.sh` are now thin wrappers.
- Harness-aware `preflight.py` (checks the selected harness's auth, not just `claude`) and
  `agent_daemon.py` (reads the bot token from whichever store the harness wrote).
- **Canonical seller-initiated delist flow.** `skills/channel/delist.md` + `bin/delist_item.py`
  give "delete/remove my listing" a defined path: resolve the item by id, run each platform's
  take-down recipe, then write the durable `items/<id>.json` to `removed_by_seller` (one canonical
  status). Wired into `/delist` and free-text intent in `bazaar-run.md` §1 + `harness_run.py`.

### Fixed
- **Sell loop no longer replies to terminal (`lost`) threads.** The sell-side thread selection
  skipped only `status:"escalated"`, so a buyer messaging a dead thread (sold/delisted) got
  re-processed and answered with a throwaway "ok". The filter now skips `{escalated, lost}` —
  the mirror of the buy side's `{closed, escalated}` (`bazaar-run.md`, `sell-watch.md`, the
  `fb`/`carousell` listing-flow recipes). `reply-pipeline.md` gains a defense-in-depth terminal
  guard so a `lost` thread never composes a reply even if the pipeline is entered directly.
- **Delisted items no longer reported as live.** A seller-initiated take-down with no active
  listing session used to write the deletion marker into the transient single-slot
  `data/listing_session.json` instead of the durable item record, leaving the item stuck at
  `status:"live"` — so the agent confidently reported a removed listing as live (e.g. "MX Master 3
  is live on FB + Carousell at $85"). The new delist flow always writes the durable record;
  `tests/test_delist_item.py` guards the no-active-session case.

### Notes
- **Claude Code is the only harness wired for runtime today.** The architecture is agnostic by
  design (`bin/harnesses/`), and additional harnesses (Codex, …) slot in by implementing one
  `Harness` subclass — but they are not yet supported at runtime.
