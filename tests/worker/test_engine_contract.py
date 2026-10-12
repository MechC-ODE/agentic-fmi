"""The shared engine contract (ADR 0009): both engines, each through a real worker, finish, stop
on SIGTERM with a partial result and a clean terminate, keep the column order and units, and
report an error as ``simulation_error``.

Each test runs once per engine on the same Reference FMU: as an FMU run (``RunSpec``, the FMPy
engine) and as a one-component system ``M`` (``SystemSpec``, the OMSimulator engine), so the
only difference is the engine and the ``M.`` prefix of the system's columns.

Collaborators: ``agentic_fmi.worker`` (under test, as ``python -m agentic_fmi worker``),
``engines/fmpy_engine.py`` and ``engines/oms_engine.py`` (run by it), the fixtures in
``tests/worker/conftest.py`` (``new_run``, ``new_system_run``, ``spawn``, ``run_worker``) and
``agentic_fmi.store.runstore``.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState, RunStatus, StartValue

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
RunWorker = Callable[[Path], RunStatus]
AwaitStatus = Callable[..., RunStatus]

COMPONENT = "M"


@dataclass(frozen=True)
class Engine:
    """How a test starts a run on one engine and names its columns."""

    name: str
    new_run: RunBuilder
    new_system_run: RunBuilder

    def start(
        self,
        fmu_path: Path,
        outputs: Sequence[str],
        *,
        stop_time: float,
        step: float,
        values: Mapping[str, StartValue] | None = None,
    ) -> Path:
        """A queued run of ``fmu_path`` recording ``outputs`` every ``step`` up to ``stop_time``,
        with ``values`` set before initialization."""
        values = dict(values or {})
        if self.name == "fmpy":
            return self.new_run(
                fmu_path,
                "CoSimulation",
                outputs=list(outputs),
                start_values=values,
                stop_time=stop_time,
                output_interval=step,
            )
        return self.new_system_run(
            {COMPONENT: fmu_path},
            outputs=[self.column(name) for name in outputs],
            parameters={self.column(k): (v, "start_values") for k, v in values.items()},
            step_size=step,
            stop_time=stop_time,
        )

    def column(self, name: str) -> str:
        """The result column of variable ``name``."""
        return name if self.name == "fmpy" else f"{COMPONENT}.{name}"


@pytest.fixture(params=["fmpy", "oms"])
def engine(
    request: pytest.FixtureRequest, new_run: RunBuilder, new_system_run: RunBuilder
) -> Engine:
    if request.param == "oms":
        request.applymarker(pytest.mark.oms)
    return Engine(request.param, new_run, new_system_run)


def test_finishes_with_columns_in_order_and_units(
    engine: Engine, fmu: FmuFactory, run_worker: RunWorker
) -> None:
    run_path = engine.start(fmu("2.0", "BouncingBall"), ["v", "h"], stop_time=3.0, step=0.01)
    status = run_worker(run_path)

    assert status.state is RunState.FINISHED, status.error
    assert status.progress == 1.0 and status.t == pytest.approx(3.0)
    result = runstore.read_result(run_path)
    columns = [engine.column("v"), engine.column("h")]
    assert result.dtype.names == ("time", *columns)
    assert len(result) == 301
    assert result["time"][-1] == pytest.approx(3.0)
    assert result[engine.column("h")][0] == 1.0
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.state is RunState.FINISHED
    assert summary.columns == columns
    assert [(v.name, v.unit) for v in summary.variables] == list(
        zip(columns, ["m/s", "m"], strict=True)
    )
    for scratch in runstore.SCRATCH_DIRS:
        assert not (run_path / scratch).exists()


def test_both_engines_compute_the_same_trajectory(
    fmu: FmuFactory, new_run: RunBuilder, new_system_run: RunBuilder, run_worker: RunWorker
) -> None:
    """One CS FMU stepped at the same communication points gives the same result."""
    path = fmu("2.0", "BouncingBall")
    fmpy, oms = Engine("fmpy", new_run, new_system_run), Engine("oms", new_run, new_system_run)
    runs = [e.start(path, ["h"], stop_time=3.0, step=0.01, values={"e": 0.5}) for e in (fmpy, oms)]
    assert [run_worker(run).state for run in runs] == [RunState.FINISHED] * 2
    by_fmpy, by_oms = (runstore.read_result(run) for run in runs)
    assert np.allclose(by_fmpy["time"], by_oms["time"], rtol=0, atol=1e-12)
    assert np.max(np.abs(by_fmpy["h"] - by_oms[oms.column("h")])) < 1e-9


def test_sigterm_stops_with_a_partial_result_and_a_clean_terminate(
    engine: Engine, fmu: FmuFactory, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = engine.start(fmu("2.0", "VanDerPol"), ["x0"], stop_time=1e6, step=0.01)
    child = spawn(run_path)
    running = await_status(run_path, lambda s: s.state is RunState.RUNNING and (s.t or 0) > 1.0)
    os.killpg(child.pid, signal.SIGTERM)
    assert child.wait(timeout=20) == 0  # no crash in terminate/delete at exit

    status = runstore.read_status(run_path)
    assert status.state is RunState.STOPPED and status.error is None
    assert running.t is not None and status.t is not None and status.t >= running.t
    result = runstore.read_result(run_path)
    assert result.dtype.names == ("time", engine.column("x0"))
    assert len(result) > 100 and result["time"][-1] == pytest.approx(status.t)
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.state is RunState.STOPPED
    assert summary.n_points == len(result) and summary.t_end < 1e6
    log = (run_path / runstore.LOG_FILE).read_text()
    assert "Traceback" not in log and "error:" not in log  # OMSimulator logs failures so
    for scratch in runstore.SCRATCH_DIRS:
        assert not (run_path / scratch).exists()


def test_an_error_is_a_simulation_error(
    engine: Engine, fmu: FmuFactory, run_worker: RunWorker
) -> None:
    path = fmu("2.0", "BouncingBall")
    run_path = engine.start(path, ["h"], stop_time=3.0, step=0.01, values={"no_such_var": 1.0})
    status = run_worker(run_path)

    assert status.state is RunState.FAILED
    error = status.error
    assert error is not None and error.code is ErrorCode.SIMULATION_ERROR
    assert "no_such_var" in error.message and len(error.message) <= 500
    assert "Traceback" not in error.message and "Traceback" not in (error.log_tail or "")
    assert "Traceback" in (run_path / runstore.LOG_FILE).read_text()  # the detail is in the log
    assert not (run_path / runstore.RESULT_FILE).exists()
    for scratch in runstore.SCRATCH_DIRS:
        assert not (run_path / scratch).exists()
