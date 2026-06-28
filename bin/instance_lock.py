#!/usr/bin/env python3
"""instance_lock.py — a robust, PID-aware singleton lock for the always-on agent_daemon.

Only one agent_daemon may run at a time: the concurrent supervisor's heartbeat-TTL lease liveness
assumes a single heartbeater, and a second consumer also fights the Telegram offset. The lock is an
advisory `fcntl.flock(LOCK_EX|LOCK_NB)` on a lock file whose body records the holder's PID.

This module exists to kill the respawn-storm seen in logs/daemon.log: launchd `KeepAlive` would
restart the agent, the fresh process would collide with the lock holder, log an ERROR, and exit
rc=3 — over and over, every ~2 minutes. The fix has two halves that live here:

  • the holder writes its PID, so a duplicate can report a TRUTHFUL "(pid=…, alive=…)" line and exit
    quietly (rc 0) instead of an ERROR storm (the caller maps not-acquired → INFO + exit 0);
  • a lock pointing at a DEAD PID (the launchd-orphan / hard-crash case) is RECLAIMED, not refused —
    so a real restart on fresh code never wedges behind a corpse.

Pure helpers (`is_pid_alive`, `read_holder_pid`) are unit-tested; `acquire` returns a dict including
the held fd, which the CALLER keeps referenced for the process lifetime (the OS frees the flock on
crash/exit). Reuses the same flock + atomic-discipline style as bin/atomic_io.py / bin/lease.py.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


def is_pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists. `os.kill(pid, 0)` is the standard liveness probe:
    ProcessLookupError → the PID is dead; PermissionError → it's alive but owned by another user
    (still alive); success → alive. A non-positive/garbage PID is treated as dead."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_holder_pid(lock_path) -> int | None:
    """Parse the holder PID recorded in the lock file. None on a missing file or garbage content —
    a corrupt lock body must never raise into the caller's startup path."""
    try:
        raw = Path(lock_path).read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid(fd: int, pid: int) -> None:
    """Truncate the (held) lock fd and rewrite it with `pid`. The fd is already flocked, so this is
    the atomic-discipline equivalent for a single-line lock body: no torn read can outlive it."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(pid).encode())
    os.fsync(fd)


def _reconcile_heartbeat_pid(heartbeat_path, pid: int) -> None:
    """Best-effort: stamp our PID onto an existing heartbeat so healthcheck's lock/heartbeat
    cross-check doesn't see a split-brain (a stale PID in the heartbeat) right after a reclaim.

    Bug D2: we ALSO stamp a FRESH `ts` (epoch seconds, the same source healthcheck.heartbeat_status
    and agent_daemon._touch_heartbeat use). The whole point of running this is that WE just took the
    lock and are alive AS OF NOW — preserving the dead holder's old `ts` would leave a fresh-PID /
    stale-ts heartbeat, which the watchdog's should_restart reads as a wedged loop and kickstarts a
    perfectly-healthy just-started daemon (a spurious restart right after a legitimate reclaim).
    Only rewrites an EXISTING heartbeat (a missing one is left for the daemon loop's first tick).
    Never raises — heartbeat IO is non-critical here."""
    path = Path(heartbeat_path)
    try:
        existing = json.loads(path.read_text())
        has_existing = isinstance(existing, dict) and existing.get("ts") is not None
    except (OSError, ValueError):
        has_existing = False
    if not has_existing:
        return  # no existing heartbeat to reconcile; the daemon loop writes a fresh one immediately
    try:
        path.write_text(json.dumps({"ts": time.time(), "pid": pid}))
    except OSError:
        return


def clear_holder(lock_path) -> bool:
    """Bug D3: empty the lock body on a CLEAN shutdown, but ONLY if OUR pid is the recorded holder.

    The watchdog decides liveness from the lock-body PID (read_holder_pid → is_pid_alive). After a
    clean exit (which KeepAlive={SuccessfulExit:false} won't restart) the body still names our now-
    dead PID; if the OS recycles that PID to an unrelated live process, is_pid_alive returns True and
    the watchdog believes the daemon is alive forever (a silent stay-down). Clearing the body to
    empty makes read_holder_pid return None → the watchdog sees NO holder → not-alive → restart.

    Guarded so we never steal a lock another process holds: a foreign (or missing/garbage) holder is
    left untouched and we return False. Returns True only when we cleared our own holder record.
    Best-effort + fail-open: any IO error is swallowed (returns False) — clearing is non-critical.

    NOTE: clears the body but keeps the file (the flock is released by the OS on process exit). The
    int-only body stays back-compatible with read_holder_pid (empty → None).

    Precondition for the read→truncate sequence to be race-free: the CALLER still holds the exclusive
    flock on this lock (true at the daemon's clean shutdown — the OS frees the flock only on fd close /
    process exit, which hasn't happened yet), so no other process can acquire it and rewrite the body
    between our ownership read and the truncate. Don't reuse this where the flock is NOT held."""
    holder = read_holder_pid(lock_path)
    if holder is None or holder != os.getpid():
        return False  # not our lock (or no holder) — never clear someone else's live lock
    try:
        # Truncate to an empty body in place; read_holder_pid("") → None → watchdog sees no holder.
        with open(lock_path, "r+b") as f:
            f.truncate(0)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return False
    return True


def acquire(lock_path, heartbeat_path) -> dict:
    """Try to take the singleton lock. Returns a dict:

        {acquired, holder_pid, holder_alive, reclaimed, fd}

    On success (the flock was free, OR it was free but the lock body named a dead holder we
    reclaimed): acquired=True, holder_pid=our PID, holder_alive=True, fd=the held descriptor (the
    caller MUST keep it referenced for the process lifetime; the OS frees the flock on exit).

    On contention (the flock is held by a LIVE duplicate): acquired=False with the truthful
    holder_pid / holder_alive read from the lock body, fd=None — the caller logs INFO + exits 0.

    The DEAD-holder case is the respawn-storm fix: a launchd-orphan / hard-crash leaves the lock file
    naming a dead PID but releases the flock, so the flock succeeds and we simply rewrite our PID and
    mark reclaimed=True. A genuinely-live duplicate still holds the flock and is correctly refused."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_pid = read_holder_pid(path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # The flock is held by a live process → a genuine duplicate. Report the truthful holder.
        os.close(fd)
        holder = read_holder_pid(path)
        return {"acquired": False, "holder_pid": holder,
                "holder_alive": is_pid_alive(holder) if holder is not None else False,
                "reclaimed": False, "fd": None}
    # The flock was free. If the lock body named a (now-dead) prior holder, this is a reclaim.
    reclaimed = prior_pid is not None and prior_pid != os.getpid() and not is_pid_alive(prior_pid)
    _write_pid(fd, os.getpid())
    _reconcile_heartbeat_pid(heartbeat_path, os.getpid())
    return {"acquired": True, "holder_pid": os.getpid(), "holder_alive": True,
            "reclaimed": reclaimed, "fd": fd}
