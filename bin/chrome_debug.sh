#!/usr/bin/env bash
# chrome_debug.sh — run the warm, logged-in Chrome that Playwright attaches to over CDP.
# Uses the SAME persistent profile as the live listings (your enabled marketplaces stay logged in).
# Runs in the FOREGROUND so launchd (KeepAlive) can supervise it; if it dies, launchd restarts.
#
# Manual: bin/chrome_debug.sh        (Ctrl-C to stop)
# Check : curl -s http://127.0.0.1:9222/json/version
set -euo pipefail

SELLER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="$SELLER_DIR/.browser-profile"
PORT="${CHROME_DEBUG_PORT:-9222}"
CHROME="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

# Already up? (e.g. launchd started it) — don't launch a second one on the same profile.
if curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "Chrome debug endpoint already up on :$PORT"
  exit 0
fi

mkdir -p "$PROFILE"
exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  --restore-last-session \
  --hide-crash-restore-bubble
