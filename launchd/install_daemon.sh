#!/usr/bin/env bash
# install_daemon.sh — install / uninstall the always-on seller agent (macOS launchd).
#   install_daemon.sh install     load Chrome + agent LaunchAgents (start now + at login)
#   install_daemon.sh uninstall   stop + remove them
#   install_daemon.sh status      show whether they're loaded + tail logs
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
PLISTS=(com.bazaarskills.chrome com.bazaarskills.agent)

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
    for p in "${PLISTS[@]}"; do
      sed -e "s#__RUNTIME__#$RUNTIME#g" -e "s#__PATH__#$RESOLVED_PATH#g" "$HERE/$p.plist" > "$LA/$p.plist"
      launchctl unload "$LA/$p.plist" 2>/dev/null || true
      launchctl load -w "$LA/$p.plist"
      echo "loaded $p"
    done
    echo "Done. Chrome + agent are running and will start at login."
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
