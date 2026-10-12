"""End-to-end tests of ``afmi worker``: real worker processes on queued run directories.

Collaborators: ``agentic_fmi.worker`` (under test, through ``python -m agentic_fmi worker``),
the fixtures in ``tests/worker/conftest.py`` (``new_run``, ``spawn``, ``run_worker``) and
``agentic_fmi.store.runstore`` for reading what the worker left behind. Timeouts, signals and
heartbeats are in ``test_worker_supervision.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState, RunStatus, Step

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
RunWorker = Callable[[Path], RunStatus]

VARIANTS = [
    ("2.0", "CoSimulation"),
    ("2.0", "ModelExchange"),
    ("3.0", "CoSimulation"),
    ("3.0", "ModelExchange"),
]


def _events(store_dir: Path, run_id: str) -> list[dict[str, object]]:
    path = store_dir / runstore.EVENTS_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [event for event in map(json.loads, lines) if event["run_id"] == run_id]


def _first_apex(h: np.ndarray) -> float:
    peaks = [i for i in range(1, len(h) - 1) if h[i] > h[i - 1] and h[i] >= h[i + 1]]
    assert peaks, "no bounce apex in result.csv"
    return float(h[peaks[0]])


@pytest.mark.parametrize(("version", "fmi_type"), VARIANTS)
def test_bouncing_ball_finishes_with_result_and_summary(
    fmu: FmuFactory,
    new_run: RunBuilder,
    run_worker: RunWorker,
    store_dir: Path,
    version: str,
    fmi_type: str,
) -> None:
    run_path = new_run(fmu(version, "BouncingBall"), fmi_type, outputs=["v", "h"])
    status = run_worker(run_path)

    assert status.state is RunState.FINISHED
    assert status.error is None
    assert (status.t, status.progress) == (3.0, 1.0)
    assert status.pid is not None and status.pid_start_time is not None
    assert status.started_at and status.heartbeat_at and status.finished_at
    assert status.started_at <= status.finished_at
    result = runstore.read_result(run_path)
    assert result.dtype.names == ("time", "v", "h")
    assert result["time"][-1] == pytest.approx(3.0)
    summary = runstore.read_summary(run_path)
    assert summary is not None
    assert summary.state is RunState.FINISHED
    assert (summary.n_points, summary.t_end, summary.columns) == (len(result), 3.0, ["v", "h"])
    assert [(v.name, v.unit) for v in summary.variables] == [("v", "m/s"), ("h", "m")]
    assert summary.variables[1].max == 1.0
    assert not (run_path / runstore.FMU_DIR).exists()
    events = _events(store_dir, status.run_id)
    assert [(e["type"], e["source"]) for e in events] == [
        ("started", "worker"),
        ("finished", "worker"),
    ]


def test_restitution_start_value_changes_apex_height(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker
) -> None:
    path = fmu("3.0", "BouncingBall")
    default, softer = new_run(path), new_run(path, start_values={"e": 0.5})
    assert run_worker(default).state is RunState.FINISHED
    assert run_worker(softer).state is RunState.FINISHED
    # Dropped from h = 1, the first apex is e**2: 0.49 with the default e = 0.7, 0.25 with 0.5.
    assert _first_apex(runstore.read_result(default)["h"]) == pytest.approx(0.49, abs=0.02)
    assert _first_apex(runstore.read_result(softer)["h"]) == pytest.approx(0.25, abs=0.02)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_step_input_drives_feedthrough_output(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker, version: str
) -> None:
    run_path = new_run(
        fmu(version, "Feedthrough"),
        outputs=["Float64_continuous_output"],
        inputs={"Float64_continuous_input": Step(time=1.0, after=3.0)},
    )
    assert run_worker(run_path).state is RunState.FINISHED
    result = runstore.read_result(run_path)
    time, output = result["time"], result["Float64_continuous_output"]
    assert np.all(output[time < 0.99] == 0.0)
    assert np.all(output[time > 1.01] == 3.0)


def test_fmu_changed_after_spec_fails_with_fmu_changed(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker, store_dir: Path
) -> None:
    path = fmu("3.0", "BouncingBall")
    run_path = new_run(path)
    with path.open("ab") as archive:  # still a valid zip, but other bytes
        archive.write(b"\0")
    status = run_worker(run_path)
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.FMU_CHANGED
    assert "changed" in status.error.message and "new run" in status.error.message
    assert status.finished_at is not None
    assert not (run_path / runstore.RESULT_FILE).exists()
    assert not (run_path / runstore.FMU_DIR).exists()
    assert [e["type"] for e in _events(store_dir, status.run_id)] == ["started", "failed"]


def test_simulation_error_is_recorded_without_a_traceback(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"), start_values={"no_such_variable": 1.0})
    status = run_worker(run_path)
    assert status.state is RunState.FAILED
    error = status.error
    assert error is not None and error.code is ErrorCode.SIMULATION_ERROR
    assert "no_such_variable" in error.message
    assert len(error.message) <= 500
    assert "Traceback" not in error.message
    assert "Traceback" not in (error.log_tail or "")
    assert "Traceback" in (run_path / runstore.LOG_FILE).read_text()  # full detail in log.txt
    assert not (run_path / runstore.FMU_DIR).exists()


def test_worker_exits_without_writing_when_the_run_is_not_queued(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker, store_dir: Path
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    queued = runstore.read_status(run_path)
    taken_over = queued.model_copy(update={"state": RunState.FAILED})
    runstore.write_status(run_path, taken_over)
    before = (run_path / runstore.STATUS_FILE).read_bytes()
    assert run_worker(run_path) == taken_over
    assert (run_path / runstore.STATUS_FILE).read_bytes() == before
    assert _events(store_dir, queued.run_id) == []


def test_worker_leaves_no_temp_dirs(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker, worker_tmp: Path
) -> None:
    path = fmu("3.0", "BouncingBall")
    assert run_worker(new_run(path, "ModelExchange")).state is RunState.FINISHED
    assert run_worker(new_run(path, start_values={"nope": 1.0})).state is RunState.FAILED
    assert list(worker_tmp.iterdir()) == []  # the workers' TMPDIR, private to this test
