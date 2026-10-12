"""Round trips with the real OMSimulator (``oms`` marker): a system file exported, imported and
run through the run machinery gives exactly the original run's results, and OMSimulator's own
importer (``SSP(path)``, in a child process) loads our export without schema warnings and
reproduces the worker's run.

Collaborators: ``agentic_fmi.ssp`` (under test), ``agentic_fmi.runs.start_run`` with a real
``Supervisor`` (workers run the OMSimulator engine), ``agentic_fmi.store.runstore`` (results),
the ``pythonfmu`` FMUs and ``supervisor`` of ``tests/ssp/conftest.py``, ``system_support`` (the
exact Jacobi recurrence of the loop) and ``ssp_support.run_oms_child``. OMSimulator never runs
in the test process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from ssp_support import Pythonfmu, run_oms_child, ssv_text
from system_fmus import write_json
from system_support import jacobi_loop

from agentic_fmi import runs
from agentic_fmi.config import Config
from agentic_fmi.ssp import export_ssp, import_ssp
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState, SystemSpec
from agentic_fmi.supervisor import Supervisor

pytestmark = pytest.mark.oms

STOP, H = 1.0, 0.01
PI = {"Kp": 7.0, "Ki": 12.0, "r": 1.0}
PLANT = {"K": 1.5, "tau": 0.4}


def write_loop(workspace: Path, pythonfmu: Pythonfmu) -> None:
    """``loop.json`` (PI.Kp and P.tau in the file) and ``p.ssv`` (PI.Ki and P.K)."""
    pythonfmu("PIController")
    pythonfmu("Plant")
    write_json(
        workspace / "loop.json",
        {
            "version": 1,
            "components": {
                "PI": {"fmu": "fmus/PIController.fmu", "parameters": {"Kp": 7}},
                "P": {"fmu": "fmus/Plant.fmu", "parameters": {"tau": 0.4}},
            },
            "connections": [{"from": "PI.u", "to": "P.F"}, {"from": "P.x", "to": "PI.y"}],
            "experiment": {"stop_time": STOP, "step_size": H},
        },
    )
    (workspace / "p.ssv").write_text(
        ssv_text({"PI.Ki": ("Real", "12"), "P.K": ("Real", "1.5")}), encoding="utf-8"
    )


def run(config: Config, supervisor: Supervisor, **arguments: Any) -> tuple[np.ndarray, SystemSpec]:
    """Start a system run, wait for it, and return its result and spec."""
    view = runs.start_run(config, supervisor, None, wait_s=60, **arguments)
    assert view.state is RunState.FINISHED, view
    run_path = runstore.run_dir(config.store_dir, view.run_id)
    spec = runstore.read_job_spec(run_path)
    assert isinstance(spec, SystemSpec)
    return runstore.read_result(run_path), spec


def test_export_import_and_run_gives_the_original_results(
    config: Config, supervisor: Supervisor, pythonfmu: Pythonfmu, workspace: Path
) -> None:
    write_loop(workspace, pythonfmu)
    original, original_spec = run(config, supervisor, system="loop.json", paramset="p.ssv")
    expected = jacobi_loop(round(STOP / H), H, PI, PLANT)
    for name in ("PI.u", "P.x"):
        assert np.max(np.abs(original[name] - expected[name])) < 1e-12, name

    export_ssp(config, "loop.json", "exports/loop.ssp", paramset="p.ssv")
    imported = import_ssp(config, "exports/loop.ssp")
    again, again_spec = run(config, supervisor, system=imported.system)

    assert imported.system == "imported/loop/loop.json"
    assert original.dtype.names == again.dtype.names == ("time", "PI.u", "P.x")
    for name in original.dtype.names:
        assert np.array_equal(original[name], again[name]), name
    values = {ref: v.value for ref, v in original_spec.parameters.items()}
    assert values == {"P.K": 1.5, "P.tau": 0.4, "PI.Ki": 12.0, "PI.Kp": 7.0}
    assert {ref: v.value for ref, v in again_spec.parameters.items()} == values
    assert {v.source for v in again_spec.parameters.values()} == {"system"}
    shas = {name: c.fmu.sha256 for name, c in original_spec.components.items()}
    assert {name: c.fmu.sha256 for name, c in again_spec.components.items()} == shas


def test_omsimulator_loads_the_export_and_matches_the_worker(
    config: Config,
    supervisor: Supervisor,
    pythonfmu: Pythonfmu,
    workspace: Path,
    tmp_path: Path,
) -> None:
    write_loop(workspace, pythonfmu)
    original, _ = run(config, supervisor, system="loop.json", paramset="p.ssv")
    export_ssp(config, "loop.json", "exports/loop.ssp", paramset="p.ssv")

    report = run_oms_child(
        workspace / "exports" / "loop.ssp", "loop", STOP, H, ["PI.u", "P.x"], tmp_path / "child"
    )

    assert report["warnings"] == []  # OMSimulator validates SSD and SSV against its XSDs
    rows = np.asarray(report["rows"], dtype=float)
    assert rows.shape == (len(original), 3)
    for column, name in enumerate(original.dtype.names or ()):
        assert np.max(np.abs(rows[:, column] - original[name])) < 1e-12, name
