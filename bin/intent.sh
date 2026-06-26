#!/usr/bin/env bash
# intent.sh — thin wrapper kept for the daemon's interface. Prints ONE short "what I'll do next"
# line via the MCP-less fast pass built in bin/harness_run.py (intent mode).
#   intent.sh "what are my active listings?"  ->  "Let me check your listings…"
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/harness_run.py" intent "${1:-[message]}"
