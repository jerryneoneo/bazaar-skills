#!/usr/bin/env bash
# install_daemon.sh — install / uninstall the always-on seller agent (macOS launchd).
#   install_daemon.sh install     load Chrome + agent LaunchAgents (start now + at login)
#   install_daemon.sh uninstall   stop + remove them
#   install_daemon.sh status      show whether they're loaded + tail logs
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
# Fix D: the independent watchdog joins the managed set so install/status/uninstall handle it too.
PLISTS=(com.bazaarskills.chrome com.bazaarskills.agent com.bazaarskills.watchdog)

case "${1:-status}" in
  install)
    mkdir -p "$LA" "$HERE/../logs"
    # The committed plists are TEMPLATES; fill __RUNTIME__ (this checkout) and __PATH__ (the dirs
    # where node/npx/claude/curl live on THIS machine) before loading — so the daemon works for any
    # clone, not just the author's. launchd jobs otherwise inherit a minimal PATH.
    RUNTIME="$(cd "$HERE/.." && pwd)"
    node_bin=""; command -v node   >/dev/null 2>&1 && node_bin="$(dirname "$(command -v node)")"
    claude_bin=""; command -v claude >/dev/null 2>&1 && claude_bin="$(dirname "$(command -v claude)")"
    RESOLVED_PATH="${node_bin:+$node_bin:}${claude_bin:+$claude_bin:}$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    # Pick a stable, FDA-grantable interpreter for the daemon. /usr/bin/python3 is the CommandLineTools
    # SHIM: under launchd it re-execs a versioned framework binary that TCC won't attribute Full Disk
    # Access to, so granting FDA to it never sticks and Instant (notification) wake mode stays off.
    # A Homebrew python is launched directly, so its FDA grant holds. Prefer one; fall back to the shim.
    DAEMON_PY=""
    for cand in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 \
                /usr/local/bin/python3.13 /usr/local/bin/python3 \
                /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
      [ -x "$cand" ] && { DAEMON_PY="$cand"; break; }
    done
    DAEMON_PY="${DAEMON_PY:-/usr/bin/python3}"
    echo "daemon interpreter: $DAEMON_PY"
    [ "$DAEMON_PY" = "/usr/bin/python3" ] && echo "  note: no Homebrew python found — Instant mode needs one (brew install python); Standard polling works regardless."
    UID_NUM="$(id -u)"
    for p in "${PLISTS[@]}"; do
      sed -e "s#__RUNTIME__#$RUNTIME#g" -e "s#__PATH__#$RESOLVED_PATH#g" -e "s#__PYTHON__#$DAEMON_PY#g" "$HERE/$p.plist" > "$LA/$p.plist"
      # Fix D: if the job is ALREADY loaded, restart it ATOMICALLY with `kickstart -k` (one stop+start)
      # instead of unload+load — the double-spawn that let a fresh instance collide with the still-live
      # lock holder (the respawn-storm this fix removes). Only a not-yet-loaded job needs load -w.
      if launchctl list 2>/dev/null | grep -q "$p"; then
        launchctl kickstart -k "gui/$UID_NUM/$p"
        echo "kickstarted $p (already loaded → atomic restart)"
      else
        launchctl load -w "$LA/$p.plist"
        echo "loaded $p"
      fi
    done
    echo "Done. Chrome + agent (+ watchdog) are running and will start at login."
    echo "Tail logs: tail -f \"$HERE/../logs/daemon.log\"" ;;
  uninstall)
    for p in "${PLISTS[@]}"; do
      launchctl unload "$LA/$p.plist" 2>/dev/null || true
      rm -f "$LA/$p.plist"
      echo "removed $p"
    done ;;
  status)
    for p in "${PLISTS[@]}"; do
      printf "%-30s " "$p:"; launchctl list | grep "$p" || echo "(not loaded)"
    done ;;
  *) echo "usage: install_daemon.sh {install|uninstall|status}" >&2; exit 2 ;;
esac
