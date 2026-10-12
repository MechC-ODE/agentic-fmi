"""Fixtures for the worker tests: queued run directories and real worker processes.

Collaborators: the shared ``workspace`` and ``fmu`` fixtures (``tests/conftest.py``),
``agentic_fmi.store.runstore`` (the run directory a server would prepare: ``spec.json`` plus a
``queued`` status) and ``agentic_fmi.fmu.inspect`` (the resolved experiment). ``spawn`` starts
``python -m agentic_fmi worker <run_dir>`` as the supervisor will: a new session, stdin from
``/dev/null``, stdout and stderr appended to ``log.txt``, and ``TMPDIR`` pointed at the test's
private ``worker_tmp``. Every child is killed and reaped at teardown, whatever the test did.
System runs (ADR 0009): ``built_fmus`` builds the pythonfmu PI, plant and stalling slaves once
per session, ``pythonfmu`` copies one into the workspace, and ``new_system_run`` writes a
``SystemSpec`` with ``system_support`` (importable because pytest puts this directory on
``sys.path``). Those two import it inside the fixture: ``tests/supervisor/conftest.py`` loads
this file by path, without this directory on ``sys.path``, for its run-directory fixtures.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.errors import ProtocolError
from agentic_fmi.fmu.inspect import read_description, resolve_experiment, sha256_file
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    FmuRef,
    InputSignal,
    RunSpec,
    RunState,
    RunStatus,
    StartValue,
    Versions,
    utc_now,
)

WORKER_COMMAND = [sys.executable, "-m", "agentic_fmi", "worker"]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]


@pytest.fixture
def store_dir(workspace: Path) -> Path:
    """The run store of the test workspace."""
    return workspace / ".agentic-fmi"


@pytest.fixture
def new_run(store_dir: Path) -> RunBuilder:
    """``new_run(fmu_path, fmi_type, outputs=..., start_values=..., inputs=..., timeout_s=...,
    force=..., live=..., idle_limit_s=..., **experiment)`` claims a run ID, writes ``spec.json``
    and a ``queued`` status, and returns the run directory. Outputs default to the FMU's outputs."""

    def build(
        fmu_path: Path,
        fmi_type: str = "CoSimulation",
        *,
        outputs: list[str] | None = None,
        start_values: Mapping[str, StartValue] | None = None,
        inputs: Mapping[str, InputSignal] | None = None,
        timeout_s: float = 60.0,
        force: bool = False,
        live: bool = False,
        idle_limit_s: float | None = None,
        **experiment: Any,
    ) -> Path:
        md = read_description(fmu_path)
        resolved = resolve_experiment(md, fmi_type, **experiment)
        run_id, run_path = runstore.claim_run_id(store_dir)
        spec = RunSpec(
            run_id=run_id,
            created_at=utc_now(),
            fmu=FmuRef(
                path=str(fmu_path),
                sha256=sha256_file(fmu_path),
                size_bytes=fmu_path.stat().st_size,
                fmi_version=md.fmiVersion,
                model_name=md.modelName,
            ),
            fmi_type=fmi_type,
            experiment=resolved,
            start_values=dict(start_values or {}),
            inputs=dict(inputs or {}),
            outputs=outputs
            if outputs is not None
            else [v.name for v in md.modelVariables if v.causality == "output"],
            timeout_s=timeout_s,
            force=force,
            live=live,
            idle_limit_s=idle_limit_s,
            versions=Versions.current(),
        )
        runstore.write_spec(run_path, spec)
        queued = RunStatus(
            run_id=run_id,
            state=RunState.QUEUED,
            t_start=resolved.start_time,
            t_stop=resolved.stop_time,
            updated_at=utc_now(),
        )
        runstore.write_status(run_path, queued)
        return run_path

    return build


@pytest.fixture(scope="session")
def built_fmus(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The pythonfmu FMUs of ``system_support.SOURCES``, built once per session; never
    modified (``pythonfmu`` hands out copies)."""
    from system_support import build_fmus

    return build_fmus(tmp_path_factory.mktemp("pythonfmu"))


@pytest.fixture
def pythonfmu(built_fmus: dict[str, Path], workspace: Path) -> Callable[[str], Path]:
    """``pythonfmu(name)`` copies the built ``<name>.fmu`` to ``<workspace>/fmus/`` once and
    returns the copy, which a test may modify."""

    def copy_fmu(name: str) -> Path:
        target = workspace / "fmus" / f"{name}.fmu"
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            shutil.copy2(built_fmus[name], target)
        return target

    return copy_fmu


@pytest.fixture
def new_system_run(store_dir: Path, workspace: Path) -> Callable[..., Path]:
    """``new_system_run(components, **system_spec_options)`` claims a run ID, writes the
    ``SystemSpec`` (``system_support.system_spec``) and a ``queued`` status, and returns the run
    directory."""
    from system_support import system_spec

    def build(components: Mapping[str, Path], **options: Any) -> Path:
        run_id, run_path = runstore.claim_run_id(store_dir)
        spec = system_spec(run_id, workspace / f"{run_id}.system.json", components, **options)
        runstore.write_spec(run_path, spec)
        queued = RunStatus(
            run_id=run_id,
            state=RunState.QUEUED,
            t_start=spec.experiment.start_time,
            t_stop=spec.experiment.stop_time,
            updated_at=utc_now(),
        )
        runstore.write_status(run_path, queued)
        return run_path

    return build


@pytest.fixture
def worker_tmp(tmp_path: Path) -> Path:
    """The temp directory (``TMPDIR``) of the workers ``spawn`` starts, private to the test: a
    test can check that a worker left nothing there (where ``tempfile.mkdtemp`` and FMPy would
    put theirs) without racing other processes that use the shared ``/tmp``."""
    path = tmp_path / "worker-tmp"
    path.mkdir()
    return path


@pytest.fixture
def spawn(worker_tmp: Path) -> Iterator[Spawn]:
    """``spawn(run_dir)`` starts a worker process the way the supervisor does, with ``TMPDIR``
    set to :func:`worker_tmp`."""
    children: list[subprocess.Popen[bytes]] = []

    def start(run_path: Path) -> subprocess.Popen[bytes]:
        with (run_path / runstore.LOG_FILE).open("ab") as log:
            child = subprocess.Popen(
                [*WORKER_COMMAND, str(run_path)],
                env={**os.environ, "TMPDIR": str(worker_tmp)},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        children.append(child)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)


@pytest.fixture
def run_worker(spawn: Spawn) -> Callable[[Path], RunStatus]:
    """``run_worker(run_dir)`` runs a worker to completion (exit code 0) and returns the status."""

    def run(run_path: Path) -> RunStatus:
        child = spawn(run_path)
        assert child.wait(timeout=60) == 0, (run_path / runstore.LOG_FILE).read_text()
        return runstore.read_status(run_path)

    return run


def wait_for_status(
    run_path: Path, condition: Callable[[RunStatus], bool], timeout_s: float = 30.0
) -> RunStatus:
    """Poll ``status.json`` until ``condition`` holds; fail the test after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while True:
        with suppress(ProtocolError):
            status = runstore.read_status(run_path)
            if condition(status):
                return status
        if time.monotonic() > deadline:
            pytest.fail(f"status condition not met within {timeout_s} s")
        time.sleep(0.02)


@pytest.fixture
def await_status() -> Callable[..., RunStatus]:
    """:func:`wait_for_status` as a fixture, for the test modules."""
    return wait_for_status
