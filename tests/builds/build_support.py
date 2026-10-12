"""Helpers for the ``fmu_build`` tests: tiny slave sources, process lookup, polling, and
hand-written ``build.json`` records for the overwrite rules.

Collaborators: ``tests/builds/conftest.py`` (the fixtures that use them), ``agentic_fmi.supervisor``
(``kill_leftovers`` reaps its workers) and the test modules,
which import this module by name because pytest puts this directory on ``sys.path``.
``agentic_fmi.store`` writes the records. The slave sources are text: the test process never
imports ``pythonfmu`` (ruff TID251), and only build workers run it.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.config import Config, Timing
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    BuildRecord,
    BuildSummary,
    BuildVersions,
    BuiltFmu,
    SourceRef,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor

TEST_TIMING = Timing(poll_interval_s=0.05)
"""Defaults (a 30 min build limit) with a faster poll while ``fmu_build`` waits."""

SLAVE = '''"""A gain: y = k * u, computed in do_step."""

from pythonfmu import DefaultExperiment, Fmi2Causality, Fmi2Slave, Fmi2Variability, Real


class {cls}(Fmi2Slave):
    author = "tests"
    description = "y = k * u"
    default_experiment = DefaultExperiment(start_time=0.0, stop_time=1.0, step_size=0.1)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k = {k}
        self.u = 1.0
        self.y = 0.0
        tunable = Fmi2Variability.tunable
        self.register_variable(Real("k", causality=Fmi2Causality.parameter, variability=tunable))
        self.register_variable(Real("u", causality=Fmi2Causality.input))
        self.register_variable(Real("y", causality=Fmi2Causality.output))

    def do_step(self, current_time, step_size):
        self.y = self.k * self.u
        return True
'''
"""A minimal valid pythonfmu slave; format with ``cls`` (the class) and ``k`` (the gain)."""

SLOW_SLAVE = """import time

from pythonfmu import Fmi2Slave

time.sleep(120)  # blocks pythonfmu's import of the module: only a stop or the timeout ends it


class AfmiSlow(Fmi2Slave):
    def do_step(self, current_time, step_size):
        return True
"""
"""A slave whose build never ends by itself."""


def processes_naming(path: Path) -> list[int]:
    """Live processes (not zombies) whose command line contains ``path``, except this one."""
    needle = str(path).encode()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline:
            found.append(int(entry.name))
    return found


def wait_until(condition: Callable[[], Any], timeout_s: float = 30.0, what: str = "it") -> Any:
    """Poll ``condition`` every 50 ms until it returns something truthy; fail after the timeout."""
    deadline = time.monotonic() + timeout_s
    while not (value := condition()):
        if time.monotonic() > deadline:
            pytest.fail(f"{what} didn't happen within {timeout_s} s")
        time.sleep(0.05)
    return value


def fake_record(name: str, sha256: str = "c" * 64, source: str = "models/old.py") -> BuildRecord:
    """A valid ``build.json`` record for ``generated/<name>/``."""
    return BuildRecord(
        run_id="r99",
        name=name,
        built_at=utc_now(),
        backend="pythonfmu",
        source=SourceRef(path=source, sha256="d" * 64, size_bytes=10),
        options={"fmi_version": "2.0", "fmu_type": "cs"},
        versions=BuildVersions(agentic_fmi="0", python="3.12", platform="x86_64-linux"),
        fmu=BuiltFmu(
            path=f"generated/{name}/{name}.fmu",
            sha256=sha256,
            size_bytes=1,
            fmi_version="2.0",
            model_name=name,
            model_identifier=name,
            interfaces=["CoSimulation"],
        ),
        duration_s=0.1,
        summary=BuildSummary(counts={"total": 0}, inputs=[], outputs=[], parameters=[]),
    )


def plant_build(config: Config, name: str, sha256: str = "c" * 64) -> Path:
    """``generated/<name>/`` as an earlier fmu_build left it: a record and a dummy FMU."""
    directory = config.generated_dir / name
    directory.mkdir(parents=True)
    (directory / f"{name}.fmu").write_bytes(b"old")
    runstore.write_build_record(directory, fake_record(name, sha256))
    return directory


def event_lines(config: Config) -> list[str]:
    """The lines of ``events.jsonl`` (empty if there is none)."""
    path = config.store_dir / runstore.EVENTS_FILE
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def processes_in(directory: Path) -> list[int]:
    """Live processes whose working directory is ``directory`` or below it, except this one
    (omc and the cmake, make and clang processes it starts all work inside ``work/``)."""
    root = str(directory)
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(f"{root}/"):
            found.append(int(entry.name))
    return found


def kill_leftovers(supervisor: Supervisor, workspace: Path) -> None:
    """Kill and reap every worker ``supervisor`` spawned, then any process still naming
    ``workspace`` (a stray build tool). Fixture teardown, so nothing outlives the tests."""
    for child in list(supervisor._children.values()):
        if child.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGKILL)
        child.process.wait(timeout=10)
    for pid in processes_naming(workspace):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
