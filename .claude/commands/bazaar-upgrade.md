---
description: Bazaar — update to the latest version (git pull → re-run setup → restart daemon)
---

# /bazaar-upgrade — update Bazaar

Pulls the latest Bazaar, re-runs the idempotent installer (refreshes launchers + config, runs any
pending migrations), restarts the always-on daemon, and shows what changed. The gstack `/gstack-upgrade`
analogue. Safe to run any time — it never touches your `data/` (listings, floors, budgets, secrets).

## Flow

```
# 1. Locate the runtime dir (the launcher already cd'd you here, but be robust).
home = `cat ~/.bazaar/home` (fallback: the current Bazaar checkout)
cd "$home"

# 2. Record the version we're upgrading FROM, then pull.
before = `cat VERSION`
run `git pull --ff-only`
  - if it fails (local changes / not a git checkout): say so, stop, and suggest `git status`.

# 3. Re-run the idempotent installer (refreshes launchers, gen-mcp, migrations, version stamp).
run `./setup --yes`     # re-run path: no onboarding, just refresh (data/seller_config.json exists)

# 4. Restart the always-on daemon so the new code takes effect (macOS).
if launchd/install_daemon.sh exists AND a daemon is loaded:
    run `launchd/install_daemon.sh install`   # reload (install is idempotent: stop+load)

# 5. Show what changed.
after = `cat VERSION`
if before != after:
    say "Updated Bazaar $before → $after."
    show the CHANGELOG.md entries newer than `before` (the section headers + bullets).
else:
    say "Already on the latest version ($after)."
```

## Notes
- **Idempotent.** Re-running with nothing new pulls a no-op, refreshes launchers, and reports
  "already on the latest version".
- **No data risk.** Upgrade only touches code + launchers + `~/.bazaar`; per-deployment state in
  `data/` is never modified (migrations, when present, are explicit and reversible).
- **Auto-upgrade (optional):** if `bazaar-config get auto_upgrade` is `true`, a shared-repo
  SessionStart hook can run this automatically — see `setup --team`.
