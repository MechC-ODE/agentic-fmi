"""``Supervisor.spawn`` and ``Supervisor.reconcile`` with real workers and staged statuses: a
normal run, a SIGKILLed worker, a zombie child that can't be reaped yet, a SIGSTOPped worker,
pid reuse, a status naming the server itself, runs that never started, a takeover that loses
the race to a worker's start, terminal runs, and ``reconcile_all``/``active_run_ids``.

Collaborators: ``agentic_fmi.supervisor`` (under test), the worker it spawns
(``python -m agentic_fmi worker``), ``agentic_fmi.store.runstore``, and the fixtures in
``tests/supervisor/conftest.py``. The stale threshold is 2 s here (``TEST_TIMING``).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi import supervisor as supervision
from agentic_fmi.config import Config, Timing
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    TERMINAL_STATES,
    ErrorCode,
    RunState,
    RunStatus,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor, is_alive, proc_stat

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
AwaitStatus = Callable[..., RunStatus]
Until = Callable[..., Any]
Sleeper = Callable[[], "subprocess.Popen[bytes]"]

STALE_S = 2.0
"""``TEST_TIMING.heartbeat_stale_s``."""

SYS_EXIT = {"x86_64": 60, "aarch64": 93}.get(os.uname().machine)
"""The ``exit`` syscall (one thread, unlike ``exit_group``) on this machine, if known."""

LINGERING_THREAD = f"""
import ctypes, os, threading, time
threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0))).start()
ctypes.CDLL(None).syscall({SYS_EXIT}, 0)
"""
"""A worker stand-in whose main thread exits alone: ``/proc`` shows the group leader as a zombie
at once, but ``waitpid`` can't reap it until its last thread ends 0.5 s later (as while the
threads of a SIGKILLed worker are still exiting under load)."""


def _terminal(status: RunStatus) -> RunStatus | None:
    return status if status.state in TERMINAL_STATES else None


def _state(pid: int) -> str | None:
    stat = proc_stat(pid)
    return stat.state if stat else None


def _events(store_dir: Path, run_id: str) -> list[dict[str, Any]]:
    path = store_dir / runstore.EVENTS_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [event for event in map(json.loads, lines) if event["run_id"] == run_id]


def _indexed_state(store_dir: Path, run_id: str) -> RunState:
    return next(e.state for e in runstore.read_index(store_dir).runs if e.run_id == run_id)


def _long_run(fmu: FmuFactory, queued_run: RunBuilder) -> Path:
    """VanDerPol CS with a 0.01 s output grid to t = 1e6: it runs until a signal ends it."""
    return queued_run(fmu("2.0", "VanDerPol"), stop_time=1e6, output_interval=0.01)


def _stage(run_path: Path, **fields: Any) -> RunStatus:
    """Overwrite ``status.json`` as if a worker (or a past server) had written ``fields``."""
    status = runstore.read_status(run_path).model_copy(update=fields)
    runstore.write_status(run_path, status)
    return status


def _running(pid: int, start_time: int | None, beat_age_s: float = 0.0) -> dict[str, Any]:
    beat = utc_now() - timedelta(seconds=beat_age_s)
    return {
        "state": RunState.RUNNING,
        "pid": pid,
        "pid_start_time": start_time,
        "started_at": beat,
        "heartbeat_at": beat,
        "updated_at": beat,
        "t": 0.25,
    }


def _assert_worker_lost(status: RunStatus, words: str) -> str:
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.WORKER_LOST
    assert words in status.error.message and "start a new run" in status.error.message
    assert len(status.error.message) <= 500 and status.finished_at is not None
    return status.error.message


def test_a_supervised_run_finishes_and_its_child_is_reaped(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    until: Until,
) -> None:
    run_path = queued_run(fmu("3.0", "BouncingBall"))
    run_id = run_path.name
    assert _indexed_state(store_dir, run_id) is RunState.QUEUED
    pid = supervisor.spawn(run_path)

    status = until(lambda: _terminal(supervisor.reconcile(run_id)), 60, "the end of the run")
    assert status.state is RunState.FINISHED and status.pid == pid and status.error is None
    assert (run_path / runstore.LOG_FILE).is_file()
    assert runstore.read_summary(run_path) is not None
    assert _indexed_state(store_dir, run_id) is RunState.FINISHED
    until(lambda: supervisor.reconcile(run_id) and proc_stat(pid) is None, 10, "the reaping")
    assert run_id not in supervisor._children
    assert [(e["type"], e["source"]) for e in _events(store_dir, run_id)] == [
        ("started", "worker"),
        ("finished", "worker"),
    ]


def test_a_killed_worker_is_taken_over_as_worker_lost(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    run_id = run_path.name
    pid = supervisor.spawn(run_path)
    running = await_status(run_path, lambda s: s.state is RunState.RUNNING and (s.t or 0) > 0.5)
    with (run_path / runstore.LOG_FILE).open("a") as log:  # what an FMU might have printed
        log.write("noise\n" * 600 + "last words from the FMU\n")
    os.killpg(pid, signal.SIGKILL)
    until(lambda: _state(pid) == "Z", 10, "the zombie")
    assert (run_path / runstore.FMU_DIR).is_dir()  # what a SIGKILL leaves behind

    # A dead (or zombie) pid is taken over at once; no need to wait for the stale threshold.
    status = supervisor.reconcile(run_id)
    _assert_worker_lost(status, f"pid {pid}) ended without recording an outcome")
    assert status.pid == pid and status.t is not None and status.t >= (running.t or 0)
    tail = status.error.log_tail if status.error else None
    assert tail is not None and tail.endswith("last words from the FMU\n")
    assert len(tail.encode()) <= 2048
    assert not (run_path / runstore.FMU_DIR).exists()
    assert proc_stat(pid) is None  # killed, reaped, no zombie
    assert _indexed_state(store_dir, run_id) is RunState.FAILED
    last = _events(store_dir, run_id)[-1]
    assert (last["type"], last["source"]) == ("failed", "server")
    assert last["message"].startswith("worker_lost: ")
    assert supervisor.reconcile(run_id) == status


@pytest.mark.skipif(SYS_EXIT is None, reason="the exit syscall number of this machine is unknown")
def test_a_zombie_child_is_gone_only_once_reaped(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    until: Until,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervision, "WORKER_COMMAND", (sys.executable, "-c", LINGERING_THREAD))
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    pid = supervisor.spawn(run_path)
    stat = proc_stat(pid)
    assert stat is not None
    until(lambda: _state(pid) == "Z", 10, "the zombie leader")
    _stage(run_path, **_running(pid, stat.start_time))

    status = supervisor.reconcile(run_path.name)
    _assert_worker_lost(status, f"pid {pid}) ended without recording an outcome")
    assert proc_stat(pid) is None  # the takeover waited for the last thread, then reaped
    assert supervisor._children[run_path.name].process.returncode == 0


def test_a_suspended_worker_is_killed_once_its_heartbeat_is_stale(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    run_id = run_path.name
    pid = supervisor.spawn(run_path)
    await_status(run_path, lambda s: s.state is RunState.RUNNING and s.heartbeat_at is not None)
    os.kill(pid, signal.SIGSTOP)
    suspended = time.monotonic()
    until(lambda: _state(pid) == "T", 10, "the stop")
    assert supervisor.reconcile(run_id).state is RunState.RUNNING  # its last beat is fresh

    status = until(lambda: _terminal(supervisor.reconcile(run_id)), 10, "the takeover")
    elapsed = time.monotonic() - suspended
    _assert_worker_lost(status, "sent no heartbeat")
    assert STALE_S - 1.0 < elapsed < STALE_S + 2.0  # the last beat may predate SIGSTOP a bit
    assert proc_stat(pid) is None  # SIGKILL ends a stopped process too; reaped
    assert not (run_path / runstore.FMU_DIR).exists()


@pytest.mark.parametrize("beat_age_s", [0.0, 60.0], ids=["fresh", "stale"])
def test_a_reused_pid_is_never_killed(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    sleeper: Sleeper,
    beat_age_s: float,
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    other = sleeper()  # an unrelated process now holds the recorded pid
    stat = proc_stat(other.pid)
    assert stat is not None
    _stage(run_path, **_running(other.pid, stat.start_time - 1, beat_age_s))
    (run_path / runstore.FMU_DIR).mkdir()

    status = supervisor.reconcile(run_path.name)
    _assert_worker_lost(status, "now belongs to another process")
    assert other.poll() is None and is_alive(other.pid, stat.start_time)  # untouched
    assert not (run_path / runstore.FMU_DIR).exists()


def test_a_status_naming_the_server_itself_is_never_confirmed_dead(
    fmu: FmuFactory, queued_run: RunBuilder, config: Config
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    own = proc_stat(os.getpid())
    assert own is not None
    staged = _stage(run_path, **_running(os.getpid(), own.start_time, beat_age_s=60.0))
    before = (run_path / runstore.STATUS_FILE).read_bytes()
    quick = Supervisor(
        replace(config, timing=Timing(heartbeat_stale_s=STALE_S, kill_confirm_s=0.2))
    )

    assert quick.reconcile(run_path.name) == staged  # stale, but it can't be killed: no write
    assert (run_path / runstore.STATUS_FILE).read_bytes() == before


def test_a_run_queued_past_the_spawn_timeout_never_started(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    config: Config,
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    run_id = run_path.name
    queued = runstore.read_status(run_path)
    assert supervisor.reconcile(run_id) == queued  # young: the worker may still be starting

    limit = config.timing.spawn_timeout_s
    _stage(run_path, updated_at=queued.updated_at - timedelta(seconds=limit + 1))
    status = supervisor.reconcile(run_id)
    _assert_worker_lost(status, "The worker never started: the run stayed queued")
    assert status.error is not None and status.error.log_tail is None  # no log.txt yet
    assert _indexed_state(store_dir, run_id) is RunState.FAILED
    assert [(e["type"], e["source"]) for e in _events(store_dir, run_id)] == [("failed", "server")]


def test_a_takeover_decided_on_a_stale_queued_status_never_overwrites_a_start(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    config: Config,
    sleeper: Sleeper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    run_id = run_path.name
    queued = runstore.read_status(run_path)
    _stage(run_path, updated_at=queued.updated_at - timedelta(seconds=60))  # past the timeout
    worker = sleeper()  # another server's slow worker, which starts right now
    stat = proc_stat(worker.pid)
    assert stat is not None and config.timing.spawn_timeout_s < 60
    decide = Supervisor._problem

    def decide_then_start(self: Supervisor, run_id: str, status: RunStatus) -> str | None:
        problem = decide(self, run_id, status)  # "never started", on the queued status
        _stage(run_path, **_running(worker.pid, stat.start_time))  # its compare-and-set wins
        (run_path / runstore.FMU_DIR).mkdir()  # and it extracts the FMU
        return problem

    monkeypatch.setattr(Supervisor, "_problem", decide_then_start)
    status = supervisor.reconcile(run_id)
    monkeypatch.undo()

    assert status.state is RunState.RUNNING and status.pid == worker.pid
    assert runstore.read_status(run_path) == status
    assert (run_path / runstore.FMU_DIR).is_dir()  # the live worker's FMU is left alone
    assert worker.poll() is None and _events(store_dir, run_id) == []
    assert supervisor.reconcile(run_id) == status  # re-evaluated: alive and beating


def test_a_worker_that_exits_while_queued_is_lost_at_once(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    until: Until,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crash = "import sys; print('worker crashed on import', file=sys.stderr); sys.exit(3)"
    monkeypatch.setattr(supervision, "WORKER_COMMAND", (sys.executable, "-c", crash))
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    pid = supervisor.spawn(run_path)

    status = until(lambda: _terminal(supervisor.reconcile(run_path.name)), 10, "the takeover")
    message = _assert_worker_lost(status, "The worker never started")
    assert "exited with code 3" in message
    assert status.error is not None and "worker crashed on import" in (status.error.log_tail or "")
    assert proc_stat(pid) is None


@pytest.mark.parametrize("state", [RunState.FINISHED, RunState.FAILED, RunState.STOPPED])
def test_terminal_runs_are_left_alone(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    sleeper: Sleeper,
    store_dir: Path,
    state: RunState,
) -> None:
    run_path = queued_run(fmu("2.0", "BouncingBall"))
    run_id = run_path.name
    other = sleeper()  # a live pid with the right start time and an ancient heartbeat
    stat = proc_stat(other.pid)
    assert stat is not None
    fields = {**_running(other.pid, stat.start_time, beat_age_s=600.0), "state": state}
    staged = _stage(run_path, **fields, finished_at=utc_now())
    before = (run_path / runstore.STATUS_FILE).read_bytes()

    for _ in range(2):
        assert supervisor.reconcile(run_id) == staged
        assert supervisor.stop(run_id) == staged
    assert (run_path / runstore.STATUS_FILE).read_bytes() == before
    assert other.poll() is None  # never signalled
    assert _events(store_dir, run_id) == []
    assert _indexed_state(store_dir, run_id) is state  # only the cached state follows


def test_reconcile_all_takes_over_lost_runs_and_counts_active_ones(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = fmu("2.0", "BouncingBall")
    young, crashed, done, damaged = (queued_run(path) for _ in range(4))
    gone = subprocess.Popen(["true"])
    gone.wait()  # reaped: its pid names no process now
    _stage(crashed, **_running(gone.pid, 1))
    _stage(done, state=RunState.FINISHED, finished_at=utc_now())
    (damaged / runstore.STATUS_FILE).write_text("{broken", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agentic_fmi.supervisor"):
        statuses = supervisor.reconcile_all()
    assert {s.run_id: s.state for s in statuses} == {
        young.name: RunState.QUEUED,
        crashed.name: RunState.FAILED,
        done.name: RunState.FINISHED,
    }
    assert f"can't reconcile run {damaged.name}" in caplog.text
    states = {e.run_id: e.state for e in runstore.read_index(store_dir).runs}
    assert states == {
        young.name: RunState.QUEUED,
        crashed.name: RunState.FAILED,
        done.name: RunState.FINISHED,
        damaged.name: RunState.QUEUED,
    }
    assert supervisor.active_run_ids() == [young.name]


def test_peek_counts_runs_whose_worker_looks_fine_and_writes_nothing(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    supervisor: Supervisor,
    store_dir: Path,
    sleeper: Sleeper,
) -> None:
    path = fmu("2.0", "BouncingBall")
    young, crashed, healthy, hung, done = (queued_run(path) for _ in range(5))
    gone = subprocess.Popen(["true"])
    gone.wait()
    beating, silent = sleeper(), sleeper()  # live processes standing in for workers
    started: dict[int, int] = {}
    for run_path, process, age in ((healthy, beating, 0.0), (hung, silent, STALE_S + 1)):
        stat = proc_stat(process.pid)
        assert stat is not None
        started[process.pid] = stat.start_time
        _stage(run_path, **_running(process.pid, stat.start_time, age))
    _stage(crashed, **_running(gone.pid, 1))
    _stage(done, state=RunState.FINISHED, finished_at=utc_now())
    before = {
        p.name: (p / runstore.STATUS_FILE).read_bytes()
        for p in (young, crashed, healthy, hung, done)
    }
    index = (store_dir / runstore.INDEX_FILE).read_bytes()

    assert supervisor.peek_active_run_ids() == [young.name, healthy.name]

    after = {
        name: (store_dir / runstore.RUNS_DIR / name / runstore.STATUS_FILE).read_bytes()
        for name in before
    }
    assert after == before and (store_dir / runstore.INDEX_FILE).read_bytes() == index
    assert is_alive(silent.pid, started[silent.pid])  # not killed, unlike a reconcile
    assert supervisor.active_run_ids() == [young.name, healthy.name]  # the same answer, with writes
    assert runstore.read_status(crashed).state is RunState.FAILED


def test_each_supervisor_has_its_own_admission_lock(config: Config) -> None:
    assert Supervisor(config).admission is not Supervisor(config).admission
