"""Fixtures for the engine tests: extracted Reference FMU copies and ``RunSpec`` builders.

Collaborators: the shared ``fmu`` fixture (``tests/conftest.py``) provides private FMU copies;
``agentic_fmi.fmu.inspect`` resolves the experiment exactly as ``run_start`` will. The engine
loads FMU binaries, so these tests run it in the test process on extracted copies only.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.fmu.inspect import read_description, resolve_experiment, sha256_file
from agentic_fmi.store.protocol import FmuRef, InputSignal, RunSpec, StartValue, Versions, utc_now

SpecBuilder = Callable[..., RunSpec]


@pytest.fixture
def extracted(tmp_path: Path) -> Callable[[Path], Path]:
    """``extracted(fmu_path)`` unpacks the whole archive once into ``tmp_path`` and returns the
    directory, as the worker's ``fmu/`` would hold it."""

    def extract(fmu_path: Path) -> Path:
        target = tmp_path / "extracted" / fmu_path.parent.name / fmu_path.stem
        if not target.exists():
            with zipfile.ZipFile(fmu_path) as archive:
                archive.extractall(target)
        return target

    return extract


@pytest.fixture
def engine_spec() -> SpecBuilder:
    """``engine_spec(fmu_path, fmi_type, outputs=..., start_values=..., inputs=..., solver=...,
    **experiment)`` builds the ``RunSpec`` the server would write (outputs default to the FMU's
    outputs; ``experiment`` overrides go through ``resolve_experiment``)."""

    def build(
        fmu_path: Path,
        fmi_type: str = "CoSimulation",
        *,
        outputs: list[str] | None = None,
        start_values: Mapping[str, StartValue] | None = None,
        inputs: Mapping[str, InputSignal] | None = None,
        solver: str = "CVode",
        **experiment: Any,
    ) -> RunSpec:
        md = read_description(fmu_path)
        return RunSpec(
            run_id="r1",
            created_at=utc_now(),
            fmu=FmuRef(
                path=str(fmu_path),
                sha256=sha256_file(fmu_path),
                size_bytes=fmu_path.stat().st_size,
                fmi_version=md.fmiVersion,
                model_name=md.modelName,
            ),
            fmi_type=fmi_type,
            solver=solver,
            experiment=resolve_experiment(md, fmi_type, **experiment),
            start_values=dict(start_values or {}),
            inputs=dict(inputs or {}),
            outputs=outputs
            if outputs is not None
            else [v.name for v in md.modelVariables if v.causality == "output"],
            timeout_s=60.0,
            versions=Versions.current(),
        )

    return build
