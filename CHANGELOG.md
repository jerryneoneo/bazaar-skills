# Changelog

All notable changes to Bazaar Skills are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com); versions track the root `VERSION` file.

## [0.1.0] — 2026-06-27

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
