"""``Supervisor.stop`` with real workers: a clean stop through the worker's SIGTERM handler
(partial result), the SIGKILL fallback for Roberts CS stuck in a native step and for a
SIGSTOPped worker, a stop right after spawn, a queued run without a worker, a worker that
starts as the grace ends, and a dead worker.

Collaborators: ``agentic_fmi.supervisor`` (under test), the worker it spawns,
``agentic_fmi.store.runstore``, and the fixtures in ``tests/supervisor/conftest.py``. The stop
grace is the default 2 s.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.config import Config
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState, RunStatus, utc_now
from agentic_fmi.supervisor import Supervisor, Target, proc_stat

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
AwaitStatus = Callable[..., RunStatus]
Until = Callable[..., Any]
Sleeper = Callable[[], "subprocess.Popen[bytes]"]


def _last_event(store_dir: Path, run_id: str) -> dict[str, Any]:
    lines = (store_dir / runstore.EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["run_id"] == run_id][-1]


def _state(pid: int) -> str | None:
    stat = proc_stat(pid)
    return stat.state if stat else None


def _long_run(fmu: FmuFactory, queued_run: RunBuilder) -> Path:
    """VanDerPol CS with a 0.01 s output grid to t = 1e6: it runs until a signal ends it."""
    return queued_run(fmu("2.0", "VanDerPol"), stop_time=1e6, output_interval=0.01)


def _stage(run_path: Path, **fields: Any) -> None:
    """Overwrite ``status.json`` as if a worker had written ``fields``."""
    runstore.write_status(run_path, runstore.read_status(run_path).model_copy(update=fields))


def _gone(supervisor: Supervisor, run_id: str, pid: int, until: Until) -> None:
    """Wait until the worker has exited and the supervisor has reaped it."""
    until(lambda: supervisor.reconcile(run_id) and proc_stat(pid) is None, 10, "the reaping")


def _assert_server_stopped(status: RunStatus, run_path: Path, store_dir: Path) -> None:
    assert status.state is RunState.STOPPED
    assert status.error is None and status.finished_at is not None
    assert not (run_path / runstore.FMU_DIR).exists()
    event = _last_event(store_dir, status.run_id)
    assert (event["type"], event["source"]) == ("stopped", "server")
    assert runstore.read_status(run_path) == status


def test_stop_lets_the_worker_record_a_partial_result(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    await_status: AwaitStatus,
    until: Until,
    config: Config,
) -> None:
    run_path = _long_run(fmu, queued_run)
    run_id = run_path.name
    pid = supervisor.spawn(run_path)
    running = await_status(run_path, lambda s: s.state is RunState.RUNNING and (s.t or 0) > 1.0)

    started = time.monotonic()
    status = supervisor.stop(run_id)
    assert time.monotonic() - started < config.timing.stop_grace_s
    assert status.state is RunState.STOPPED and status.error is None
    assert status.t is not None and running.t is not None and status.t >= running.t
    result = runstore.read_result(run_path)
    assert len(result) > 100 and result["time"][-1] == status.t
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.state is RunState.STOPPED
    event = _last_event(store_dir, run_id)
    assert (event["type"], event["source"]) == ("stopped", "worker")
    assert supervisor.stop(run_id) == status  # idempotent
    _gone(supervisor, run_id, pid, until)
    assert not (run_path / runstore.FMU_DIR).exists()
    assert next(e.state for e in runstore.read_index(store_dir).runs if e.run_id == run_id) == (
        RunState.STOPPED
    )


def test_stop_kills_a_worker_stuck_in_a_native_step_after_the_grace(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    await_status: AwaitStatus,
    config: Config,
) -> None:
    run_path = queued_run(fmu("3.0", "Roberts"), force=True)  # the first doStep takes ages
    pid = supervisor.spawn(run_path)
    beat = await_status(run_path, lambda s: s.state is RunState.RUNNING).heartbeat_at
    assert beat is not None
    await_status(  # one second into the run: inside the native doStep
        run_path,
        lambda s: s.heartbeat_at is not None and (s.heartbeat_at - beat).total_seconds() >= 1.0,
    )
    assert (run_path / runstore.FMU_DIR).is_dir()

    started = time.monotonic()
    status = supervisor.stop(run_path.name)
    elapsed = time.monotonic() - started
    grace = config.timing.stop_grace_s
    assert grace <= elapsed < grace + 1.5
    _assert_server_stopped(status, run_path, store_dir)
    assert status.t == status.t_start  # doStep never returned
    assert "2 s grace" in _last_event(store_dir, status.run_id)["message"]
    assert not (run_path / runstore.RESULT_FILE).exists()  # nothing partial to keep
    assert proc_stat(pid) is None  # killed and reaped


def test_stop_kills_a_suspended_worker_after_the_grace(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    pid = supervisor.spawn(run_path)
    await_status(run_path, lambda s: s.state is RunState.RUNNING)
    os.kill(pid, signal.SIGSTOP)
    until(lambda: _state(pid) == "T", 10, "the stop")

    status = supervisor.stop(run_path.name)  # SIGTERM stays pending; SIGKILL ends it
    _assert_server_stopped(status, run_path, store_dir)
    assert proc_stat(pid) is None


def test_stop_right_after_spawn_ends_stopped(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    pid = supervisor.spawn(run_path)

    status = supervisor.stop(run_path.name)  # the worker may not even have its handler yet
    assert status.state is RunState.STOPPED and status.error is None
    _gone(supervisor, run_path.name, pid, until)
    assert runstore.read_status(run_path) == status  # nobody wrote after the stop
    assert not (run_path / runstore.FMU_DIR).exists()
    assert _last_event(store_dir, status.run_id)["type"] == "stopped"


def test_stop_of_a_queued_run_without_a_worker_waits_the_grace(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    config: Config,
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    started = time.monotonic()
    status = supervisor.stop(run_path.name)
    assert time.monotonic() - started >= config.timing.stop_grace_s
    _assert_server_stopped(status, run_path, store_dir)
    assert "before a worker took the run over" in _last_event(store_dir, status.run_id)["message"]


def test_a_worker_that_starts_as_the_grace_ends_is_stopped_too(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    sleeper: Sleeper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    worker = sleeper()  # another server's worker, which leaves queued just as the grace ends
    stat = proc_stat(worker.pid)
    assert stat is not None
    request = Supervisor._request_stop
    targets: list[Target | None] = []

    def request_then_start(
        self: Supervisor, run_id: str, path: Path
    ) -> tuple[RunStatus, Target | None]:
        status, target = request(self, run_id, path)
        if not targets:  # its compare-and-set wins the race with the server's `stopped`
            beat = utc_now()
            fields = {"pid": worker.pid, "pid_start_time": stat.start_time, "t": 0.25}
            _stage(path, state=RunState.RUNNING, heartbeat_at=beat, updated_at=beat, **fields)
            (path / runstore.FMU_DIR).mkdir()
        targets.append(target)
        return status, target

    monkeypatch.setattr(Supervisor, "_request_stop", request_then_start)
    status = supervisor.stop(run_path.name)

    _assert_server_stopped(status, run_path, store_dir)
    assert targets == [None, (worker.pid, stat.start_time)]  # the started worker was stopped
    assert worker.wait(timeout=10) == -signal.SIGTERM


def test_stop_of_a_dead_worker_reports_it_lost(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    pid = supervisor.spawn(run_path)
    await_status(run_path, lambda s: s.state is RunState.RUNNING)
    os.killpg(pid, signal.SIGKILL)
    until(lambda: _state(pid) == "Z", 10, "the zombie")

    status = supervisor.stop(run_path.name)
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.WORKER_LOST
    assert proc_stat(pid) is None
