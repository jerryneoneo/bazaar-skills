#!/usr/bin/env bash
# run_pass.sh — thin wrapper kept for the daemon's interface. The real, harness-agnostic logic
# lives in bin/harness_run.py (builds a PassSpec → asks the active harness for argv via the seam).
#   run_pass.sh seller | run_pass.sh buyer
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/harness_run.py" "$@"
