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
    for p in "${PLISTS[@]}"; do
      cp "$HERE/$p.plist" "$LA/$p.plist"
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
