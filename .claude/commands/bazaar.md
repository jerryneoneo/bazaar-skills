---
description: Bazaar — settings & capabilities menu (view/change interface, marketplaces, autonomy)
---

# /bazaar — settings menu

The home menu: shows what Bazaar can do and your current setup, and lets you change any of it. It is
a thin router that **re-enters the onboarding anchor sections** (one source of truth per step) — it
never duplicates their logic. Works on any adapter and any harness.

Read first: `skills/channel/intro.md` (capabilities body), `skills/channel/onboarding.md` (anchors),
`skills/channel/adapters.md`, `skills/marketplaces.md`, `skills/bazaar-config.md`.

## Flow

```
say  the CAPABILITIES body from skills/channel/intro.md (what Bazaar does)

# Current settings — read from config + seller_config + buyer_config + the registries (read-shims).
load seller_config.json, buyer_config.json, config.json, data/marketplaces.json
say  "Here's your current setup:
      • Interface:    <channel.adapter>  (+ detect() status of the others)
      • Connectors:   per adapter — ✓ bound / available / not set up
      • Selling:      <seller enabled ids>  (region <region>; listings filtered per item by category)
      • Buying:       <on if buyer_config exists: search <buyer enabled ids>; deliver to
                       <delivery_area.area>; pay <payment_methods> | off>
      • Autonomy:     <approvals.preset>  (+ any per-step overrides, incl. buy_offer / buy_accept)
      • Style:        <style.voice.tone>/<voice.humor>, lowballers: <voice.lowball_response>,
                      sell firmness: <negotiation.sell_firmness>, learning: <learning>
                      (+ ' · N suggestion(s) pending' if `python3 bin/style.py proposals` is non-empty)
      • Running as:   <always-on daemon | interactive session>
                      <if `python3 bin/control.py is-paused` exits 0: append '  ⏸ PAUSED (since
                       <control.since>, via <control.source>); N correction(s) queued'>
      • Wake speed:   <run `python3 bin/notify_db.py`; if it reports available AND a marketplace is on
                       the notification path: '⚡ Instant (Facebook/Instagram); Carousell on Standard'
                       else: '🛡️ Standard (polling)'>"

ask  "What would you like to change?"
     options=[interface=Interface, connectors=Connectors, marketplaces=Selling marketplaces,
              buying=Buying (search + budget), autonomy=Autonomy, style=Style/persona,
              speed=Wake speed (Instant vs Standard), pause=Pause/Resume the agent,
              health=Health check, reinstall=Re-run full setup, done=Nothing, close]
  interface    -> goto skills/channel/onboarding.md#CHOOSE_INTERFACE
                  # if a daemon is loaded, do uninstall -> rewrite channel -> reinstall (adapters.md)
  connectors   -> goto skills/channel/onboarding.md#CHOOSE_INTERFACE   # re-run detect()/connect() to
                  # (re)authorize a channel without necessarily switching the bound adapter
  marketplaces -> goto skills/channel/onboarding.md#CHOOSE_MARKETPLACES
  buying       -> goto skills/channel/onboarding.md#BUYER_PROFILE       # delivery area, payment, search markets
  autonomy     -> goto skills/channel/onboarding.md#APPROVALS
  style        -> goto skills/channel/onboarding.md#STYLE   # voice/persona + sell firmness + learning;
                  # also where pending learning suggestions are reviewed/applied (bin/style.py proposals)
  pause        -> if NOT paused: run .claude/commands/pause.md (stop the agent so you can correct it).
                  if paused: run .claude/commands/resume.md (apply queued corrections, then continue).
  speed        -> read skills/bazaar-config.md "Wake speed". Say the two modes (PROS ONLY, no cons):
                  "⚡ Instant (Full Disk Access): replies the moment a buyer messages on Facebook or
                  Instagram, often answering straight from the notification.  🛡️ Standard (no extra
                  permissions): hands-off, checks your inboxes on a quick cycle, works out of the box."
                  Then run `python3 bin/notify_db.py` (available?):
                  - NOT available -> offer Instant: guide Full Disk Access (System Settings → Privacy &
                    Security → Full Disk Access → add the daemon's python3). It then auto-activates for
                    push-capable markets; Carousell stays on Standard. Keeping marketplace tabs
                    backgrounded so push keeps firing is automatic (bin/tab_park.py). Both modes are safe.
                  - available -> "⚡ Instant is on: Facebook/Instagram wake instantly; Carousell uses
                    Standard polling." Offer to return to Standard (revoke Full Disk Access).
                  Read-only: this guides an OS permission and writes no config (the path auto-detects).
  health       -> run `python3 bin/healthcheck.py` (read-only: CDP, marketplace logins, daemon); report results.
  reinstall    -> run .claude/commands/bazaar-install.md
  done/close   -> say "All set."
loop back to the menu after a change (so several things can be tweaked in one sitting).
```

## Notes
- **Single source of truth:** every branch re-enters an onboarding anchor; this menu adds no new
  settings logic. Adding a setting means editing the anchor, and it shows up here for free.
- **Interface switch safety:** changing the bound adapter while the always-on daemon runs must go
  uninstall → rewrite `seller_config.channel` → reinstall (the same dance `DAEMON.md` mandates for
  `/sell`); the `CHOOSE_INTERFACE` anchor handles this.
- **Autonomy** changes write `config.approvals` *and* regenerate the harness permission layer via
  `bin/install.py gen-settings --autonomy <level>` (both layers, per `skills/bazaar-config.md`).
- Read-only by default: nothing changes until the user picks a branch and completes its sub-flow.
