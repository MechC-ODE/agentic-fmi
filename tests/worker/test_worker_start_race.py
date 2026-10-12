"""The worker's compare-and-set start (ADR 0007): it writes ``running`` only if ``status.json``
is still ``queued``, re-read under the store lock, so a run that a server ended before the
worker got going (the queued takeover, or ``run_stop`` of a queued run) stays ended and is
never simulated. The lock is held for that read-check-write only, never while simulating.

Collaborators: ``agentic_fmi.worker`` (under test, in process for the race and as
``python -m agentic_fmi worker`` for the lock), the fixtures in ``tests/worker/conftest.py``,
``agentic_fmi.store.atomic.locked`` (held here as the server holds it for a terminal write)
and ``agentic_fmi.store.runstore``.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fmi import worker
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import locked
from agentic_fmi.store.protocol import ErrorCode, RunError, RunSpec, RunState, RunStatus, utc_now

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
RunWorker = Callable[[Path], RunStatus]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
AwaitStatus = Callable[..., RunStatus]

PROC_LOCKS = Path("/proc/locks")
LOCK_WAIT_S = 30.0


def _end(run_path: Path, state: RunState) -> bytes:
    """End the run the way ``Supervisor._end`` does (the caller holds the store lock) and
    return the bytes written."""
    now = utc_now()
    error = RunError(code=ErrorCode.WORKER_LOST, message="The worker never started.")
    ended = runstore.read_status(run_path).model_copy(
        update={
            "state": state,
            "error": error if state is RunState.FAILED else None,
            "finished_at": now,
            "updated_at": now,
        }
    )
    runstore.write_status(run_path, ended)
    return (run_path / runstore.STATUS_FILE).read_bytes()


def _worker_events(store_dir: Path, run_id: str) -> list[dict[str, object]]:
    path = store_dir / runstore.EVENTS_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    events = map(json.loads, lines)
    return [e for e in events if e["run_id"] == run_id and e["source"] == "worker"]


def _assert_left_alone(run_path: Path, store_dir: Path, ended: bytes) -> None:
    """Status unchanged, nothing extracted or recorded, no worker event."""
    assert (run_path / runstore.STATUS_FILE).read_bytes() == ended
    for name in (runstore.FMU_DIR, runstore.RESULT_FILE, runstore.SUMMARY_FILE):
        assert not (run_path / name).exists(), name
    assert _worker_events(store_dir, run_path.name) == []


def _await_lock_wait(child: subprocess.Popen[bytes]) -> None:
    """Wait until ``child`` is blocked on an ``flock`` (a ``->`` line in ``/proc/locks``)."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        for line in PROC_LOCKS.read_text().splitlines():
            fields = line.split()
            if fields[1:2] == ["->"] and fields[5:6] == [str(child.pid)]:
                return
        assert child.poll() is None, f"the worker exited with {child.returncode} instead"
        assert time.monotonic() < deadline, "the worker never waited for the store lock"
        time.sleep(0.02)


def _lock_is_free(lock_path: Path) -> bool:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)
    return True


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.STOPPED])
def test_worker_runs_nothing_when_a_server_ended_the_run_after_its_queued_check(
    fmu: FmuFactory,
    new_run: RunBuilder,
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: RunState,
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    read_spec = runstore.read_spec
    ended: list[bytes] = []

    def server_ends_it_meanwhile(path: Path) -> RunSpec:  # after main's unlocked queued check
        with locked(store_dir / runstore.LOCK_FILE):
            ended.append(_end(run_path, state))
        return read_spec(path)

    monkeypatch.setattr(runstore, "read_spec", server_ends_it_meanwhile)
    assert worker.main(run_path) == worker.EXIT_OK
    _assert_left_alone(run_path, store_dir, ended[0])


@pytest.mark.skipif(not PROC_LOCKS.is_file(), reason="needs /proc/locks (Linux)")
def test_worker_waiting_for_the_store_lock_yields_to_the_server_holding_it(
    fmu: FmuFactory, new_run: RunBuilder, spawn: Spawn, store_dir: Path
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    with locked(store_dir / runstore.LOCK_FILE):
        child = spawn(run_path)
        _await_lock_wait(child)  # past its queued check, blocked in the compare-and-set
        ended = _end(run_path, RunState.STOPPED)
    assert child.wait(timeout=LOCK_WAIT_S) == worker.EXIT_OK
    _assert_left_alone(run_path, store_dir, ended)


def test_queued_start_still_runs_to_finished(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker, store_dir: Path
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    status = run_worker(run_path)
    assert status.state is RunState.FINISHED
    assert status.pid is not None and status.started_at is not None
    assert (run_path / runstore.RESULT_FILE).is_file()
    kinds = [e["type"] for e in _worker_events(store_dir, status.run_id)]
    assert kinds == ["started", "finished"]


def test_store_lock_is_free_while_the_worker_simulates(
    fmu: FmuFactory, new_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus, store_dir: Path
) -> None:
    run_path = new_run(fmu("2.0", "VanDerPol"), stop_time=1e6, output_interval=0.01)
    child = spawn(run_path)
    await_status(run_path, lambda s: s.state is RunState.RUNNING and (s.t or 0.0) > 0.5)
    assert _lock_is_free(store_dir / runstore.LOCK_FILE)
    os.killpg(child.pid, signal.SIGTERM)
    assert child.wait(timeout=10) == worker.EXIT_OK
    assert runstore.read_status(run_path).state is RunState.STOPPED
