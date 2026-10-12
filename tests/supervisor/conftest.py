"""Fixtures for the supervisor tests: a fast-timing ``Config``, a ``Supervisor`` whose children
are killed at teardown, queued runs with their index entry, stray-process cleanup, and the
``until`` polling helper.

Collaborators: the shared ``workspace`` and ``fmu`` fixtures (``tests/conftest.py``), and the
run-directory fixtures of ``tests/worker/conftest.py`` (``store_dir``, ``new_run`` and
``await_status``), which is loaded by path and re-exported here because conftest modules
aren't importable packages. ``agentic_fmi.supervisor`` is under test; nothing here loads an FMU
binary in the test process.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import IndexEntry
from agentic_fmi.supervisor import Supervisor, is_alive, proc_stat

RunBuilder = Callable[..., Path]

TEST_TIMING = Timing(heartbeat_stale_s=2.0, poll_interval_s=0.05)
"""Defaults except a 2 s stale threshold (four heartbeat intervals) and a faster stop poll."""


def _worker_fixtures() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "worker" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_supervisor_worker_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_worker = _worker_fixtures()
store_dir = _worker.store_dir
new_run = _worker.new_run
await_status = _worker.await_status


def wait_until(condition: Callable[[], Any], timeout_s: float = 30.0, what: str = "it") -> Any:
    """Poll ``condition`` every 20 ms until it returns something truthy; fail after the timeout."""
    deadline = time.monotonic() + timeout_s
    while not (value := condition()):
        if time.monotonic() > deadline:
            pytest.fail(f"{what} didn't happen within {timeout_s} s")
        time.sleep(0.02)
    return value


def kill_stray(pid: int, start_time: int | None) -> None:
    """SIGKILL ``pid``'s group if it is still that process, and wait until it is gone."""
    if not is_alive(pid, start_time):
        return
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    with suppress(ChildProcessError):
        os.waitpid(pid, 0)
    wait_until(lambda: not is_alive(pid, start_time), 10.0, f"the end of pid {pid}")


@pytest.fixture
def until() -> Callable[..., Any]:
    """:func:`wait_until` as a fixture: ``until(condition, timeout_s, what)``."""
    return wait_until


@pytest.fixture
def config(workspace: Path) -> Config:
    """The server configuration for the test workspace, with :data:`TEST_TIMING`."""
    return replace(load_config(workspace), timing=TEST_TIMING)


@pytest.fixture
def supervisor(config: Config) -> Iterator[Supervisor]:
    """A ``Supervisor``; every worker it spawned is killed and reaped at teardown."""
    made = Supervisor(config)
    yield made
    for child in list(made._children.values()):
        if child.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGKILL)
        child.process.wait(timeout=10)


@pytest.fixture
def strays() -> Iterator[Callable[[int], None]]:
    """``track(pid)`` records a process a test started outside the supervisor (an orphaned
    worker); it is killed at teardown if it still is the process that had that pid."""
    tracked: dict[int, int | None] = {}

    def track(pid: int) -> None:
        stat = proc_stat(pid)
        tracked[pid] = stat.start_time if stat else None

    yield track
    for pid, start_time in tracked.items():
        kill_stray(pid, start_time)


@pytest.fixture
def queued_run(new_run: RunBuilder, store_dir: Path) -> RunBuilder:
    """``new_run`` plus the ``queued`` index entry the server writes before it spawns."""

    def build(*args: Any, **kwargs: Any) -> Path:
        run_path = new_run(*args, **kwargs)
        spec, status = runstore.read_spec(run_path), runstore.read_status(run_path)
        runstore.upsert_index(
            store_dir,
            IndexEntry(
                run_id=spec.run_id,
                state=status.state,
                fmu_name=spec.fmu.model_name,
                fmu_path=spec.fmu.path,
                created_at=spec.created_at,
                updated_at=status.updated_at,
            ),
        )
        return run_path

    return build


@pytest.fixture
def sleeper() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """``sleeper()`` starts an unrelated ``sleep 60`` in its own session (killed at teardown)."""
    children: list[subprocess.Popen[bytes]] = []

    def start() -> subprocess.Popen[bytes]:
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        children.append(child)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
