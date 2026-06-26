#!/usr/bin/env python3
"""availability.py — answer "when can I pick up?" from the seller's calendar.

Thin shim over two sources, chosen by config.json `availability_source`:
  - "local":        compute open meetup windows from data/availability.json (deterministic).
  - "calendar_mcp": Python can't reach an MCP server, so emit an instruction telling the
                    caller (the model) to query the connected Google Calendar MCP for free
                    slots in the range and treat busy events as blocked. meetup_areas still
                    come from the local file.

Usage:
    python3 availability.py <from_date YYYY-MM-DD> <to_date YYYY-MM-DD>
Output (stdout, JSON): see build_local_slots / build_mcp_directive below.
Exit codes: 0 ok · 2 bad input · 3 config/data missing.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = DATA_DIR / "config.json"
AVAIL_PATH = DATA_DIR / "availability.json"

MAX_RANGE_DAYS = 31  # never expand an unbounded range
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _load_json(path, label):
    if not path.exists():
        raise FileNotFoundError(f"{label} not found at {path}")
    return json.loads(path.read_text())


def build_local_slots(avail, from_d, to_d):
    """Expand weekly_windows across [from_d, to_d], minus blocked_dates."""
    windows_by_day = {}
    for w in avail.get("weekly_windows", []):
        windows_by_day.setdefault(w["day"], []).append({"from": w["from"], "to": w["to"]})
    blocked = set(avail.get("blocked_dates", []))

    slots = []
    cursor = from_d
    while cursor <= to_d:
        iso = cursor.isoformat()
        day_name = WEEKDAY_NAMES[cursor.weekday()]
        if iso not in blocked and day_name in windows_by_day:
            for window in windows_by_day[day_name]:
                slots.append({"date": iso, "day": day_name, **window})
        cursor += timedelta(days=1)
    return {
        "source": "manual",
        "timezone": avail.get("timezone"),
        "slots": slots,
    }


def build_mcp_directive(avail, from_d, to_d):
    """Tell the model to consult the Calendar MCP itself (Python can't reach it)."""
    return {
        "source": "calendar_mcp",
        "timezone": avail.get("timezone"),
        "instruction": (
            f"Query the connected Google Calendar MCP for FREE/BUSY between "
            f"{from_d.isoformat()} and {to_d.isoformat()}. Treat busy events as blocked. "
            f"Tell the buyer when the seller can ship / hand the item to the courier — "
            f"this is shipping/handover availability, not a meetup. Never invent availability."
        ),
        "range": {"from": from_d.isoformat(), "to": to_d.isoformat()},
    }


def build_skip_directive():
    """Availability not configured — keep timing answers vague, promise no slot."""
    return {
        "source": "skip",
        "instruction": "Availability isn't set up; keep timing vague (e.g. 'usually a day or "
                       "two after payment') and don't promise a specific ship date.",
    }


def _parse_date(s):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"dates must be YYYY-MM-DD: {exc}") from exc


def run(from_s, to_s):
    from_d = _parse_date(from_s)
    to_d = _parse_date(to_s)
    if to_d < from_d:
        raise ValueError("to_date is before from_date")
    if (to_d - from_d).days > MAX_RANGE_DAYS:
        raise ValueError(f"range exceeds {MAX_RANGE_DAYS} days")

    # Availability source now lives in seller_config.json (calendar_mcp | manual | skip),
    # falling back to the legacy config.json availability_source for older setups.
    source = "manual"
    seller_cfg_path = DATA_DIR / "seller_config.json"
    if seller_cfg_path.exists():
        source = json.loads(seller_cfg_path.read_text()).get("availability", {}).get("source", source)
    elif CONFIG_PATH.exists():
        source = json.loads(CONFIG_PATH.read_text()).get("availability_source", source)

    if source == "skip":
        return build_skip_directive()
    if source == "calendar_mcp":
        avail = _load_json(AVAIL_PATH, "availability.json")
        return build_mcp_directive(avail, from_d, to_d)
    avail = _load_json(AVAIL_PATH, "availability.json")
    return build_local_slots(avail, from_d, to_d)


def main(argv):
    if len(argv) != 3:
        print(json.dumps({"error": "usage: availability.py <from YYYY-MM-DD> <to YYYY-MM-DD>"}),
              file=sys.stderr)
        return 2
    try:
        result = run(argv[1], argv[2])
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
