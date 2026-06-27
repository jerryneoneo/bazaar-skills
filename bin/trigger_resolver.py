#!/usr/bin/env python3
"""trigger_resolver.py — per-platform trigger path: notification vs polling (no LLM).

Each enabled platform is driven by whichever wake mechanism is actually available on this machine:

  • "notification" — read the OS Notification Center (notify_db.py): instant, ~0 tokens. Chosen only
    when a READABLE notification from the platform's origin has actually arrived within the viability
    window, so the choice is EMPIRICAL, not a guess from a permission bit. (e.g. Facebook web push.)
  • "poll" — the existing cheap browser probe (buyer_peek + buyer_recheck) driven over CDP. The SAFE
    DEFAULT: a platform with no readable notifications (e.g. Carousell, which has no web-push
    subscription and is browser-only with no backend access) always lands here.

Why empirical: a platform may hold a push subscription yet never deliver a readable desktop
notification under our warm-tab setup (the site delivers in-app while its tab is open). So we trust
only what actually shows up in the notification DB. If notifications start flowing for a platform,
the resolver upgrades it automatically next check; if they stop, it falls back to polling.

Pure + testable: resolve()/notification_viable() take the notification list + current time as
arguments (the I/O of reading the DB lives in notify_db.py)."""

from __future__ import annotations

import datetime

# Platform id -> origin substring to match against a notification's source domain. Substring match
# tolerates the `www.` prefix and regional TLDs (carousell.sg, ebay.com.sg, ...).
PLATFORM_ORIGINS: dict[str, str] = {
    "fb": "facebook.com",
    "carousell": "carousell.sg",
    "ebay": "ebay.",
}

# A platform counts as notification-capable only if a readable notification from its origin landed
# within this many hours. Wide by default: notifications are bursty, and a quiet day must not drop a
# platform off the path it was just using. The poll path is always the fallback, so this is safe.
DEFAULT_VIABILITY_HOURS = 168  # 7 days


def _within(ts_iso: str, now_iso: str, hours: float) -> bool:
    """True if `ts_iso` is within `hours` before `now_iso`. Bad/blank timestamps fail closed."""
    try:
        ts = datetime.datetime.fromisoformat(ts_iso)
        now = datetime.datetime.fromisoformat(now_iso)
    except (ValueError, TypeError):
        return False
    return datetime.timedelta(0) <= (now - ts) <= datetime.timedelta(hours=hours)


def notification_viable(platform: str, notifs: list[dict], now_iso: str,
                        window_hours: float = DEFAULT_VIABILITY_HOURS) -> bool:
    """True if a readable notification from `platform`'s origin arrived within the window.

    `notifs` is notify_db.read_recent() output: dicts with at least {origin, ts}. An unknown
    platform (no origin mapping) is never viable."""
    origin = PLATFORM_ORIGINS.get(platform)
    if not origin:
        return False
    for n in notifs:
        if origin in (n.get("origin") or "") and _within(n.get("ts", ""), now_iso, window_hours):
            return True
    return False


def resolve(platform: str, notifs: list[dict], now_iso: str,
            window_hours: float = DEFAULT_VIABILITY_HOURS) -> str:
    """Return the trigger path for `platform`: "notification" if its notifications are actually
    flowing on this machine, else "poll" (the safe default)."""
    return "notification" if notification_viable(platform, notifs, now_iso, window_hours) else "poll"
