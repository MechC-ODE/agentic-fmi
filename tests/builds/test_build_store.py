"""Run-store support for build jobs: ``spec.json`` dispatch on ``kind``, the index entry of a
build job (also after an index rebuild), and ``build.json`` reads and writes.

Collaborators: ``agentic_fmi.store.runstore`` (under test), ``agentic_fmi.build`` (a real
``BuildSpec`` from ``check_request`` and ``build_spec``), ``build_support.fake_record`` and the
``config`` and ``write_source`` fixtures. No process is started.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from build_support import fake_record

from agentic_fmi import build
from agentic_fmi.config import Config
from agentic_fmi.errors import ProtocolError
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import BuildSpec, RunState, RunStatus, utc_now

WriteSource = Callable[..., Path]


def _build_job(config: Config, write_source: WriteSource) -> Path:
    """A queued build job's run directory, as ``start_build`` writes it (without spawning)."""
    write_source("models/afmi_gain.py")
    request = build.check_request(config, "models/afmi_gain.py")
    run_id, run_path = runstore.claim_run_id(config.store_dir)
    runstore.write_build_source(run_path, request.source.name, request.data)
    runstore.write_spec(run_path, build.build_spec(config, request, run_id))
    status = RunStatus(
        run_id=run_id, state=RunState.QUEUED, t_start=0.0, t_stop=0.0, updated_at=utc_now()
    )
    runstore.write_status(run_path, status)
    return run_path


def test_a_build_spec_is_read_by_kind_and_indexed_as_a_build(
    config: Config, write_source: WriteSource
) -> None:
    run_path = _build_job(config, write_source)
    spec = runstore.read_job_spec(run_path)
    assert isinstance(spec, BuildSpec)
    assert (run_path / "source" / "afmi_gain.py").read_bytes() == (
        config.workspace / "models" / "afmi_gain.py"
    ).read_bytes()
    with pytest.raises(ProtocolError):
        runstore.read_spec(run_path)  # the simulation reader refuses a build spec
    entry = runstore.index_entry(spec, runstore.read_status(run_path))
    assert entry.kind == "build"
    assert entry.fmu_name == "afmi_gain"
    assert entry.fmu_path == str(config.workspace / "generated" / "afmi_gain" / "afmi_gain.fmu")
    (config.store_dir / runstore.INDEX_FILE).unlink(missing_ok=True)
    assert runstore.read_index(config.store_dir).runs == [entry]  # rebuilt from the spec


def test_an_unknown_kind_is_a_protocol_error(config: Config, write_source: WriteSource) -> None:
    run_path = _build_job(config, write_source)
    path = run_path / runstore.SPEC_FILE
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**document, "kind": "study"}), encoding="utf-8")
    with pytest.raises(ProtocolError, match="unknown kind 'study'"):
        runstore.read_job_spec(run_path)


def test_build_records_round_trip_and_damage_is_a_protocol_error(tmp_path: Path) -> None:
    assert runstore.read_build_record(tmp_path) is None
    record = fake_record("Gain")
    runstore.write_build_record(tmp_path, record)
    assert runstore.read_build_record(tmp_path) == record
    (tmp_path / runstore.BUILD_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ProtocolError):
        runstore.read_build_record(tmp_path)
