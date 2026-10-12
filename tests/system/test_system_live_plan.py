"""Planning a live system run (ADR 0010): ``run_start(system=..., live=True)`` records ``live``
and ``idle_limit_s`` in the ``SystemSpec``, applies the same checks as a batch system run, and
hints at ``run_control``; a batch system run records neither.

Collaborators: ``agentic_fmi.runs`` (``start_run``, ``view_of``; under test),
``agentic_fmi.system`` (``plan_run``, ``SystemPlan.spec``), the fixtures of
``tests/system/conftest.py`` (synthetic FMUs, a fake OMSimulator install that matches the pin,
and a faked ``Supervisor.spawn``: no worker starts and nothing is run).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from system_fmus import WriteSystem, loop_system

from agentic_fmi import runs, system
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import OmsVersion, RunState, SystemSpec
from agentic_fmi.supervisor import Supervisor


def read_spec(config: Config, run_id: str) -> SystemSpec:
    spec = runstore.read_job_spec(runstore.run_dir(config.store_dir, run_id))
    assert isinstance(spec, SystemSpec)
    return spec


def test_a_live_system_run_records_live_and_the_idle_limit(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    view = runs.start_run(config, supervisor, system="loop.json", live=True, wait_s=0)
    spec = read_spec(config, view.run_id)
    assert (spec.live, spec.idle_limit_s) == (True, config.timing.live_idle_limit_s)
    assert (view.kind, view.state, len(spawned)) == ("system", RunState.QUEUED, 1)
    assert view.hint is not None
    assert "run_status('r1')" in view.hint and "run_control('r1'" in view.hint
    assert spec.master.step_size == 0.01 and spec.outputs  # planned exactly like a batch run


def test_a_batch_system_run_records_neither(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    view = runs.start_run(config, supervisor, system="loop.json", wait_s=0)
    spec = read_spec(config, view.run_id)
    assert (spec.live, spec.idle_limit_s) == (False, None)
    assert view.hint is not None and "run_control" not in view.hint


def test_the_idle_limit_follows_the_configured_timing(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    configured = replace(config, timing=replace(config.timing, live_idle_limit_s=45.0))
    runs.start_run(configured, supervisor, system="loop.json", live=True, wait_s=0)
    assert read_spec(config, "r1").idle_limit_s == 45.0


def test_plan_run_builds_the_live_spec_directly(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem, oms: OmsVersion
) -> None:
    write_system()
    loaded = system.load_system(config, "loop.json")
    live = system.plan_run(config, loaded, timeout_s=60.0, omsimulator=oms, live=True)
    batch = system.plan_run(config, loaded, timeout_s=60.0, omsimulator=oms)
    assert (live.spec("r1").live, live.spec("r1").idle_limit_s) == (True, 1800.0)
    assert (batch.spec("r1").live, batch.spec("r1").idle_limit_s) == (False, None)
    same = {"live", "idle_limit_s", "created_at"}  # the only fields a live plan changes
    assert live.spec("r1").model_dump(exclude=same) == batch.spec("r1").model_dump(exclude=same)


def test_a_live_system_run_gets_the_same_refusals_as_a_batch_one(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system(loop_system(experiment={"start_time": 0, "stop_time": 10, "step_size": 1e-12}))
    with pytest.raises(UserError, match="communication steps"):
        runs.start_run(config, supervisor, system="loop.json", live=True, wait_s=0)
    assert runstore.list_run_ids(config.store_dir) == [] and spawned == []
    write_system()
    with pytest.raises(UserError, match="Systems have no input signals"):
        runs.start_run(
            config,
            supervisor,
            system="loop.json",
            live=True,
            inputs={"PI.r": {"kind": "constant", "value": 1}},  # type: ignore[dict-item]
            wait_s=0,
        )
