"""``fmu_build`` refusals: every bad request is refused by ``runs.start_build`` before a job is
created and before any process is started, with a message that names the fix.

Collaborators: ``agentic_fmi.runs.start_build`` and ``agentic_fmi.build`` (under test), the
``config``, ``supervisor`` and ``write_source`` fixtures (``tests/builds/conftest.py``),
``build_support.plant_build``, and ``agentic_fmi.store.runstore`` (no run directory may appear).
``subprocess.Popen`` is replaced by a function that fails the test, so a refusal that started
the worker or a build tool can't pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from build_support import plant_build

from agentic_fmi import build, runs
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.supervisor import Supervisor

REPO_ROOT = Path(__file__).resolve().parents[2]
WriteSource = Callable[..., Path]


@pytest.fixture(autouse=True)
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that starts a process (the worker or a build tool)."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail(f"a process was started: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _refused(config: Config, supervisor: Supervisor, source: str, **kwargs: Any) -> str:
    """The refusal text of ``start_build``; fails if a job was created."""
    with pytest.raises(UserError) as caught:
        runs.start_build(config, supervisor, source, **kwargs)
    assert runstore.list_run_ids(config.store_dir) == []
    return str(caught.value)


def test_only_py_and_mo_sources_are_built(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source("models/afmi_gain.txt")
    message = _refused(config, supervisor, "models/afmi_gain.txt")
    assert "not a .py or .mo file" in message


def test_an_outside_source_is_refused_alike_whether_or_not_it_exists(
    config: Config, supervisor: Supervisor, tmp_path: Path
) -> None:
    outside = tmp_path / "outside" / "afmi_gain.py"
    outside.parent.mkdir()
    outside.write_text("Fmi2Slave", encoding="utf-8")
    existing = _refused(config, supervisor, str(outside))
    outside.unlink()
    missing = _refused(config, supervisor, str(outside))
    assert existing == missing
    assert "outside the workspace" in existing
    relative = _refused(config, supervisor, "../outside/afmi_gain.py")
    assert "outside the workspace" in relative


def test_a_symlink_out_of_the_workspace_is_resolved_and_refused(
    config: Config, supervisor: Supervisor, workspace: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere" / "afmi_gain.py"
    target.parent.mkdir()
    target.write_text("Fmi2Slave", encoding="utf-8")
    link = workspace / "models" / "afmi_link.py"
    link.parent.mkdir()
    link.symlink_to(target)
    assert "outside the workspace" in _refused(config, supervisor, "models/afmi_link.py")


def test_sources_in_the_store_or_in_generated_are_refused(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source(".agentic-fmi/afmi_gain.py")
    write_source("generated/old/afmi_gain.py")
    assert "inside the run store" in _refused(config, supervisor, ".agentic-fmi/afmi_gain.py")
    assert "inside generated/" in _refused(config, supervisor, "generated/old/afmi_gain.py")


def test_missing_empty_directory_and_oversized_sources_are_refused(
    config: Config, supervisor: Supervisor, workspace: Path, write_source: WriteSource
) -> None:
    assert "is empty" in _refused(config, supervisor, " ")
    assert "was not found" in _refused(config, supervisor, "models/afmi_none.py")
    (workspace / "models" / "afmi_dir.py").mkdir(parents=True)
    assert "is a directory" in _refused(config, supervisor, "models/afmi_dir.py")
    limit = config.limits.build_source_max_bytes
    write_source("models/afmi_big.py", "# Fmi2Slave\n" + "#" * limit)
    assert "larger than 256 KiB" in _refused(config, supervisor, "models/afmi_big.py")
    (workspace / "models" / "afmi_latin.py").write_bytes(b"# Fmi2Slave caf\xe9\n")
    assert "not UTF-8" in _refused(config, supervisor, "models/afmi_latin.py")


def test_a_python_file_without_a_slave_is_never_run(
    config: Config, supervisor: Supervisor, workspace: Path
) -> None:
    script = workspace / "scripts" / "fetch-reference-fmus.py"
    script.parent.mkdir()
    shutil.copyfile(REPO_ROOT / "scripts" / "fetch-reference-fmus.py", script)
    message = _refused(config, supervisor, "scripts/fetch-reference-fmus.py")
    assert "defines no pythonfmu slave" in message
    assert "Fmi2Slave" in message


@pytest.mark.parametrize(
    ("stem", "reason"),
    [
        ("1gain", "not a Python module name"),
        ("gain-model", "not a Python module name"),
        ("class", "keyword"),
        ("json", "standard-library module"),
        ("pythonfmu", "build tool's own package"),
        ("numpy", "installed module"),
    ],
)
def test_module_names_python_would_not_import_from_the_file_are_refused(
    config: Config, supervisor: Supervisor, write_source: WriteSource, stem: str, reason: str
) -> None:
    write_source(f"models/{stem}.py")
    message = _refused(config, supervisor, f"models/{stem}.py", name="Gain")
    assert reason in message
    assert "Rename the file" in message


@pytest.mark.parametrize("name", ["1gain", "gain-model", "_gain", "g" * 65, "gain\n"])
def test_invalid_build_names_are_refused(
    config: Config, supervisor: Supervisor, write_source: WriteSource, name: str
) -> None:
    write_source("models/afmi_gain.py")
    assert "is invalid" in _refused(config, supervisor, "models/afmi_gain.py", name=name)


@pytest.mark.parametrize("name", ["CON", "nul", "Com1", "LPT9", "aux"])
def test_windows_reserved_names_are_refused(
    config: Config, supervisor: Supervisor, write_source: WriteSource, name: str
) -> None:
    write_source("models/afmi_gain.py")
    message = _refused(config, supervisor, "models/afmi_gain.py", name=name)
    assert "reserved on Windows" in message


def test_a_name_differing_only_by_case_from_an_output_is_refused(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source("models/afmi_gain.py")
    plant_build(config, "Gain")
    message = _refused(config, supervisor, "models/afmi_gain.py", name="gain", overwrite=True)
    assert "only by case" in message


def test_an_existing_build_needs_overwrite_and_a_foreign_directory_is_never_replaced(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source("models/afmi_gain.py")
    plant_build(config, "afmi_gain", sha256="e" * 64)
    message = _refused(config, supervisor, "models/afmi_gain.py")
    assert "overwrite=true" in message
    assert "models/old.py" in message and "eeeeeeeeeeee" in message
    foreign = config.generated_dir / "Foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine", encoding="utf-8")
    message = _refused(config, supervisor, "models/afmi_gain.py", name="Foreign", overwrite=True)
    assert "not made by fmu_build" in message
    assert os.listdir(foreign) == ["notes.txt"]


def test_a_name_another_job_is_building_is_refused(config: Config) -> None:
    with pytest.raises(UserError, match="already queued or running"):
        build.check_name(config, "gain", overwrite=False, building=["Gain"])
    build.check_name(config, "gain", overwrite=False, building=["Other"])


def test_a_valid_request_reads_and_hashes_the_source_once_without_a_process(
    config: Config, write_source: WriteSource
) -> None:
    path = write_source("models/afmi_gain.py")
    request = build.check_request(config, "models/afmi_gain.py")
    assert request.source == path
    assert request.source_rel == "models/afmi_gain.py"
    assert request.data == path.read_bytes()
    assert request.backend == "pythonfmu"
    assert request.name == "afmi_gain"
    spec = build.build_spec(config, request, "r1")
    assert spec.fmu_path == "generated/afmi_gain/afmi_gain.fmu"
    assert spec.options == {"fmi_version": "2.0", "fmu_type": "cs"}
    assert spec.timeout_s == config.timing.build_timeout_s
    assert spec.source.sha256 == request.sha256
