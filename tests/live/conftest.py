"""Fixtures for the live-run tests (ADR 0010): live run directories (one FMU or a system) and the
Discarder C FMU (a step discarded without terminating).

Collaborators: the shared ``workspace`` and ``fmu`` fixtures (``tests/conftest.py``), the
worker fixtures of ``tests/worker/conftest.py`` (``store_dir``, ``new_run``, ``spawn``,
``worker_tmp``, ``run_worker``, ``await_status``, and for systems ``built_fmus`` and
``pythonfmu``), loaded by path and re-exported here because conftest modules aren't importable
packages, ``tests/worker/system_support.py`` (the system spec builder, the pythonfmu slaves and
the exact Jacobi recurrence), loaded by path as ``system_support`` because only this directory
is on ``sys.path`` when these tests run alone, and ``live_support`` (the Discarder build). The
``discarder`` FMU is compiled once per session (the tests that need it skip without ``gcc``);
the test process never loads it, only workers do, and never imports OMSimulator or
``pythonfmu``.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from live_support import build_discarder

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState, RunStatus, utc_now

TESTS_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _system_support() -> ModuleType:
    """``tests/worker/system_support.py`` as ``system_support`` (once per interpreter), so the
    worker fixtures that import it by name and this directory's tests share one module."""
    if "system_support" not in sys.modules:
        module = _load("system_support", TESTS_DIR / "worker" / "system_support.py")
        sys.modules["system_support"] = module
    return sys.modules["system_support"]


SYSTEM_SUPPORT = _system_support()
_worker = _load("_live_worker_fixtures", TESTS_DIR / "worker" / "conftest.py")
store_dir = _worker.store_dir
new_run = _worker.new_run
worker_tmp = _worker.worker_tmp
spawn = _worker.spawn
run_worker = _worker.run_worker
await_status = _worker.await_status
wait_for_status = _worker.wait_for_status
built_fmus = _worker.built_fmus
pythonfmu = _worker.pythonfmu

IDLE_LIMIT_S = 60.0
"""The idle limit of a live test run unless a test sets its own."""


@pytest.fixture
def new_live_run(new_run: Callable[..., Path]) -> Callable[..., Path]:
    """``new_live_run(fmu_path, idle_limit_s=..., **new_run_options)``: a queued live run
    (Co-Simulation), built with ``new_run``."""

    def build(fmu_path: Path, idle_limit_s: float = IDLE_LIMIT_S, **options: Any) -> Path:
        return new_run(fmu_path, "CoSimulation", live=True, idle_limit_s=idle_limit_s, **options)

    return build


@pytest.fixture
def new_live_system_run(store_dir: Path, workspace: Path) -> Callable[..., Path]:
    """``new_live_system_run(components, idle_limit_s=..., **system_spec_options)``: a queued
    live system run (``system_support.system_spec`` with ``live`` and ``idle_limit_s``)."""

    def build(
        components: Mapping[str, Path], idle_limit_s: float = IDLE_LIMIT_S, **options: Any
    ) -> Path:
        run_id, run_path = runstore.claim_run_id(store_dir)
        system_file = workspace / f"{run_id}.system.json"
        batch = SYSTEM_SUPPORT.system_spec(run_id, system_file, components, **options)
        spec = batch.model_copy(update={"live": True, "idle_limit_s": idle_limit_s})
        runstore.write_spec(run_path, spec)
        queued = RunStatus(
            run_id=run_id,
            state=RunState.QUEUED,
            t_start=spec.experiment.start_time,
            t_stop=spec.experiment.stop_time,
            updated_at=utc_now(),
        )
        runstore.write_status(run_path, queued)
        return run_path

    return build


@pytest.fixture(scope="session")
def built_discarder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The Discarder FMU, built once per session (skips without gcc)."""
    if shutil.which("gcc") is None:
        pytest.skip("gcc is needed to build the Discarder test FMU")
    return build_discarder(tmp_path_factory.mktemp("discarder"))


@pytest.fixture
def discarder(built_discarder: Path, workspace: Path) -> Path:
    """A copy of the Discarder FMU in the test workspace."""
    target = workspace / "fmus" / built_discarder.name
    target.parent.mkdir(exist_ok=True)
    shutil.copy2(built_discarder, target)
    return target
