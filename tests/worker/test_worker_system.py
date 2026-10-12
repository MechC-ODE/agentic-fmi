"""System runs through real workers (ADR 0009): the closed loop against its exact recurrence,
parameters set before initialization, the pin and FMU checks, what a discarded step or an FMU's
terminate request does (FMI 2.0 stalls, FMI 3.0 is ignored), and the cleanup.

Collaborators: ``agentic_fmi.worker`` (under test, as ``python -m agentic_fmi worker``, and once
in-process for the fail-closed pin check), ``agentic_fmi.engines.oms_engine`` (run by the
worker), the fixtures in ``tests/worker/conftest.py`` (``built_fmus``, ``pythonfmu``,
``new_system_run``, ``run_worker``, ``worker_tmp``), ``system_support`` (the recurrence) and
``agentic_fmi.store.runstore``. OMSimulator runs only in the workers, never in this process.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from system_support import PI_DEFAULTS, PLANT_DEFAULTS, jacobi_loop

from agentic_fmi import oms_pin, worker
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, OmsVersion, RunState, RunStatus

pytestmark = pytest.mark.oms

FmuFactory = Callable[[str, str], Path]
Pythonfmu = Callable[[str], Path]
SystemRunBuilder = Callable[..., Path]
RunWorker = Callable[[Path], RunStatus]

LOOP = [("PI.u", "P.F"), ("P.x", "PI.y")]
H, STOP = 0.01, 1.0
RESULT_FILE_PATTERNS = ("*.mat", "*_res.*")
"""What OMSimulator writes when its result file isn't switched off (``default_res.mat``)."""


def _events(store_dir: Path, run_id: str) -> list[str]:
    path = store_dir / runstore.EVENTS_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [event["type"] for event in map(json.loads, lines) if event["run_id"] == run_id]


def _result_files(*directories: Path) -> set[Path]:
    return {
        path
        for directory in directories
        for pattern in RESULT_FILE_PATTERNS
        for path in directory.rglob(pattern)
    }


def _failed(status: RunStatus, code: ErrorCode) -> str:
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is code, status.error
    return status.error.message


def _loop(pythonfmu: Pythonfmu) -> dict[str, Path]:
    return {"PI": pythonfmu("PIController"), "P": pythonfmu("Plant")}


def test_closed_loop_matches_the_exact_jacobi_recurrence(
    pythonfmu: Pythonfmu,
    new_system_run: SystemRunBuilder,
    run_worker: RunWorker,
    store_dir: Path,
    workspace: Path,
    worker_tmp: Path,
) -> None:
    parameters = {
        "PI.Kp": (7.0, "system"),
        "P.K": (1.5, "paramset"),
        "P.tau": (0.4, "start_values"),
    }
    outputs = ["PI.u", "P.x", "PI.y", "P.F"]
    cwd_before = _result_files(Path.cwd())
    run_path = new_system_run(
        _loop(pythonfmu), connections=LOOP, parameters=parameters, outputs=outputs
    )
    status = run_worker(run_path)

    assert status.state is RunState.FINISHED, status.error
    assert status.progress == 1.0 and status.t == pytest.approx(STOP)
    result = runstore.read_result(run_path)
    assert result.dtype.names == ("time", *outputs)
    assert len(result) == round(STOP / H) + 1
    assert np.allclose(result["time"], np.arange(len(result)) * H, rtol=0, atol=1e-12)
    pi = {**PI_DEFAULTS, "Kp": 7.0}
    plant = {"K": 1.5, "tau": 0.4}
    expected = jacobi_loop(len(result) - 1, H, pi, plant)
    for name in outputs:
        assert np.max(np.abs(result[name] - expected[name])) < 1e-12, name
    assert np.array_equal(result["PI.y"], result["P.x"])  # post-exchange: input == source
    assert np.array_equal(result["P.F"], result["PI.u"])
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.columns == outputs
    assert [v.unit for v in summary.variables] == [None] * len(outputs)  # pythonfmu: no units
    assert _events(store_dir, status.run_id) == ["started", "finished"]
    assert not (run_path / runstore.WORK_DIR).exists()
    assert list(worker_tmp.iterdir()) == []  # OMSimulator's temp dirs stayed in work/tmp
    assert _result_files(workspace) == set()
    assert _result_files(Path.cwd()) == cwd_before  # the worker's working directory


def test_parameters_change_the_loop(
    pythonfmu: Pythonfmu, new_system_run: SystemRunBuilder, run_worker: RunWorker
) -> None:
    default = new_system_run(_loop(pythonfmu), connections=LOOP)
    assert run_worker(default).state is RunState.FINISHED
    expected = jacobi_loop(round(STOP / H), H, PI_DEFAULTS, PLANT_DEFAULTS)
    result = runstore.read_result(default)
    assert np.max(np.abs(result["P.x"] - expected["P.x"])) < 1e-12
    assert result.dtype.names == ("time", "PI.u", "P.x")  # every output, system-file order


def test_component_fmu_changed_after_the_spec_fails_with_fmu_changed(
    pythonfmu: Pythonfmu,
    new_system_run: SystemRunBuilder,
    run_worker: RunWorker,
    store_dir: Path,
) -> None:
    components = _loop(pythonfmu)
    run_path = new_system_run(components, connections=LOOP)
    with components["P"].open("ab") as archive:  # still a valid zip, but other bytes
        archive.write(b"\0")
    message = _failed(run_worker(run_path), ErrorCode.FMU_CHANGED)
    assert "Plant.fmu of component P changed" in message and "new run" in message
    assert not (run_path / runstore.RESULT_FILE).exists()
    assert not (run_path / runstore.WORK_DIR).exists()
    assert _events(store_dir, run_path.name) == ["started", "failed"]


def test_unreadable_component_fmu_fails_with_fmu_changed(
    pythonfmu: Pythonfmu, new_system_run: SystemRunBuilder, run_worker: RunWorker
) -> None:
    components = _loop(pythonfmu)
    run_path = new_system_run(components, connections=LOOP)
    components["PI"].unlink()
    message = _failed(run_worker(run_path), ErrorCode.FMU_CHANGED)
    assert "component PI can no longer be read" in message


def test_omsimulator_differing_from_the_spec_fails_with_oms_changed(
    pythonfmu: Pythonfmu,
    new_system_run: SystemRunBuilder,
    run_worker: RunWorker,
    worker_tmp: Path,
) -> None:
    installed = oms_pin.installed()
    assert installed is not None
    recorded = OmsVersion(version=installed.version, sha256="0" * 64)
    run_path = new_system_run(_loop(pythonfmu), connections=LOOP, omsimulator=recorded)
    message = _failed(run_worker(run_path), ErrorCode.OMS_CHANGED)
    assert installed.sha256 in message and recorded.sha256 in message
    assert "uv sync --locked" in message and "OMSIMULATOR_PIN" in message
    log = (run_path / runstore.LOG_FILE).read_text()
    assert "info:" not in log  # OMSimulator never loaded: it logs as soon as it runs
    assert not (run_path / runstore.WORK_DIR).exists()
    assert list(worker_tmp.iterdir()) == []


def test_worker_fails_closed_without_importing_omsimulator(
    pythonfmu: Pythonfmu, new_system_run: SystemRunBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process: the pin is checked before anything imports OMSimulator. A changed install
    (simulated) gives ``oms_changed``; an import attempt would fail on the ``None`` entry in
    ``sys.modules`` and give another error."""
    run_path = new_system_run(_loop(pythonfmu), connections=LOOP)
    changed = OmsVersion(version=oms_pin.pinned().version, sha256="f" * 64)
    monkeypatch.setattr(oms_pin, "installed", lambda: changed)
    monkeypatch.setitem(sys.modules, "OMSimulator", None)
    assert worker.main(run_path) == worker.EXIT_OK
    message = _failed(runstore.read_status(run_path), ErrorCode.OMS_CHANGED)
    assert changed.sha256 in message and oms_pin.pinned().sha256 in message
    assert not (run_path / runstore.WORK_DIR).exists()


def test_install_matching_the_spec_but_not_the_pin_fails_with_oms_changed(
    pythonfmu: Pythonfmu, new_system_run: SystemRunBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process: the spec records exactly what is installed, but the worker's code now pins
    another package (a spec written under an old pin, or a forged one). Fail closed."""
    installed = oms_pin.installed()
    assert installed is not None
    run_path = new_system_run(_loop(pythonfmu), connections=LOOP, omsimulator=installed)
    newer = OmsVersion(version="9.9.9", sha256="e" * 64)
    monkeypatch.setattr(oms_pin, "installed", lambda: installed)
    monkeypatch.setattr(oms_pin, "pinned", lambda: newer)
    monkeypatch.setitem(sys.modules, "OMSimulator", None)
    assert worker.main(run_path) == worker.EXIT_OK
    message = _failed(runstore.read_status(run_path), ErrorCode.OMS_CHANGED)
    assert "9.9.9" in message and newer.sha256 in message and installed.sha256 in message
    assert not (run_path / runstore.WORK_DIR).exists()


def test_parameter_omsimulator_refuses_is_a_simulation_error(
    fmu: FmuFactory, new_system_run: SystemRunBuilder, run_worker: RunWorker
) -> None:
    """``der(v)`` is a calculated local: OMSimulator only warns and ignores the set."""
    run_path = new_system_run(
        {"B": fmu("2.0", "BouncingBall")}, parameters={"B.der(v)": (1.0, "start_values")}
    )
    message = _failed(run_worker(run_path), ErrorCode.SIMULATION_ERROR)
    assert "did not set B.der(v) = 1.0" in message and "initial='exact'" in message
    assert "Traceback" not in message
    assert not (run_path / runstore.WORK_DIR).exists()


def test_a_discarded_step_fails_instead_of_hanging(
    pythonfmu: Pythonfmu,
    new_system_run: SystemRunBuilder,
    run_worker: RunWorker,
    worker_tmp: Path,
) -> None:
    run_path = new_system_run({"S": pythonfmu("Stall")}, step_size=0.1, timeout_s=30.0)
    message = _failed(run_worker(run_path), ErrorCode.SIMULATION_ERROR)
    assert "no progress at t=0.6" in message
    assert "discarded" in (run_path / runstore.LOG_FILE).read_text()  # OMSimulator's line
    assert not (run_path / runstore.RESULT_FILE).exists()
    assert list(worker_tmp.iterdir()) == []


def test_non_zero_start_time_is_refused(
    fmu: FmuFactory, new_system_run: SystemRunBuilder, run_worker: RunWorker
) -> None:
    """OMSimulator 3.0.0.post198 starts the clock at 0 whatever the start time."""
    run_path = new_system_run({"B": fmu("2.0", "BouncingBall")}, start_time=0.5)
    message = _failed(run_worker(run_path), ErrorCode.SIMULATION_ERROR)
    assert "start the run at 0" in message


def test_a_stair_2_0_terminate_request_stalls_the_clock_and_fails(
    fmu: FmuFactory, new_system_run: SystemRunBuilder, run_worker: RunWorker
) -> None:
    """FMI 2.0: OMSimulator returns ok from a step that ends with the FMU's terminate request
    and the clock stops (spike (i) item 9), so the no-progress check fails the run. It can't tell
    this from a discard."""
    run_path = new_system_run({"S": fmu("2.0", "Stair")}, step_size=0.2, stop_time=10.0)
    message = _failed(run_worker(run_path), ErrorCode.SIMULATION_ERROR)
    assert "no progress at t=9" in message


def test_a_stair_3_0_terminate_request_is_ignored_until_the_fmu_fails(
    fmu: FmuFactory, new_system_run: SystemRunBuilder, run_worker: RunWorker
) -> None:
    """FMI 3.0: OMSimulator ignores the FMU's terminate request (spike (i) item 9). Stair 3.0
    asks to terminate at t = 9 and the clock keeps advancing. The run fails only when the FMU's
    counter overflows, with the engine's ``Status.error`` (not the no-progress message), and the
    FMU's own line is in the log. If OMSimulator starts honouring the request, this test is the
    first to change."""
    run_path = new_system_run({"S": fmu("3.0", "Stair")}, step_size=0.2, stop_time=10.0)
    status = run_worker(run_path)
    message = _failed(status, ErrorCode.SIMULATION_ERROR)
    assert "Failed to do step: Status.error" in message and "no progress" not in message
    assert status.t == pytest.approx(9.8)  # it stepped past the request at t = 9
    assert status.error is not None and status.error.log_tail is not None
    assert 'Variable "counter" cannot be incremented for values >= 10' in status.error.log_tail
