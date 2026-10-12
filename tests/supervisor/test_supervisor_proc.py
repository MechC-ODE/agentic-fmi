"""The process primitives of ``supervisor.py`` on plain processes (``sleep``, ``sh``, Python):
``/proc`` parsing, zombies (a zombie leader whose other thread still runs, too), start-time
checks, and kill-and-reap of our own children (through their ``Popen`` handle), of processes
that aren't our children, of stopped processes and of reused pids.

Collaborators: ``agentic_fmi.supervisor`` (under test) and the ``sleeper``, ``strays`` and
``until`` fixtures in ``tests/supervisor/conftest.py``. Nothing here runs an FMU.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.supervisor import is_alive, kill_and_reap, proc_stat

Sleeper = Callable[[], "subprocess.Popen[bytes]"]
Track = Callable[[int], None]
Until = Callable[..., Any]

RENAMING_CHILD = """
import time
from pathlib import Path
print(Path("/proc/self/stat").read_text().split()[21], flush=True)
Path("/proc/self/comm").write_text("a) b (c")
print("renamed", flush=True)
time.sleep(60)
"""
"""Prints its own start time while its name is plain, then renames itself ``a) b (c``."""

ORPHAN_MAKER = """
import subprocess
quiet = subprocess.DEVNULL
child = subprocess.Popen(["sleep", "60"], stdout=quiet, stderr=quiet, start_new_session=True)
print(child.pid)
"""
"""Starts ``sleep 60`` in its own session and exits, so init adopts the sleeper."""

SYS_EXIT = {"x86_64": 60, "aarch64": 93}.get(os.uname().machine)
"""The ``exit`` syscall (one thread, unlike ``exit_group``) on this machine, if known."""

LINGERING_LEADER = f"""
import ctypes, os, threading, time
threading.Thread(target=lambda: (time.sleep(1.0), os._exit(0))).start()
ctypes.CDLL(None).syscall({SYS_EXIT}, 0)
"""
"""Its main thread exits alone: the group leader is a zombie at once, but can't be reaped until
the other thread ends the process a second later."""

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")


def _start_time(pid: int) -> int:
    stat = proc_stat(pid)
    assert stat is not None
    return stat.start_time


def _state(pid: int) -> str | None:
    stat = proc_stat(pid)
    return stat.state if stat else None


def _missing_pid() -> int:
    return int(Path("/proc/sys/kernel/pid_max").read_text()) + 1


def test_proc_stat_reads_the_state_and_start_time_of_this_process() -> None:
    stat = proc_stat(os.getpid())
    assert stat is not None and stat.state == "R"
    assert stat.start_time == int(Path("/proc/self/stat").read_text().split()[21])


def test_proc_stat_of_a_missing_pid_is_none() -> None:
    assert proc_stat(_missing_pid()) is None


def test_proc_stat_parses_a_name_with_spaces_and_parentheses(until: Until) -> None:
    with subprocess.Popen(
        [sys.executable, "-c", RENAMING_CHILD], stdout=subprocess.PIPE, text=True
    ) as child:
        try:
            assert child.stdout is not None
            start_time = int(child.stdout.readline())
            assert child.stdout.readline() == "renamed\n"
            assert Path(f"/proc/{child.pid}/comm").read_text() == "a) b (c\n"
            # Still R between its print and time.sleep (for a while under load): wait for S. A
            # parse that splits at the wrong ")" never yields "S" and times out here.
            until(lambda: _state(child.pid) == "S", 10, "the sleep")
            stat = proc_stat(child.pid)
            assert stat is not None and stat.state == "S" and stat.start_time == start_time
        finally:
            child.kill()


def test_a_zombie_is_not_alive_and_is_gone_once_reaped(until: Until) -> None:
    child = subprocess.Popen(["true"])
    start_time = _start_time(child.pid)  # unreaped, so readable even after it exited
    until(lambda: _state(child.pid) == "Z", 10, "the zombie")
    assert not is_alive(child.pid, start_time)
    assert not is_alive(child.pid, None)
    assert kill_and_reap(child.pid, start_time, 0.1)  # without our handle: gone, not reaped
    assert _state(child.pid) == "Z"
    assert kill_and_reap(child.pid, start_time, 1.0, child)
    assert proc_stat(child.pid) is None and child.returncode == 0  # reaped: no zombie left


@pytest.mark.skipif(SYS_EXIT is None, reason="the exit syscall number of this machine is unknown")
def test_kill_and_reap_waits_until_our_zombie_child_can_be_reaped(until: Until) -> None:
    child = subprocess.Popen([sys.executable, "-c", LINGERING_LEADER])
    start_time = _start_time(child.pid)
    until(lambda: _state(child.pid) == "Z", 10, "the zombie leader")
    assert child.poll() is None  # its other thread still runs, so it can't be reaped yet
    assert kill_and_reap(child.pid, start_time, 5.0, child)
    assert child.returncode == 0 and proc_stat(child.pid) is None
    assert kill_and_reap(child.pid, None, 0.1, child)  # already reaped: its pid is left alone


def test_is_alive_checks_the_start_time(sleeper: Sleeper) -> None:
    other = sleeper()
    start_time = _start_time(other.pid)
    assert is_alive(other.pid, start_time)
    assert is_alive(other.pid, None)
    assert not is_alive(other.pid, start_time + 1)


def test_kill_and_reap_kills_the_whole_group_and_reaps_our_child(until: Until) -> None:
    with subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, start_new_session=True
    ) as leader:
        assert leader.stdout is not None
        member = int(leader.stdout.readline())
        member_start = _start_time(member)
        assert kill_and_reap(leader.pid, _start_time(leader.pid), 2.0, leader)
        assert proc_stat(leader.pid) is None  # killed and reaped by kill_and_reap itself
        until(lambda: not is_alive(member, member_start), 10, "the group member's end")


def test_kill_and_reap_leaves_a_reused_pid_alone(sleeper: Sleeper) -> None:
    other = sleeper()
    start_time = _start_time(other.pid)
    assert kill_and_reap(other.pid, start_time - 1, 0.5)  # "gone": not the recorded process
    assert other.poll() is None and is_alive(other.pid, start_time)


def test_kill_and_reap_kills_a_stopped_process(sleeper: Sleeper, until: Until) -> None:
    other = sleeper()
    os.kill(other.pid, signal.SIGSTOP)
    until(lambda: _state(other.pid) == "T", 10, "the stop")
    assert kill_and_reap(other.pid, _start_time(other.pid), 2.0, other)
    assert proc_stat(other.pid) is None


def test_kill_and_reap_confirms_a_process_that_is_not_our_child(strays: Track) -> None:
    made = subprocess.run(
        [sys.executable, "-c", ORPHAN_MAKER], capture_output=True, text=True, check=True
    )
    orphan = int(made.stdout)
    strays(orphan)
    start_time = _start_time(orphan)
    with pytest.raises(ChildProcessError):
        os.waitpid(orphan, os.WNOHANG)
    assert kill_and_reap(orphan, start_time, 5.0)
    assert not is_alive(orphan, start_time)


def test_kill_and_reap_without_a_group_kills_only_the_pid() -> None:
    same_group = subprocess.Popen(["sleep", "60"])  # in this process's group, leads none
    try:
        assert kill_and_reap(same_group.pid, _start_time(same_group.pid), 2.0, same_group)
        assert proc_stat(same_group.pid) is None
    finally:
        if same_group.poll() is None:
            same_group.kill()
        same_group.wait(timeout=10)
    # Reaching this line proves our own group was never signalled.


def test_kill_and_reap_never_signals_this_process() -> None:
    assert not kill_and_reap(os.getpid(), _start_time(os.getpid()), 0.1)


def test_kill_and_reap_of_a_missing_pid_is_true() -> None:
    assert kill_and_reap(_missing_pid(), 1, 0.1)
