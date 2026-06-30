#!/usr/bin/env python3
"""research_result.py — the background research worker's ONLY write path.

The `research` pass (harness_run.py mode `research`) is a detached, browser-free worker that
identifies a listing's item from its photos and finds market comps. It is allowed EXACTLY ONE Bash
command — `python3 bin/research_result.py ...` — and no general shell, no browser, no channel send.
So the only thing it can do with its findings is deposit them here, in
`data/research_results/<batch_id>.json`, for the daemon to pick up and present. This narrow seam is
what makes "run research in the background" safe: the worker can never message the seller or drive
the live marketplace, it can only leave a result file.

Usage:
    python3 bin/research_result.py --batch <batch_id> --result '<json object>'

The JSON is validated (must be an object) and written atomically. `batch_id` is stamped into the
record so a stale/mismatched result can't be misattributed. Exit codes: 0 ok · 2 bad input.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
_SAFE_BATCH = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="research_result.py", add_help=False)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--result", required=True, help="the research findings as a JSON object")
    try:
        ns = ap.parse_args(argv[1:])
    except SystemExit:
        print(json.dumps({"error": "usage: --batch <id> --result <json>"}), file=sys.stderr)
        return 2

    batch = ns.batch.strip()
    if not _SAFE_BATCH.match(batch):
        print(json.dumps({"error": "invalid batch id"}), file=sys.stderr)
        return 2
    try:
        result = json.loads(ns.result)
    except ValueError as exc:
        print(json.dumps({"error": f"result is not valid JSON: {exc}"}), file=sys.stderr)
        return 2
    if not isinstance(result, dict):
        print(json.dumps({"error": "result must be a JSON object"}), file=sys.stderr)
        return 2

    result["batch_id"] = batch  # stamp so a mismatched result can't be misattributed
    dest = DATA / "research_results" / f"{batch}.json"
    atomic_io.write_json(dest, result)
    print(json.dumps({"ok": True, "path": str(dest)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
