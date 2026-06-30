#!/usr/bin/env bash
# Migration v0.3.0 — Bazaar -> SELLY rebrand cleanup.
#
# Idempotent. Runs from setup step 6 with $SELLY_HOME pointing at the runtime dir.
# The config-dir move (~/.bazaar -> ~/.selly) is handled earlier, inline in `setup`,
# so the user's host/autonomy survive. This script handles the rest:
#   1. tear down the old launchd jobs (labels com.bazaarskills.* -> com.selly.*)
#   2. remove stale global launchers carrying the OLD launcher marker (hard cutover:
#      the bazaar* commands are gone; only selly* should remain after gen-launchers)
#   3. rewrite the agent's own-thread `source` tag "bazaar" -> "selly" in runtime state
set -u

HOME_DIR="${HOME:-$(echo ~)}"

# 1) Old launchd jobs ------------------------------------------------------------------------
if command -v launchctl >/dev/null 2>&1; then
  uid="$(id -u)"
  for j in chrome agent watchdog; do
    old="com.bazaarskills.$j"
    plist="$HOME_DIR/Library/LaunchAgents/$old.plist"
    launchctl bootout "gui/$uid/$old" 2>/dev/null \
      || launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
  done
fi

# 2) Stale global launchers (old marker) -----------------------------------------------------
skills_dir="$HOME_DIR/.claude/skills"
if [ -d "$skills_dir" ]; then
  find "$skills_dir" -name SKILL.md 2>/dev/null | while IFS= read -r f; do
    if grep -q 'bazaar-skills launcher (generated)' "$f" 2>/dev/null; then
      rm -rf "$(dirname "$f")"
    fi
  done
fi

# 3) Runtime own-thread source tag -----------------------------------------------------------
data_dir="${SELLY_DATA_DIR:-${SELLY_HOME:-.}/data}"
for f in "$data_dir"/inbox_buy_state.json "$data_dir"/inbox_sell_state.json \
         "$data_dir"/wants/*.json "$data_dir"/items/*.json; do
  [ -f "$f" ] || continue   # unmatched globs stay literal -> skipped here
  sed -i.bak -e 's/"source": *"bazaar"/"source": "selly"/g' -e 's/"source":"bazaar"/"source":"selly"/g' "$f" \
    && rm -f "$f.bak"
done

exit 0
