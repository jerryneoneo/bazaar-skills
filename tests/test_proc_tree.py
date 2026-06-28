#!/usr/bin/env python3
"""Tests for proc_tree.py — the shared kill-the-whole-tree teardown used by BOTH the default
single-flight loop (agent_daemon.run_pass) and the concurrent supervisor loop.

    python3 tests/test_proc_tree.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import proc_tree  # noqa: E402

_fail = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        _fail.append(name)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_confirm_dead_kills_grandchild():
    print("confirm_dead kills the whole process GROUP (no orphaned grandchild):")
    # wrapper (sh) spawns a grandchild `sleep` and waits — mirrors run_pass.sh → harness_run → claude.
    # start_new_session=True puts both in one group so killpg reaches the grandchild.
    proc = subprocess.Popen(["sh", "-c", "sleep 60 & echo $! ; wait"],
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    gc_pid = int(proc.stdout.readline().strip())
    proc_tree.confirm_dead(proc, grace=3)
    check("wrapper is dead", proc.poll() is not None)
    check("grandchild killed too (orphan bug fixed)", not _alive(gc_pid))


def test_confirm_dead_sigterms_a_cooperative_child_within_grace():
    print("a cooperative child exits on SIGTERM well before the SIGKILL escalation:")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    t0 = time.monotonic()
    proc_tree.confirm_dead(proc, grace=5)
    elapsed = time.monotonic() - t0
    check("child reaped", proc.poll() is not None)
    check("reaped via SIGTERM, not the 5s SIGKILL fallback", elapsed < 4)


def test_confirm_dead_waits_for_grandchild_that_outlives_the_leader():
    print("confirm_dead reaps a grandchild that IGNORES SIGTERM and outlives the wrapper (orphan fix):")
    # Mirrors production: the wrapper (leader) dies immediately on SIGTERM, but the grandchild traps
    # SIGTERM and keeps running — exactly the case where waiting on the leader alone returns too early.
    gc_src = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    cmd = f"{sys.executable} -c '{gc_src}' & echo $! ; wait"
    proc = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, text=True, start_new_session=True)
    gc_pid = int(proc.stdout.readline().strip())
    check("grandchild ignoring SIGTERM is alive before teardown", _alive(gc_pid))
    proc_tree.confirm_dead(proc, grace=1)  # SIGTERM won't kill it; confirm_dead must escalate to SIGKILL
    check("wrapper (leader) is dead", proc.poll() is not None)
    check("SIGTERM-ignoring grandchild reaped via SIGKILL before confirm_dead returned", not _alive(gc_pid))


def test_kill_tree_on_already_dead_is_safe():
    print("kill_tree on an already-exited process does not raise:")
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    raised = False
    try:
        proc_tree.kill_tree(proc, __import__("signal").SIGTERM)
    except Exception:  # noqa: BLE001 — the whole point is that it must not raise
        raised = True
    check("kill_tree swallows ProcessLookupError on a dead group", not raised)


if __name__ == "__main__":
    print("proc_tree tests\n")
    test_confirm_dead_kills_grandchild()
    test_confirm_dead_sigterms_a_cooperative_child_within_grace()
    test_confirm_dead_waits_for_grandchild_that_outlives_the_leader()
    test_kill_tree_on_already_dead_is_safe()
    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): {', '.join(_fail)}")
        sys.exit(1)
    print("ALL PASS")
