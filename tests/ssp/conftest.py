"""Fixtures for the SSP tests: a ``Config`` for the temporary workspace, the synthetic loop FMUs,
an SSP writer, the pythonfmu FMUs of the system-run tests, and a ``Supervisor`` whose workers
are killed and reaped at teardown.

Collaborators: ``tests/conftest.py`` (``workspace``), ``tests/system_fmus.py`` (synthetic FMUs:
zips with a ``modelDescription.xml`` and a placeholder binary; never run), ``ssp_support``
(SSD/SSV text and archives), and ``tests/worker/system_support.py`` (the pythonfmu slave
sources, their build and the exact Jacobi recurrence), loaded by path when this conftest is
imported, because only this directory is on ``sys.path`` when these tests run alone. The test
process never imports ``pythonfmu`` or OMSimulator.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import sys
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from ssp_support import MakeSsp, Pythonfmu, ssd_text, write_zip
from system_fmus import loop_system, write_json, write_loop_fmus

from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.supervisor import Supervisor


@pytest.fixture
def config(workspace: Path) -> Config:
    return replace(load_config(workspace), timing=Timing(poll_interval_s=0.05))


@pytest.fixture
def fmus(workspace: Path) -> dict[str, Path]:
    """``fmus/pi.fmu`` (controller) and ``fmus/plant.fmu`` (plant) in the workspace."""
    return write_loop_fmus(workspace)


@pytest.fixture
def loop(workspace: Path, fmus: dict[str, Path]) -> str:
    """``loop.json``: the closed loop of the two synthetic FMUs (``PI.Kp`` = 10)."""
    write_json(workspace / "loop.json", loop_system())
    return "loop.json"


@pytest.fixture
def make_ssp(workspace: Path, fmus: dict[str, Path]) -> MakeSsp:
    """``make_ssp(name="loop.ssp", ssd=None, members=None, **ssd_options)`` writes an SSP into
    the workspace: ``SystemStructure.ssd`` (``ssd`` bytes, or ``ssd_text(**ssd_options)``),
    ``resources/pi.fmu``, ``resources/plant.fmu``, plus ``members`` (``None`` values drop an
    entry). Returns its path."""

    def make(
        name: str = "loop.ssp",
        ssd: bytes | None = None,
        members: Mapping[str, bytes | None] | None = None,
        **ssd_options: Any,
    ) -> Path:
        content: dict[str, bytes | None] = {
            "SystemStructure.ssd": ssd if ssd is not None else ssd_text(**ssd_options),
            "resources/pi.fmu": fmus["pi"].read_bytes(),
            "resources/plant.fmu": fmus["plant"].read_bytes(),
            **(members or {}),
        }
        kept = {key: value for key, value in content.items() if value is not None}
        return write_zip(workspace / name, kept)

    return make


def _system_support() -> ModuleType:
    """``tests/worker/system_support.py``, imported by path (once per interpreter)."""
    if "system_support" in sys.modules:
        return sys.modules["system_support"]
    path = Path(__file__).resolve().parent.parent / "worker" / "system_support.py"
    spec = importlib.util.spec_from_file_location("system_support", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["system_support"] = module
    spec.loader.exec_module(module)
    return module


SYSTEM_SUPPORT = _system_support()
"""Loaded when this conftest is, so test modules can ``import system_support``."""


@pytest.fixture(scope="session")
def built_fmus(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The pythonfmu PI controller, plant and stalling slave, built once per session."""
    built: dict[str, Path] = SYSTEM_SUPPORT.build_fmus(tmp_path_factory.mktemp("ssp-pyfmu"))
    return built


@pytest.fixture
def pythonfmu(built_fmus: dict[str, Path], workspace: Path) -> Pythonfmu:
    """``pythonfmu(name)`` copies the built ``<name>.fmu`` to ``<workspace>/fmus/``."""

    def copy(name: str) -> Path:
        target = workspace / "fmus" / f"{name}.fmu"
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            shutil.copy2(built_fmus[name], target)
        return target

    return copy


@pytest.fixture
def supervisor(config: Config) -> Iterator[Supervisor]:
    """A ``Supervisor`` whose workers are killed (if still alive) and reaped at teardown."""
    made = Supervisor(config)
    yield made
    for child in list(made._children.values()):
        if child.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGKILL)
        child.process.wait(timeout=10)
