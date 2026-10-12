"""The committed JSON Schemas in ``docs/schemas/`` match the protocol models and the system file
model, the build documents added in Phase 2 (ADR 0008) round-trip, and the system run spec
(ADR 0009) keeps its ``{"from", "to"}`` connections through a write and read.

Collaborators: ``agentic_fmi.store.protocol`` (``DOCUMENTS``, ``export_schemas``, the build and
system models), ``agentic_fmi.system`` (``export_system_file_schema``) and
``agentic_fmi.store.atomic`` (``write_json``, ``read_json``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_fmi.store.atomic import read_json, write_json
from agentic_fmi.store.protocol import (
    DOCUMENTS,
    BuildEvent,
    BuildRecord,
    BuildSpec,
    BuildSummary,
    BuildVersions,
    BuiltFmu,
    ComponentSpec,
    Experiment,
    FileRef,
    FmuRef,
    MasterAlgorithm,
    OmsVersion,
    ResolvedValue,
    SourceRef,
    SystemConnection,
    SystemSpec,
    Versions,
    export_schemas,
)
from agentic_fmi.system import SYSTEM_SCHEMA_FILE, export_system_file_schema

SCHEMAS = Path(__file__).resolve().parents[2] / "docs" / "schemas"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
SHA = "a" * 64


def test_committed_schemas_match_the_models(tmp_path: Path) -> None:
    written = [*export_schemas(tmp_path), export_system_file_schema(tmp_path)]
    expected = {f"{stem}.schema.json" for stem in DOCUMENTS} | {SYSTEM_SCHEMA_FILE}
    assert {path.name for path in written} == expected
    committed = {path.name for path in SCHEMAS.glob("*.schema.json")}
    assert committed == {path.name for path in written}, "regenerate docs/schemas"
    for path in written:
        assert (SCHEMAS / path.name).read_bytes() == path.read_bytes(), (
            f"docs/schemas/{path.name} is stale; regenerate it (export_schemas, "
            "export_system_file_schema)"
        )


def test_system_spec_round_trips_with_from_and_to(tmp_path: Path) -> None:
    fmu = FmuRef(
        path="/lab/ws/generated/PI/PI.fmu",
        sha256=SHA,
        size_bytes=660_000,
        fmi_version="2.0",
        model_name="PIController",
    )
    spec = SystemSpec(
        run_id="r4",
        created_at=NOW,
        name="loop",
        system=FileRef(path="/lab/ws/loop.json", sha256=SHA, size_bytes=400),
        components={"PI": ComponentSpec(fmu=fmu), "MSD": ComponentSpec(fmu=fmu)},
        connections=[SystemConnection.model_validate({"from": "PI.u", "to": "MSD.F"})],
        parameters={"PI.Kp": ResolvedValue(value=12.0, source="start_values")},
        master=MasterAlgorithm(step_size=0.01),
        experiment=Experiment(start_time=0.0, stop_time=10.0, output_interval=0.01),
        outputs=["MSD.x", "PI.u"],
        timeout_s=600.0,
        versions=Versions.current(),
        omsimulator=OmsVersion(version="3.0.0.post198", sha256=SHA),
    )
    path = tmp_path / "spec.json"
    write_json(path, spec)
    assert '"from":"PI.u"' in path.read_text().replace(" ", "")
    assert read_json(path, SystemSpec) == spec
    with pytest.raises(ValidationError):  # one result row per communication step
        SystemSpec.model_validate({**spec.model_dump(), "master": {"step_size": 0.02}})


def _source() -> SourceRef:
    return SourceRef(path="models/MSD.mo", sha256=SHA, size_bytes=420)


def test_build_spec_round_trips(tmp_path: Path) -> None:
    spec = BuildSpec(
        run_id="r3",
        created_at=NOW,
        workspace="/lab/ws",
        source=_source(),
        name="MSD",
        fmu_path="generated/MSD/MSD.fmu",
        backend="omc",
        options={"fmi_version": "2.0", "fmu_type": "me_cs", "fmi_flags": "s:cvode"},
        timeout_s=1800.0,
        versions=Versions.current(),
    )
    path = tmp_path / "spec.json"
    write_json(path, spec)
    assert read_json(path, BuildSpec) == spec
    assert spec.kind == "build" and spec.overwrite is False


def test_build_record_round_trips(tmp_path: Path) -> None:
    record = BuildRecord(
        run_id="r3",
        name="MSD",
        built_at=NOW,
        backend="omc",
        source=_source(),
        options={"fmi_version": "2.0", "fmu_type": "me_cs", "fmi_flags": "s:cvode"},
        versions=BuildVersions(
            agentic_fmi="0.1.0", python="3.12.13", platform="x86_64-linux", omc="1.27.1"
        ),
        fmu=BuiltFmu(
            path="generated/MSD/MSD.fmu",
            sha256=SHA,
            size_bytes=1_320_000,
            fmi_version="2.0",
            model_name="MSD",
            model_identifier="MSD",
            interfaces=["CoSimulation", "ModelExchange"],
        ),
        duration_s=6.4,
        summary=BuildSummary(
            counts={"input": 1, "output": 2, "parameter": 3, "total": 6},
            inputs=["F"],
            outputs=["x", "v"],
            parameters=["m", "k", "d"],
        ),
    )
    path = tmp_path / "build.json"
    write_json(path, record)
    assert read_json(path, BuildRecord) == record


def test_build_event_hash_and_build_names_are_validated() -> None:
    with pytest.raises(ValidationError):
        BuildEvent(at=NOW, run_id="r3", fmu_path="generated/x/x.fmu", fmu_sha256="nothex")
    event = BuildEvent(at=NOW, run_id="r3", fmu_path="generated/MSD/MSD.fmu", fmu_sha256=SHA)
    assert event.type == "fmu_built" and event.source == "worker"
    for bad in ("1abc", "_x", "a-b", "a" * 65, ""):
        with pytest.raises(ValidationError):
            BuildSpec.model_validate(
                {
                    "run_id": "r1",
                    "created_at": NOW,
                    "workspace": "/ws",
                    "source": _source().model_dump(),
                    "name": bad,
                    "fmu_path": f"generated/{bad}/{bad}.fmu",
                    "backend": "pythonfmu",
                    "options": {},
                    "timeout_s": 1.0,
                    "versions": Versions.current().model_dump(),
                }
            )
