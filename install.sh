#!/usr/bin/env bash
# SELLY bootstrap (the hosted/curl entry). It does the ONE thing that must happen before a checkout
# exists — clone the repo into a safe runtime dir — then hands off to ./setup for everything else
# (prereqs, harness, sign-in, launchers, onboarding). If you already have a checkout, you don't need
# this: just `cd` in and run `./setup`.
#
#   # From GitHub (the primary install):
#   git clone https://github.com/jerryneoneo/selly-agent.git ~/selly-agent && cd ~/selly-agent && ./setup
#
#   # Or, if you self-host this script, a curl one-liner:
#   curl -fsSL https://<your-host>/install.sh | bash
#
# Already have a checkout? Skip this script — just `cd` into it and run `./setup`.
# To exercise the clone path from a local git checkout: `SELLY_REPO="$PWD" bash install.sh`.
#
# Env overrides:
#   SELLY_REPO   git URL          (default: the GitHub repo below)
#   SELLY_DIR    install location (default: ~/selly-agent — must be outside ~/Documents on macOS)
# Any extra args are passed straight through to ./setup (e.g. --host, --yes).
set -euo pipefail

REPO="${SELLY_REPO:-https://github.com/jerryneoneo/selly-agent}"
DIR="${SELLY_DIR:-$HOME/selly-agent}"

say()  { printf '\033[1;36mSELLY:\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mSELLY:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mSELLY:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

say "Bootstrapping your personal P2P seller agent."

# Minimal gates to even clone; ./setup re-checks the rest (node/npx/Chrome) with fix hints.
case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux)  OS=linux ;;
  *)      die "Unsupported OS. On Windows run the PowerShell installer (install.ps1)." ;;
esac
have git     || die "git is required. Install it and re-run."
have python3 || die "python3 is required. Install it and re-run."

# Refuse a TCC-blocked runtime dir on macOS (launchd can't read ~/Documents, etc.).
if [ "$OS" = macos ]; then
  case "$DIR" in
    "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
      die "SELLY_DIR=$DIR is under a macOS privacy-protected folder. Pick a path like ~/selly-agent." ;;
  esac
fi

# Clone (or fast-forward) into the runtime dir.
if [ -d "$DIR/.git" ]; then
  say "Updating existing install at $DIR"
  git -C "$DIR" pull --ff-only || warn "git pull failed — continuing with the existing checkout."
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "$DIR exists and is not empty. Move it or set SELLY_DIR to a fresh path."
else
  say "Cloning $REPO -> $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR"
[ -x ./setup ] || die "$DIR/setup is missing or not executable — is this a SELLY checkout?"
say "Handing off to ./setup…"
exec ./setup "$@"
