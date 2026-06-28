#!/usr/bin/env python3
"""atomic_io.py — crash-safe JSON writes + a cross-process per-file lock.

Two primitives shared by the money/state ledgers (negotiate, checkout, buyer_negotiate,
delist_item, scan_state, eval_state) so that:

  • a kill/watchdog mid-write can never leave torn JSON   → write_json() does tmp + os.replace
  • two processes can never lose a read-modify-write race  → locked() serializes them on a lock file

Same flock + tmp-rename discipline already used by bin/lease.py and bin/pacing_gate.py, factored
out so every ledger writer shares one correct implementation (DRY). Each agent action shells out a
fresh process, so the contention here is cross-process — fcntl.flock (advisory, OS-arbitrated) is
the right mutex.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl  # POSIX only
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover — Windows has no fcntl; the lock degrades to a no-op
    _HAVE_FCNTL = False


def write_json(path, obj, *, indent: int = 2, ensure_ascii: bool = True, mode: int | None = None) -> None:
    """Atomically write `obj` as JSON to `path` via a temp file + os.replace (atomic rename).

    Readers always see either the old file or the fully-written new one, never a torn write.
    `mode` (e.g. 0o600) is applied to the temp file before the rename so the final file is never
    momentarily world-readable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii) + "\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)  # atomic on the same filesystem


def harden(path, mode: int = 0o600) -> None:
    """Tighten `path` to owner-only perms if it's currently group/other-accessible (idempotent).

    Used to self-heal secret/confidential files (the Telegram token store, the hidden sell floors
    and buy budgets) that may have been created world-readable (umask 022 → 0644) — e.g. floor and
    budget files are written by the agent, not by Python, so we harden them whenever we read them.
    """
    path = Path(path)
    try:
        if path.stat().st_mode & 0o077:  # any group/other permission bit set
            os.chmod(path, mode)
    except OSError:
        return  # best-effort self-heal — must never raise into a confidential-record read


@contextmanager
def locked(path):
    """Hold an exclusive cross-process lock for the lifetime of the block.

    The lock is an advisory flock on <path>.lock; the data file itself is never opened for locking,
    so write_json's atomic replace stays valid. Degrades to a no-op where fcntl is unavailable.
    """
    path = Path(path)
    if not _HAVE_FCNTL:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
