---
description: Bazaar — guided install & onboarding (Stage 2; the bootstrap hands off here)
---

# /bazaar-install — guided onboarding (Stage 2)

The conversational onboarding the bootstrap installer hands off to. The user never types
`/bazaar-install` on a fresh machine — they install by cloning the repo and running `./setup`:

- `git clone https://github.com/jerryneoneo/bazaar-skills.git ~/bazaar-skills && cd ~/bazaar-skills && ./setup`
- (Optional, if you self-host the bootstrap: `curl -fsSL https://<your-host>/install.sh | bash`.)

> `./setup` (Stage 1) hands off here automatically on first run. You can also reach this runbook by
> running `install.sh` from a local checkout, or by opening this file directly in an agent session.

`install.sh` / `install.ps1` check prerequisites and clone the repo to a safe runtime dir, then have
you **select a runtime** (Claude Code or Codex), **sign in** to it, and only then launch it with
`follow .claude/commands/bazaar-install.md` — passing the verified choice as `$BAZAAR_HARNESS`. So
by the time this runs, the file is on disk and the harness is already chosen + authenticated (§0
trusts it). After setup it's also a normal slash command for re-runs; `/bazaar` handles piecemeal
changes.

Bazaar is **OS-agnostic and harness-agnostic**: every OS-specific step goes through `bin/platforms/`
(launchd / Task Scheduler) and every agent-runtime-specific step goes through `bin/harnesses/`
(Claude Code / Codex). This command drives the flow via the `channel.md` verbs on the `console`
adapter, generating + validating all config through `bin/preflight.py` and `bin/install.py`.

> **No hand-edited config, no memorized gotchas, no token ever printed.**

Read first: `skills/channel/intro.md`, `skills/channel/adapters.md`, `skills/marketplaces.md`,
`skills/bazaar-config.md`, `skills/channel/onboarding.md`.

## Flow

```
say  intro  (reuse skills/channel/intro.md body — "I'm Bazaar …")

# 0. HARNESS — which agent runtime are you using?
#    Normal path: Stage 1 (install.sh / install.ps1) already had you SELECT a runtime and SIGN IN,
#    and exports it as $BAZAAR_HARNESS — so trust it and DON'T re-ask.
if  env $BAZAAR_HARNESS is set:
     run  python3 bin/install.py harness --name "$BAZAAR_HARNESS"   # confirm still signed in (exit 0)
     -> exit 0 -> say "Using <harness>." ; <harness> = $BAZAAR_HARNESS.
     -> non-zero -> say it's no longer signed in, how to sign in; stop until resolved.
else (runbook opened directly, no Stage 1):
     run  python3 bin/install.py harness        # detects Claude Code / Codex: present + signed_in
     -> exactly one signed in    -> use it (say "Using <harness>").
        more than one signed in  -> ask "Which agent are you using?" options=[claude-code, codex].
        none signed in           -> say which CLIs exist and how to sign in; stop until resolved.
remember <harness>; pass --harness <harness> to every install.py call below.

# 1. TARGET — how should Bazaar run?  (Bazaar is designed to run ALWAYS-ON.)
ask  "How should Bazaar run?"
     options=[always_on=Always-on (background, recommended), interactive=Only while a session is open]
  # always_on installs a background supervisor via the platform module (launchd on macOS,
  # Task Scheduler on Windows). interactive = a kept-open /sell-run (or /loop /sell-run) session.

# 2. PREFLIGHT (SETUP.md §2)
run  python3 bin/preflight.py
     -> show each check; for any ok:false, show fix_hint and let the user fix + re-run.
        platform unsupported (exit 3) -> stop with the message.

# 3. RUNTIME LOCATION (SETUP.md §3 — TCC on macOS only)
run  python3 bin/install.py runtime-dir        # install.sh already cloned to a safe dir; confirm
                                                # tcc_blocked == false (always false on Windows).

# 4. CHOOSE INTERFACE + CONNECT (probe & bind — skills/channel/adapters.md)
goto skills/channel/onboarding.md#CHOOSE_INTERFACE
     -> runs each candidate adapter's detect(); offers "Detected <X> — use it?" for what exists;
        runs connect() only for the chosen-but-unbound adapter (BotFather / FDA grant / WA creds).
        (Telegram + console are the channels supported today; iMessage + WhatsApp land later.) Writes seller_config.channel.

# 5. SECRETS (SETUP.md §5 — harness-specific, behind bin/harnesses)
run  python3 bin/install.py gen-settings --harness <harness>
     # Claude Code -> .claude/settings.local.json (env + allow-list);
     # Codex       -> .codex/.env (+ approval mode). Tokens read from env, never printed.
     # (The autonomy allow-list / approval mode is finalized in §8.)

# 6. BROWSER (SETUP.md §6)
run  python3 bin/install.py gen-mcp --harness <harness>   # writes MCP config in the harness's
                                                           # format (.mcp.json / config.toml)
run  bin/chrome_debug.sh &                                 # warm Chrome on the persistent profile
     -> verify CDP: curl -s http://127.0.0.1:9222/json/version

# 7. CHOOSE MARKETPLACES + LOG IN (SETUP.md §6 login loop)
goto skills/channel/onboarding.md#CHOOSE_MARKETPLACES   # region-filtered from data/marketplaces.json
for each enabled marketplace with connector.auth=chrome_session:
     open its login page in the warm Chrome ; confirm "logged in?" ; set marketplaces[id].auth

# 8. AUTONOMY — the step that makes Bazaar run by itself (skills/bazaar-config.md)
goto skills/channel/onboarding.md#APPROVALS
     -> ask the autonomy LEVEL: hands-free / balanced / all-steps. Sets BOTH layers at once:
        • business approvals: write config.approvals.preset + steps (list/search/replies auto on
          hands-free; only above-list money ever escalates).
        • harness permissions: re-run `python3 bin/install.py gen-settings --harness <harness>
          --autonomy <level>` so the agent can list, search (scan/cross-list), and check chats
          WITHOUT a prompt per step (allow-list for Claude Code; approval mode for Codex).
     say what hands-free unlocks vs what still asks (above-list bids + above-budget buys always confirm).

# 8b. BUYING (optional) — set up the buyer side too
ask  "Want me to BUY for you as well (search marketplaces + negotiate on your behalf)?"
     options=[yes=Set up buying, skip=Selling only]
  yes  -> goto skills/channel/onboarding.md#BUYER_PROFILE   # delivery area, payment methods, search
          # markets + login; writes data/buyer_config.json (reuses channel/region/currency from above)
  skip -> say "You can enable buying any time via /bazaar -> buying."

# 8c. GLOBAL LAUNCHERS — make /bazaar, /sell, /buy work from any project (gstack-style)
#     `./setup` already did this before handing off; re-run here too so the direct-onboarding path
#     (plain folder copy, no setup) also gets global commands. Idempotent.
run  python3 bin/bazaar-config set home "$(pwd)"                # record runtime dir (~/.bazaar/home)
run  python3 bin/install.py gen-launchers --harness <harness>   # launchers → harness skills dir

# 9. SUPERVISOR (SETUP.md §8)  [only if target == always_on]
run  python3 bin/install.py supervisor --no-dry-run   # platform module: launchd plists (macOS) or
                                                       # schtasks ONLOGON jobs (Windows); surfaces
                                                       # any platform notes (e.g. Windows run_pass.ps1).
     -> interactive target: skip; tell the user to keep a `/bazaar-run` (or `/loop /bazaar-run`) session
        (the unified sell+buy loop; `/sell-run` / `/buy-run` run one side only).

# 10. VERIFY (SETUP.md §9)
run  python3 bin/install.py validate --harness <harness>   # generated config is valid
run  for t in floor_gate shipping telegram negotiate marketplaces approvals budget_gate buyer_negotiate: python3 tests/test_$t.py
run  a first-message smoke test on the bound adapter (telegram round-trip / imessage chat.db read /
     whatsapp creds check), via the harness's headless pass (`bin/harness_run.py` → `pass_argv` seam).
say  "✅ Bazaar is set up. Send /list with photos to sell, /search to buy, or open the /bazaar menu
      any time to change your interface, marketplaces, buying, or autonomy settings."
```

## Notes
- **Harness-agnostic:** the only steps that differ between Claude Code and Codex are §5/§6/§8/§10,
  all delegated to `bin/harnesses/`. Detection (§0) picks the harness once; everything else is
  identical.
- **OS-agnostic, always-on both ways:** the supervisor (§9) is launchd on macOS, Task Scheduler on
  Windows, behind `bin/platforms/`. Agent logic is identical in `always_on` and `interactive` modes.
- The autonomy step (§8) is what lets Bazaar list, search, and check chats unattended — it wires the
  business-approval preset AND the harness allow-list/approval-mode together (see
  `skills/bazaar-config.md` → "Two layers of autonomy").
- Re-running is safe and idempotent; for piecemeal changes after setup, use `/bazaar`.
- Honor `--dry-run`: `install.py supervisor` stays in dry-run; no Chrome launch; report intended steps.
```
