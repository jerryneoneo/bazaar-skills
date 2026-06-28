#!/usr/bin/env python3
"""proc_tree.py — kill a launched subprocess and its whole descendant tree, deterministically.

A pass is launched with start_new_session=True so it leads its own process group (pgid == pid).
run_pass.sh `exec`s harness_run.py, which subprocess.run()s the `claude` grandchild that actually
drives the browser tab. Signalling only proc.pid would orphan that grandchild, leaving it acting on
the live marketplace account AFTER the daemon released the lock / unlinked the run lock — the
CRITICAL orphan bug. So we always signal the GROUP and, on a forced stop, WAIT until the whole tree
is gone before the caller releases its lock.

Shared by bin/supervisor.py (concurrent workers) and bin/agent_daemon.py (default single-flight) so
both teardown paths use one correct implementation (DRY).
"""
import logging
import os
import signal
import subprocess
import time

GRACE_SEC = 10     # time a SIGTERM'd tree gets to exit cleanly before we SIGKILL it
KILL_WAIT_SEC = 5  # after SIGKILL, how long we wait to confirm the whole group is reaped
_POLL_SEC = 0.05   # group-liveness poll interval


def _killpg(pgid, sig):
    """Signal a process group by pgid, swallowing 'already gone' / unsignalable errors."""
    if pgid is None:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_tree(proc, sig):
    """Send `sig` to the process GROUP of `proc` (requires start_new_session=True at launch)."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return  # already gone
    _killpg(pgid, sig)


def _group_alive(pgid):
    """True while ANY process remains in the group (signal 0 = existence probe)."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but unsignalable — treat as alive (conservative)


def _wait_group_gone(pgid, timeout):
    """Poll until the group is empty or `timeout` elapses. Returns True iff the group is gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(_POLL_SEC)
    return not _group_alive(pgid)


def confirm_dead(proc, grace=GRACE_SEC):
    """SIGTERM the group, then WAIT until the WHOLE tree is gone — not just the group leader.

    proc.wait() reaps only the leader (run_pass.sh → harness_run.py, which dies instantly on SIGTERM
    with no handler); the `claude` grandchild that drives the live account can take longer to exit.
    We capture the pgid up front (the leader's PID vanishes once reaped), signal + reap the leader,
    then poll the GROUP until it is empty, escalating to SIGKILL after `grace`. Only then is it safe
    for the caller to release its lock — closing the orphaned-grandchild window.
    """
    try:
        pgid = os.getpgid(proc.pid)  # capture BEFORE the leader is reaped (its PID then vanishes)
    except (OSError, TypeError):
        pgid = None
    _killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)     # reap the leader so its zombie can't keep the group 'alive'
    except subprocess.TimeoutExpired:
        pass
    if pgid is None or _wait_group_gone(pgid, grace):
        return                       # whole tree gone (or group unknown — nothing more we can do)
    _killpg(pgid, signal.SIGKILL)    # a descendant ignored SIGTERM → force the entire group
    try:
        proc.wait(timeout=KILL_WAIT_SEC)  # reap the leader NOW so its zombie doesn't read as 'alive'
    except subprocess.TimeoutExpired:
        pass
    if not _wait_group_gone(pgid, KILL_WAIT_SEC):
        logging.error("process group %s survived SIGKILL", pgid)
