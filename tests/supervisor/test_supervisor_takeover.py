"""Two servers on one store: a ``Supervisor`` that didn't spawn the worker (no ``Popen``
handle, and the worker isn't even its child) takes it over or stops it using only
``status.json`` and ``/proc``; two supervisors racing on one dead worker write its end once.

"Server A" is a separate interpreter that spawns the worker with ``Supervisor.spawn`` and
exits, so init adopts the worker and ``waitpid`` fails for the test process (server B).
Collaborators: ``agentic_fmi.supervisor`` (under test), ``agentic_fmi.store.runstore``, and the
fixtures in ``tests/supervisor/conftest.py`` (``strays`` kills adopted workers at teardown).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.config import Config
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import TERMINAL_STATES, ErrorCode, RunState, RunStatus
from agentic_fmi.supervisor import Supervisor, is_alive, proc_stat

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
AwaitStatus = Callable[..., RunStatus]
Until = Callable[..., Any]
Track = Callable[[int], None]

SERVER_A = """
import sys
from pathlib import Path
from agentic_fmi.config import load_config
from agentic_fmi.supervisor import Supervisor
print(Supervisor(load_config(Path(sys.argv[1]))).spawn(Path(sys.argv[2])))
"""
"""Another server process: spawns the worker for ``argv[2]`` and exits at once."""


def _terminal(status: RunStatus) -> RunStatus | None:
    return status if status.state in TERMINAL_STATES else None


def _server_events(store_dir: Path, run_id: str) -> list[dict[str, Any]]:
    lines = (store_dir / runstore.EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    events = map(json.loads, lines)
    return [e for e in events if e["run_id"] == run_id and e["source"] == "server"]


def _spawn_elsewhere(workspace: Path, run_path: Path, strays: Track) -> int:
    """Run server A; return the pid of the worker it left behind (adopted by init)."""
    done = subprocess.run(
        [sys.executable, "-c", SERVER_A, str(workspace), str(run_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    pid = int(done.stdout)
    strays(pid)
    return pid


def _long_run(fmu: FmuFactory, queued_run: RunBuilder) -> Path:
    return queued_run(fmu("2.0", "VanDerPol"), stop_time=1e6, output_interval=0.01)


def test_another_server_takes_over_a_suspended_worker_it_did_not_spawn(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    workspace: Path,
    config: Config,
    strays: Track,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    pid = _spawn_elsewhere(workspace, run_path, strays)
    running = await_status(run_path, lambda s: s.state is RunState.RUNNING)
    assert running.pid == pid and running.pid_start_time is not None
    with pytest.raises(ChildProcessError):  # not our child: waitpid can't reap it
        os.waitpid(pid, os.WNOHANG)
    os.killpg(pid, signal.SIGSTOP)

    server_b = Supervisor(config)
    status = until(lambda: _terminal(server_b.reconcile(run_path.name)), 10, "the takeover")
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.WORKER_LOST
    assert "sent no heartbeat" in status.error.message
    assert not is_alive(pid, running.pid_start_time)  # killed; init reaps it
    until(lambda: proc_stat(pid) is None, 10, "init's reaping")
    assert not (run_path / runstore.FMU_DIR).exists()


def test_another_server_stops_a_worker_it_did_not_spawn(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    workspace: Path,
    config: Config,
    strays: Track,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    pid = _spawn_elsewhere(workspace, run_path, strays)
    await_status(run_path, lambda s: s.state is RunState.RUNNING and (s.t or 0) > 1.0)

    status = Supervisor(config).stop(run_path.name)
    assert status.state is RunState.STOPPED and status.error is None
    assert len(runstore.read_result(run_path)) > 100  # the worker's own partial result
    until(lambda: proc_stat(pid) is None, 10, "the worker's exit")


def test_two_servers_racing_on_a_dead_worker_record_it_once(
    fmu: FmuFactory,
    queued_run: RunBuilder,
    config: Config,
    store_dir: Path,
    supervisor: Supervisor,
    await_status: AwaitStatus,
    until: Until,
) -> None:
    run_path = _long_run(fmu, queued_run)
    pid = supervisor.spawn(run_path)  # server A holds the handle; server B has none
    await_status(run_path, lambda s: s.state is RunState.RUNNING)
    os.killpg(pid, signal.SIGKILL)
    until(lambda: (stat := proc_stat(pid)) is not None and stat.state == "Z", 10, "the zombie")

    servers = [supervisor, Supervisor(config)]
    barrier = threading.Barrier(len(servers))
    results: list[RunStatus] = []

    def reconcile(server: Supervisor) -> None:
        barrier.wait()
        results.append(server.reconcile(run_path.name))

    threads = [threading.Thread(target=reconcile, args=(server,)) for server in servers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2 and results[0] == results[1]
    assert results[0].state is RunState.FAILED
    assert [e["type"] for e in _server_events(store_dir, run_path.name)] == ["failed"]
    # B counts A's zombie as gone without reaping it (it holds no handle); A reaps it, if not
    # in its own takeover then on its next call.
    until(lambda: supervisor.reconcile(run_path.name) and proc_stat(pid) is None, 10, "A's reap")
