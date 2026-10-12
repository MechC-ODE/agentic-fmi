"""Fixtures for the system tests: a ``Config`` for the temporary workspace, synthetic FMUs and
system files, an OMSimulator stand-in for the pin check, and fabricated system runs.

Collaborators: ``tests/conftest.py`` (``workspace``), ``tests/system_fmus.py`` (the synthetic
FMUs: zips with a ``modelDescription.xml`` and a placeholder where a binary would be; nothing
here runs one), ``agentic_fmi.oms_pin`` (monkeypatched, so no OMSimulator install is needed),
``agentic_fmi.supervisor`` (``spawn`` is faked; no worker starts) and ``agentic_fmi.store``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from system_fmus import (
    AddSystemRun,
    WriteSystem,
    loop_system,
    system_spec,
    write_json,
    write_loop_fmus,
)

from agentic_fmi import oms_pin
from agentic_fmi.analysis.summary import summarize
from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    OmsVersion,
    RunError,
    RunState,
    RunStatus,
    RunSummary,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor

OMS = OmsVersion(version="3.0.0.post198", sha256="b" * 64)
"""The OMSimulator identity the fake install reports."""


@pytest.fixture
def config(workspace: Path) -> Config:
    return replace(
        load_config(workspace), timing=Timing(heartbeat_stale_s=4.0, poll_interval_s=0.02)
    )


@pytest.fixture
def supervisor(config: Config) -> Supervisor:
    return Supervisor(config)


@pytest.fixture
def fmus(workspace: Path) -> dict[str, Path]:
    """``fmus/pi.fmu`` (controller) and ``fmus/plant.fmu`` in the workspace."""
    return write_loop_fmus(workspace)


@pytest.fixture
def write_system(workspace: Path) -> WriteSystem:
    """``write_system(document=None, name="loop.json")`` writes the closed loop (or
    ``document``) into the workspace and returns the relative path."""

    def write(document: Mapping[str, Any] | None = None, name: str = "loop.json") -> str:
        write_json(workspace / name, loop_system() if document is None else dict(document))
        return name

    return write


@pytest.fixture
def oms(monkeypatch: pytest.MonkeyPatch) -> OmsVersion:
    """A fake OMSimulator install that matches the pin; returns the identity it reports."""
    monkeypatch.setattr(oms_pin, "installed", lambda: OMS)
    monkeypatch.setattr(oms_pin, "pin_problem", lambda found, expected: None)
    return OMS


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace ``Supervisor.spawn``: no worker starts; the run directories it was asked to
    start are recorded."""
    started: list[Path] = []

    def fake_spawn(self: Supervisor, run_dir: Path) -> int:
        started.append(Path(run_dir))
        return 12345

    monkeypatch.setattr(Supervisor, "spawn", fake_spawn)
    return started


@pytest.fixture
def add_system_run(config: Config) -> AddSystemRun:
    """``add_system_run(parameters={"PI.Kp": 10.0}, ...)`` fabricates a finished system run in
    the store (spec, status, summary, ``result.csv``, index entry) with chosen result columns."""

    def build(
        parameters: Mapping[str, float] | None = None,
        *,
        columns: Mapping[str, Any] | None = None,
        state: RunState = RunState.FINISHED,
        rows: int = 101,
        t_end: float = 1.0,
        step: float = 0.01,
        sha: str = "a",
        system_sha: str = "c",
        name: str = "loop",
        error: RunError | None = None,
    ) -> str:
        run_id, path = runstore.claim_run_id(config.store_dir)
        values = columns if columns is not None else {"MSD.x": lambda t: 1.0 - np.exp(-t)}
        time = np.linspace(0.0, t_end, rows)
        table = {
            n: v(time) if callable(v) else np.asarray(v, dtype=float) for n, v in values.items()
        }
        spec = system_spec(
            run_id,
            parameters,
            root=str(config.workspace),
            name=name,
            step=step,
            stop_time=t_end,
            fmu_sha=sha,
            system_sha=system_sha,
        ).model_copy(update={"outputs": list(table)})
        runstore.write_spec(path, spec)
        status = RunStatus(
            run_id=run_id,
            state=state,
            t=t_end,
            t_start=0.0,
            t_stop=t_end,
            progress=1.0,
            error=error,
            updated_at=utc_now(),
        )
        runstore.write_status(path, status)
        array = np.empty(rows, dtype=[("time", float), *((n, float) for n in table)])
        array["time"] = time
        for column, data in table.items():
            array[column] = data
        runstore.write_result(path, array)
        runstore.write_summary(
            path,
            RunSummary(
                run_id=run_id,
                state=state,
                t_end=t_end,
                n_points=rows,
                wall_time_s=0.1,
                columns=list(table),
                variables=summarize(array, {n: "m" for n in table}),
            ),
        )
        runstore.upsert_index(config.store_dir, runstore.index_entry(spec, status))
        return run_id

    return build
