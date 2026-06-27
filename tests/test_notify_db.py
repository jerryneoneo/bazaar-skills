#!/usr/bin/env python3
"""Tests for notify_db.py — the read-only macOS Notification Center reader.

    python3 tests/test_notify_db.py

The DB read itself is machine + permission dependent (needs Full Disk Access), so we unit-test the
pure pieces (bplist decode, cocoa-timestamp conversion) and the fail-open CONTRACT of the I/O
functions (always a list / bool, never raise), which is what keeps the daemon safe without FDA.
"""

import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import notify_db  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def test_decode_record_extracts_fields():
    print("_decode_record pulls title/subtitle(origin)/body from a Chrome-style bplist:")
    blob = plistlib.dumps({"req": {"titl": "Baba Mahamed", "subt": "www.facebook.com",
                                   "body": "Hi, is this available?"}})
    title, origin, body = notify_db._decode_record(blob)
    check("title", title == "Baba Mahamed")
    check("origin (subtitle)", origin == "www.facebook.com")
    check("body", body == "Hi, is this available?")


def test_decode_record_failopen():
    print("_decode_record tolerates junk → empty strings, never raises:")
    t, o, b = notify_db._decode_record(b"not a plist")
    check("blank tuple on junk", (t, o, b) == ("", "", ""))


def test_cocoa_to_iso():
    print("_cocoa_to_iso converts a Cocoa timestamp to a parseable local ISO string:")
    iso = notify_db._cocoa_to_iso(0)  # 2001-01-01 in Cocoa epoch
    import datetime
    ok = False
    try:
        dt = datetime.datetime.fromisoformat(iso)
        ok = dt.year == 2001
    except (ValueError, TypeError):
        ok = False
    check("year 2001 round-trips", ok)
    check("junk → empty string", notify_db._cocoa_to_iso("nope") == "")


def test_read_recent_failopen_contract():
    print("read_recent / available never raise; read_recent always returns a list:")
    recs = notify_db.read_recent(limit=5)
    check("read_recent returns a list (or [] without FDA)", isinstance(recs, list))
    check("available returns a bool", isinstance(notify_db.available(), bool))
    # If we DID read records (FDA granted on this machine), each is well-formed for the resolver.
    if recs:
        r = recs[0]
        check("record has resolver fields", all(k in r for k in ("origin", "ts", "title", "body")))
    else:
        check("no FDA / no records → empty list (graceful)", recs == [])


if __name__ == "__main__":
    print("notify_db tests\n")
    test_decode_record_extracts_fields()
    test_decode_record_failopen()
    test_cocoa_to_iso()
    test_read_recent_failopen_contract()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
