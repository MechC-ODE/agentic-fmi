"""``fmu_build`` of Modelica sources with the real ``omc``: a first build and its provenance,
the diagnostics of failed builds, a fresh ``HOME`` that must never download anything, and a stop
that must leave no compiler process behind.

Collaborators: ``agentic_fmi.runs`` (``start_build``, ``get_run``, ``stop_run``),
``agentic_fmi.supervisor.Supervisor`` (spawns the workers, which start ``omc``),
``agentic_fmi.store.runstore`` (documents) and the fixtures in ``tests/builds/conftest.py``. Every
test is marked ``omc`` and skipped without ``omc`` on ``PATH``. A build takes 6 to 10 s warm
(about 20 s on one core): omc generates C, then cmake, make and clang compile it.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from build_support import processes_in, wait_until

from agentic_fmi import budget, runs
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState
from agentic_fmi.supervisor import Supervisor

pytestmark = [
    pytest.mark.omc,
    pytest.mark.skipif(shutil.which("omc") is None, reason="omc (OpenModelica) is not installed"),
]

WriteSource = Callable[..., Path]

TINY = """model Tiny "First-order lag with a gain"
  parameter Real k = 2 "gain";
  input Real u = 1 "input";
  output Real y "state";
equation
  der(y) = k*(u - y);
  annotation(experiment(StartTime=0, StopTime=2));
end Tiny;
"""
"""Builds, with omc's warning that the initial conditions are not fully specified (``y`` has
no ``fixed`` start value)."""

SYNTAX = """model Bad
  Real x(start=0, fixed=true);
equation
  der(x) = -x
end Bad;
"""
UNDECLARED = """model Bad
  Real x(start=0, fixed=true);
equation
  der(x) = -y;
end Bad;
"""
UNDER_DETERMINED = """model Bad
  Real x(start=0, fixed=true);
  Real z;
equation
  der(x) = -x;
end Bad;
"""

TOOL = "Full log: .agentic-fmi/runs/{run_id}/build.log"


def _list(directory: Path) -> list[str]:
    return sorted(os.listdir(directory))


def test_a_tiny_model_builds_with_cvode_and_nothing_leaks_into_the_workspace(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    source = write_source("models/Tiny.mo", TINY)
    before = (source.read_bytes(), _list(source.parent))
    view = runs.start_build(config, supervisor, "models/Tiny.mo", wait_s=60)

    assert view.state is RunState.FINISHED, view
    assert view.kind == "build" and view.fmu == "generated/Tiny/Tiny.fmu"
    output = config.generated_dir / "Tiny"
    assert _list(output) == ["Tiny.fmu", "build.json"]
    record = runstore.read_build_record(output)
    assert record is not None and record.backend == "omc"
    assert record.fmu.interfaces == ["CoSimulation", "ModelExchange"]
    assert record.fmu.model_identifier == "Tiny" and record.fmu.fmi_version == "2.0"
    assert record.options == {"fmi_version": "2.0", "fmu_type": "me_cs", "fmi_flags": "s:cvode"}
    assert record.versions.omc is not None and record.versions.omc.startswith("1.27")
    assert record.versions.msl == "4.1.0"
    assert (record.summary.inputs, record.summary.outputs) == (["u"], ["y"])
    assert record.summary.parameters == ["k"]
    assert record.summary.default_experiment is not None
    assert record.summary.default_experiment.stop_time == 2.0
    # omc's "initial conditions are not fully specified" is a warning of a successful build.
    assert len(record.warnings) == 1
    assert record.warnings[0].startswith("warning: The initial conditions are not fully specified")
    assert view.build is not None and view.build["warnings"] == record.warnings
    assert budget.json_size(view) <= config.limits.build_result_max_bytes
    # CVode is built in (without it the CS interface integrates with forward Euler).
    with zipfile.ZipFile(output / "Tiny.fmu") as archive:
        names = set(archive.namelist())
        assert json.loads(archive.read("resources/Tiny_flags.json")) == {"s": "cvode"}
    assert "binaries/linux64/libsundials_cvode.so.5" in names
    # The source is untouched, omc's side files never reached the workspace, and the private
    # build directory is gone.
    assert (source.read_bytes(), _list(source.parent)) == before
    assert _list(config.workspace) == [".agentic-fmi", "generated", "models"]
    run_path = config.store_dir / "runs" / view.run_id
    assert runstore.read_build_record(run_path) == record
    assert not (run_path / runstore.WORK_DIR).exists()
    wait_until(lambda: not processes_in(run_path), 10, "the worker's exit")


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        (
            SYNTAX,
            [
                "Build failed (omc): 1 error.",
                "models/Bad.mo:5:1: error: Expected token of type SEMICOLON, got 'Bad' of type "
                "END_IDENT",
            ],
        ),
        (
            UNDECLARED,
            [
                "Build failed (omc): 1 error.",
                "models/Bad.mo:4:3: error: Variable y not found in scope Bad.",
            ],
        ),
        (
            UNDER_DETERMINED,
            [
                "Build failed (omc): 1 error, 1 warning.",
                "error: Too few equations, under-determined system. The model has 1 equation(s) "
                "and 2 variable(s).",
                "models/Bad.mo:3:3: warning: Variable z does not have any remaining equation to "
                "be solved in. The original equations were:",
            ],
        ),
    ],
    ids=["syntax", "undeclared", "under-determined"],
)
def test_a_failed_build_raises_the_exact_diagnostics_and_writes_nothing(
    config: Config,
    supervisor: Supervisor,
    write_source: WriteSource,
    text: str,
    lines: list[str],
) -> None:
    source = write_source("models/Bad.mo", text)
    before = _list(source.parent)
    with pytest.raises(UserError) as caught:
        runs.start_build(config, supervisor, "models/Bad.mo", wait_s=60)
    message = str(caught.value)
    run_id = runstore.list_run_ids(config.store_dir)[-1]
    assert message.splitlines() == [
        *lines,
        TOOL.format(run_id=run_id),
        "Fix the source and call fmu_build again.",
    ]
    for internal in ("/work/", "/tmp", "/home/", "MODELICAPATH", "Curl", "\x1b"):
        assert internal not in message
    assert len(message) <= config.limits.build_diagnostics_max_chars
    assert not config.generated_dir.exists() and _list(source.parent) == before
    view = runs.get_run(config, supervisor, run_id)
    assert view.state is RunState.FAILED and view.kind == "build"
    assert view.error is not None and view.error.code is ErrorCode.BUILD_FAILED
    assert view.error.message == message
    log = (config.store_dir / "runs" / run_id / runstore.BUILD_LOG_FILE).read_text("utf-8")
    assert "@@afmi fmu=" in log  # the raw log keeps everything, including the follow-up noise
    assert not (config.store_dir / "runs" / run_id / runstore.WORK_DIR).exists()


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty ``HOME`` for the worker (and so for omc), removed afterwards (omc copies 154 MB
    of libraries into it)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    yield home
    shutil.rmtree(home, ignore_errors=True)


@pytest.mark.slow
def test_a_fresh_home_builds_offline_and_the_log_shows_only_failed_downloads(
    config: Config,
    supervisor: Supervisor,
    write_source: WriteSource,
    fresh_home: Path,
) -> None:
    write_source("models/Tiny.mo", TINY)
    view = runs.start_build(config, supervisor, "models/Tiny.mo", wait_s=60)
    assert view.state is RunState.FINISHED, view

    log = (config.store_dir / "runs" / view.run_id / runstore.BUILD_LOG_FILE).read_text("utf-8")
    # omc tried to fetch the package index (a fresh HOME has none), and could not: no
    # connection was possible, so nothing was downloaded.
    assert "Curl error for URL https://libraries.openmodelica.org/index/v1/index.json" in log
    assert "Couldn't connect to server" in log
    assert "Downloaded" not in log
    # The libraries came from the omlibrary cache on disk, not from the network.
    assert "Cached libraries were found and will be installed" in log
    assert (fresh_home / ".openmodelica" / "libraries" / "Modelica 4.1.0+maint.om").is_dir()
    # The harmless Error lines of the failed download are noise, not warnings of the build.
    record = runstore.read_build_record(config.generated_dir / "Tiny")
    assert record is not None and "Curl" not in " ".join(record.warnings)
    assert "download" not in " ".join(record.warnings)
    assert str(fresh_home) not in " ".join(record.warnings)


def test_stopping_a_build_kills_omc_and_its_compilers(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source("models/Tiny.mo", TINY)
    view = runs.start_build(config, supervisor, "models/Tiny.mo", wait_s=0)
    run_path = config.store_dir / "runs" / view.run_id
    work = run_path / runstore.WORK_DIR
    wait_until(lambda: len(processes_in(work)) >= 3, 30, "omc and its compiler processes")

    stopped = runs.stop_run(config, supervisor, view.run_id)
    assert stopped.state is RunState.STOPPED, stopped
    wait_until(lambda: not processes_in(run_path), 10, "the end of omc, cmake, make and clang")
    assert not config.generated_dir.exists()
    assert not work.exists()
