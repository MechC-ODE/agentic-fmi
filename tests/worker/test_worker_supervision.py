"""Worker self-supervision with real processes: the heartbeat thread, the timeout watchdog,
SIGTERM (clean stop with a partial result) and SIGKILL (the precondition the server detects).

Collaborators: ``agentic_fmi.worker`` (under test, as ``python -m agentic_fmi worker``), the
fixtures in ``tests/worker/conftest.py``, ``agentic_fmi.store.runstore``. Roberts 3.0 in
Co-Simulation is the hung native step: its first ``doStep`` takes far longer than any test, so
only the watchdog can end it (spike (a)).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState, RunStatus

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
AwaitStatus = Callable[..., RunStatus]

TIMEOUT_S = 5.0
POLL_S = 0.1
WATCHDOG_SLACK_S = 1.5
"""First ``running`` seen (the worker's clock starts) to ``failed{timeout}`` seen: one heartbeat
tick (0.2 s), the write and one poll, with room for a loaded machine. Measured on this
process's monotonic clock, never with the store's wall-clock timestamps: WSL2 steps the wall
clock by about a second now and then."""
SPAWN_ALLOWANCE_S = 15.0
"""Interpreter start and imports before the worker writes ``running``: about 1 s on an idle
machine, several seconds under CPU load. Not part of ``timeout_s``, so not asserted tightly."""


def _start_time(pid: int) -> int:
    """Field 22 of ``/proc/<pid>/stat``, parsed independently of the worker."""
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text.rsplit(")", 1)[1].split()[19])


def _is_running(status: RunStatus) -> bool:
    return status.state is RunState.RUNNING


def _beat_age(status: RunStatus, earlier: RunStatus) -> float:
    """Seconds between two heartbeats (0 if either is missing)."""
    if status.heartbeat_at is None or earlier.heartbeat_at is None:
        return 0.0
    return (status.heartbeat_at - earlier.heartbeat_at).total_seconds()


def _long_run(fmu: FmuFactory, new_run: RunBuilder) -> Path:
    """VanDerPol CS with a 0.01 s output grid to t = 1e6: it runs until a signal ends it."""
    return new_run(fmu("2.0", "VanDerPol"), stop_time=1e6, output_interval=0.01)


@pytest.mark.slow
def test_timeout_kills_a_hung_native_step(
    fmu: FmuFactory, new_run: RunBuilder, spawn: Spawn, worker_tmp: Path
) -> None:
    run_path = new_run(fmu("3.0", "Roberts"), timeout_s=TIMEOUT_S, force=True)
    limit = SPAWN_ALLOWANCE_S + TIMEOUT_S + WATCHDOG_SLACK_S
    started = time.monotonic()
    child = spawn(run_path)
    beats: list[RunStatus] = []
    running_seen: float | None = None
    while (status := runstore.read_status(run_path)).state in (RunState.QUEUED, RunState.RUNNING):
        if status.state is RunState.RUNNING:
            beats.append(status)
            if running_seen is None:
                running_seen = time.monotonic()
        assert time.monotonic() - started < limit, "the watchdog never fired"
        time.sleep(POLL_S)
    failed_seen = time.monotonic()

    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.TIMEOUT
    assert "timeout_s" in status.error.message
    assert failed_seen - started >= TIMEOUT_S
    # The watchdog's precision, without the interpreter start: the first `running` may be seen
    # up to a poll (more under load) after the clock started, hence the lower bound's margin.
    assert running_seen is not None
    assert TIMEOUT_S - 5 * POLL_S <= failed_seen - running_seen < TIMEOUT_S + WATCHDOG_SLACK_S
    heartbeats = sorted({beat.heartbeat_at for beat in beats if beat.heartbeat_at})
    assert len(heartbeats) >= 5
    assert (heartbeats[-1] - heartbeats[0]).total_seconds() > TIMEOUT_S - 2
    assert {beat.t for beat in beats} == {status.t_start}  # doStep never returned
    child.wait(timeout=2)  # the process is gone: it ended itself
    assert child.returncode in (0, -signal.SIGKILL)
    assert not Path(f"/proc/{child.pid}").exists()
    assert not (run_path / runstore.FMU_DIR).exists()
    assert list(worker_tmp.iterdir()) == []  # the worker's TMPDIR, private to this test


def test_sigterm_stops_the_run_with_a_partial_result(
    fmu: FmuFactory, new_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = _long_run(fmu, new_run)
    child = spawn(run_path)
    running = await_status(run_path, lambda s: _is_running(s) and (s.t or 0.0) > 1.0)
    assert running.pid == child.pid
    assert running.pid_start_time == _start_time(child.pid)
    assert 0.0 < running.progress < 1.0
    os.killpg(child.pid, signal.SIGTERM)
    assert child.wait(timeout=10) == 0

    status = runstore.read_status(run_path)
    assert status.state is RunState.STOPPED
    assert status.error is None and status.finished_at is not None
    result = runstore.read_result(run_path)
    assert len(result) > 100
    assert result["time"][-1] == pytest.approx(status.t)
    assert running.t is not None and status.t is not None and status.t >= running.t
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.state is RunState.STOPPED
    assert summary.n_points == len(result) and summary.t_end < 1e6
    assert not (run_path / runstore.FMU_DIR).exists()


def test_sigkill_leaves_running_with_a_stale_heartbeat(
    fmu: FmuFactory, new_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = _long_run(fmu, new_run)
    child = spawn(run_path)
    # Past the first steps, so fmu/ is extracted: what a crash mid-run leaves behind.
    await_status(run_path, lambda s: _is_running(s) and (s.t or 0.0) > 0.0)
    os.killpg(child.pid, signal.SIGKILL)
    assert child.wait(timeout=10) == -signal.SIGKILL
    killed = runstore.read_status(run_path)
    time.sleep(1.5)  # three heartbeat intervals: nobody beats any more

    status = runstore.read_status(run_path)
    assert status == killed
    assert status.state is RunState.RUNNING  # the server's takeover turns this into worker_lost
    assert status.pid == child.pid
    assert (run_path / runstore.FMU_DIR).is_dir()  # the server must clean it up on takeover


def test_heartbeat_advances_and_progress_follows_the_simulation(
    fmu: FmuFactory, new_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = _long_run(fmu, new_run)
    child = spawn(run_path)
    first = await_status(run_path, lambda s: _is_running(s) and s.heartbeat_at is not None)
    later = await_status(run_path, lambda s: _beat_age(s, first) >= 1.0, timeout_s=5.0)
    assert later.state is RunState.RUNNING
    assert first.t is not None and later.t is not None and later.t > first.t
    assert later.progress >= first.progress
    os.killpg(child.pid, signal.SIGTERM)
    assert child.wait(timeout=10) == 0
