"""Starting system runs: the fully resolved ``spec.json``, the refusals (the step and result-size
guards included), admission, and the views.

Collaborators: ``agentic_fmi.runs`` (``start_run(system=...)``, ``get_run``, ``list_runs``; under
test), ``agentic_fmi.oms_pin`` (monkeypatched by the ``oms`` fixture: no OMSimulator install is
needed), ``Supervisor.spawn`` (faked by ``spawned``: no worker starts, so ``spec.json`` is inspected
as written), ``agentic_fmi.ssp.read_paramset`` (monkeypatched where a parameter set is wanted) and
the fixtures of ``tests/system/conftest.py``. FMUs are synthetic; nothing is run.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from system_fmus import PLANT_VARIABLES, AddSystemRun, WriteSystem, loop_system, out, write_fmu

from agentic_fmi import oms_pin, runs, ssp
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    Constant,
    ErrorCode,
    FileRef,
    OmsVersion,
    RunError,
    RunState,
    StartValue,
    SystemSpec,
)
from agentic_fmi.supervisor import Supervisor
from agentic_fmi.system import Paramset


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def start(config: Config, supervisor: Supervisor, **arguments: Any) -> runs.RunView:
    arguments.setdefault("system", "loop.json")
    arguments.setdefault("wait_s", 0)
    return runs.start_run(config, supervisor, arguments.pop("fmu", None), **arguments)


def read_spec(config: Config, run_id: str) -> SystemSpec:
    spec = runstore.read_job_spec(runstore.run_dir(config.store_dir, run_id))
    assert isinstance(spec, SystemSpec)
    return spec


def nothing_started(config: Config, spawned: list[Path]) -> None:
    assert runstore.list_run_ids(config.store_dir) == [] and spawned == []


# --- the spec --------------------------------------------------------------------------------


def test_a_system_run_writes_the_fully_resolved_spec(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
    workspace: Path,
) -> None:
    write_system()
    before = {path: sha256(path) for path in [*fmus.values(), workspace / "loop.json"]}
    view = start(config, supervisor, start_values={"PI.Ki": 3})
    assert (view.run_id, view.kind, view.state) == ("r1", "system", RunState.QUEUED)
    assert view.fmu == "loop.json" and view.fmi_type == "CoSimulation" and view.t_stop == 10
    assert view.hint is not None and "run_status('r1')" in view.hint
    run_path = runstore.run_dir(config.store_dir, "r1")
    assert spawned == [run_path]

    spec = read_spec(config, "r1")
    assert (spec.kind, spec.protocol, spec.name) == ("system", 1, "loop")
    assert spec.system == FileRef(
        path=str(workspace / "loop.json"),
        sha256=before[workspace / "loop.json"],
        size_bytes=(workspace / "loop.json").stat().st_size,
    )
    assert spec.paramset is None
    assert list(spec.components) == ["PI", "MSD"]
    pi = spec.components["PI"].fmu
    assert (pi.path, pi.sha256, pi.fmi_version, pi.model_name) == (
        str(fmus["pi"]),
        before[fmus["pi"]],
        "2.0",
        "PI",
    )
    assert spec.components["MSD"].fmu.sha256 == before[fmus["plant"]]
    assert {k: (v.value, v.source) for k, v in spec.parameters.items()} == {
        "PI.Kp": (10.0, "system"),
        "PI.Ki": (3.0, "start_values"),
    }
    assert (spec.master.method, spec.master.coupling, spec.master.step_size) == (
        "oms_ma",
        "jacobi",
        0.01,
    )
    experiment = spec.experiment
    assert (experiment.start_time, experiment.stop_time, experiment.output_interval) == (
        0,
        10,
        0.01,
    )
    assert experiment.relative_tolerance is None and experiment.step_size is None
    assert spec.outputs == ["PI.u", "MSD.x", "MSD.v"]
    assert spec.estimated_steps == pytest.approx(1000) and spec.force is False
    assert spec.timeout_s == config.timing.default_timeout_s
    assert spec.omsimulator == oms and spec.versions.agentic_fmi

    raw = json.loads((run_path / "spec.json").read_text(encoding="utf-8"))
    assert raw["kind"] == "system" and raw["protocol"] == 1
    assert raw["connections"] == [
        {"from": "PI.u", "to": "MSD.F"},
        {"from": "MSD.x", "to": "PI.y"},
    ]
    assert raw["parameters"]["PI.Kp"] == {"value": 10.0, "source": "system"}

    status = runstore.read_status(run_path)
    assert (status.state, status.t_start, status.t_stop) == (RunState.QUEUED, 0, 10)
    (entry,) = runstore.read_index(config.store_dir).runs
    assert (entry.kind, entry.fmu_name, entry.fmu_path) == (
        "system",
        "loop",
        str(workspace / "loop.json"),
    )
    events = (config.store_dir / runstore.EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    assert "system loop (2 components, h=0.01) t=0..10" in events[0]
    assert {path: sha256(path) for path in before} == before  # inputs untouched


def test_overrides_a_parameter_set_and_outputs_reach_the_spec(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_system()
    ref = FileRef(path=str(config.workspace / "p.ssv"), sha256="e" * 64, size_bytes=4)
    values: dict[str, StartValue] = {"PI.Kp": 11, "MSD.k": 5}
    seen: list[tuple[Config, str]] = []

    def read_paramset(given: Config, name: str) -> Paramset:
        seen.append((given, name))
        return Paramset(ref=ref, values=values, units={})

    monkeypatch.setattr(ssp, "read_paramset", read_paramset)
    view = start(
        config,
        supervisor,
        paramset="p.ssv",
        start_values={"MSD.k": 6},
        stop_time=2,
        output_interval=0.05,
        outputs=["MSD.x", "time", "PI.u", "MSD.x"],
        timeout_s=30,
    )
    assert seen == [(config, "p.ssv")]
    spec = read_spec(config, view.run_id)
    assert spec.paramset == ref
    assert {k: (v.value, v.source) for k, v in spec.parameters.items()} == {
        "MSD.k": (6.0, "start_values"),
        "PI.Kp": (11.0, "paramset"),
    }
    assert (spec.experiment.start_time, spec.experiment.stop_time) == (0, 2)
    assert spec.master.step_size == 0.05 == spec.experiment.output_interval
    assert spec.estimated_steps == pytest.approx(40)
    assert spec.outputs == ["MSD.x", "PI.u"] and spec.timeout_s == 30
    assert (view.t_stop, spawned[0].name) == (2, "r1")


def test_a_huge_step_count_needs_force(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    with pytest.raises(
        UserError, match=r"1e\+10 communication steps.*raise output_interval.*force"
    ):
        start(config, supervisor, output_interval=1e-9)
    nothing_started(config, spawned)
    view = start(config, supervisor, output_interval=1e-9, force=True, timeout_s=60)
    spec = read_spec(config, view.run_id)
    assert spec.force is True and spec.estimated_steps == pytest.approx(1e10)


def test_a_result_table_above_the_cell_cap_needs_force_or_a_smaller_table(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    """The loop records 3 outputs and time: 1001 rows x 4 columns at h = 0.01 is 4004 cells."""
    write_system()
    capped = replace(config, limits=replace(config.limits, system_max_cells=3000))
    with pytest.raises(UserError, match=r"1,001 rows x 4 columns = 4,004 values") as refused:
        start(capped, supervisor)
    text = str(refused.value)
    assert "(10 s at h = 0.01 s, one row per step; about 0.1 MB of result.csv)" in text
    assert "above the limit of 3,000" in text
    for option in ("raise output_interval", "record fewer outputs", "shorten stop_time", "force"):
        assert option in text
    nothing_started(capped, spawned)

    fewer = start(capped, supervisor, outputs=["MSD.x"])  # 1001 x 2
    coarser = start(capped, supervisor, output_interval=0.05)  # 201 x 4
    forced = start(capped, supervisor, force=True)
    assert read_spec(capped, fewer.run_id).outputs == ["MSD.x"]
    assert read_spec(capped, coarser.run_id).master.step_size == 0.05
    assert read_spec(capped, forced.run_id).force is True
    assert len(spawned) == 3


# --- refusals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "match"),
    [
        ({"fmu": "fmus/pi.fmu"}, r"Pass either fmu \(one FMU\) or system .*, not both"),
        ({"system": None}, r"Pass exactly one of fmu .* or system"),
        ({"system": None, "paramset": "p.ssv"}, r"paramset applies to system runs only.*system="),
        (
            {"system": None, "fmu": "fmus/pi.fmu", "paramset": "p.ssv"},
            r"paramset applies to system runs only.*start_values",
        ),
        ({"inputs": {"PI.r": Constant(value=1)}}, r"Systems have no input signals.*start_values"),
        ({"fmi_type": "ModelExchange"}, r"Systems run Co-Simulation FMUs only; remove fmi_type"),
        ({"fmi_type": "CoSimulation"}, r"remove fmi_type"),
        ({"solver": "CVode"}, r"fixed-step master.*remove solver.*output_interval"),
        ({"relative_tolerance": 1e-6}, r"keeps its own tolerance; remove relative_tolerance"),
        ({"timeout_s": -1}, r"timeout_s must be above 0"),
    ],
)
def test_misuse_is_refused_before_anything_is_claimed(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    match: str,
) -> None:
    write_system()
    monkeypatch.setattr(ssp, "read_paramset", lambda *_: pytest.fail("read_paramset called"))
    with pytest.raises(UserError, match=match):
        start(config, supervisor, **arguments)
    nothing_started(config, spawned)


def test_empty_inputs_are_not_inputs(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    assert start(config, supervisor, inputs={}).kind == "system"


def test_a_pin_mismatch_is_refused_first_with_the_pin_texts_message(
    config: Config,
    supervisor: Supervisor,
    spawned: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = OmsVersion(version="3.0.0", sha256="f" * 64)
    calls: list[tuple[OmsVersion | None, OmsVersion]] = []

    def pin_problem(got: OmsVersion | None, expected: OmsVersion) -> str | None:
        calls.append((got, expected))
        return f"OMSimulator {got and got.sha256} is not the pinned {expected.sha256}. Reinstall."

    monkeypatch.setattr(oms_pin, "installed", lambda: found)
    monkeypatch.setattr(oms_pin, "pin_problem", pin_problem)
    # Even a system file that does not exist: the pin is checked before it is read.
    with pytest.raises(UserError, match=r"f{64} is not the pinned .*Reinstall"):
        start(config, supervisor, system="missing.json")
    assert calls == [(found, oms_pin.pinned())]
    nothing_started(config, spawned)
    monkeypatch.setattr(oms_pin, "installed", lambda: None)
    monkeypatch.setattr(oms_pin, "pin_problem", lambda got, expected: None)
    with pytest.raises(UserError, match="OMSimulator is not installed"):
        start(config, supervisor, system="missing.json")


def test_a_system_with_errors_is_refused_naming_the_first_three(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    pairs = [{"from": f"PI.bad{i}", "to": "MSD.F"} for i in range(5)]
    write_system(loop_system(connections=pairs))
    with pytest.raises(UserError, match=r"Can't run loop: it has 5 error\(s\)") as raised:
        start(config, supervisor)
    text = str(raised.value)
    assert "(1) connections[0].from 'PI.bad0'" in text and "(3) connections[2]" in text
    assert "(4)" not in text and "Call system_check for all of them" in text
    nothing_started(config, spawned)


@pytest.mark.parametrize(
    ("arguments", "match"),
    [
        ({"stop_time": 0}, r"stop_time \(0\) must be greater than start_time \(0\)"),
        ({"start_time": 20}, r"stop_time \(10\) must be greater than start_time \(20\)"),
        ({"output_interval": 20}, r"Can't run loop: the step 20 s exceeds the horizon 10 s"),
        ({"start_time": 1}, r"Can't run loop: start_time 1 can't be used: .*always starts"),
        (
            {"output_interval": 0.3},
            r"horizon 10 s is 33.3333 steps of 0.3 s.*such as 0.30303 s \(33 steps",
        ),
        ({"output_interval": float("nan")}, r"must be a positive number"),
        ({"start_values": {"PI.Kpp": 1}}, r"start_values entry 'PI.Kpp'.*Close matches: Kp"),
        ({"start_values": {"Kp": 1}}, r"'Kp' is not '<component>.<variable>'"),
        ({"start_values": {"PI.u": 1}}, r"PI.u is an output"),
        ({"start_values": {"MSD.m": "heavy"}}, r"needs a number"),
        ({"outputs": ["MSD.xx"]}, r"outputs entry 'MSD.xx'.*no variable 'xx'.*Close matches: x"),
        ({"outputs": ["x"]}, r"outputs entry 'x': 'x' is not '<component>.<variable>'"),
        ({"outputs": ["time"]}, r"names only 'time'"),
    ],
)
def test_bad_run_arguments_are_refused_with_the_fix(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
    arguments: dict[str, Any],
    match: str,
) -> None:
    write_system()
    with pytest.raises(UserError, match=match):
        start(config, supervisor, **arguments)
    nothing_started(config, spawned)


def test_a_string_output_cannot_be_recorded(
    config: Config,
    supervisor: Supervisor,
    workspace: Path,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_fmu(
        workspace / "fmus" / "plant.fmu",
        [*PLANT_VARIABLES, out("note", type="String")],
        model="MSD",
    )
    write_system()
    with pytest.raises(UserError, match=r"Output 'MSD.note' is a String variable"):
        start(config, supervisor, outputs=["MSD.note"])
    # The defaults skip it.
    assert read_spec(config, start(config, supervisor).run_id).outputs == [
        "PI.u",
        "MSD.x",
        "MSD.v",
    ]


def test_without_outputs_to_record_the_caller_must_name_some(
    config: Config,
    supervisor: Supervisor,
    workspace: Path,
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_fmu(workspace / "sink.fmu", [out("s", type="String")], model="Sink")
    write_system(
        {
            "version": 1,
            "components": {"S": {"fmu": "sink.fmu"}},
            "experiment": {"stop_time": 1, "step_size": 0.1},
        }
    )
    with pytest.raises(UserError, match=r"No component of loop declares a recordable output"):
        start(config, supervisor)
    nothing_started(config, spawned)


# --- admission -------------------------------------------------------------------------------


def test_a_system_run_takes_a_worker_slot(
    config: Config,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    one = replace(config, max_workers=1)
    supervisor = Supervisor(one)
    assert start(one, supervisor).run_id == "r1"
    with pytest.raises(UserError, match=r"already queued or running \(r1\); the limit is 1"):
        start(one, supervisor)
    assert runstore.list_run_ids(config.store_dir) == ["r1"]


def test_concurrent_system_starts_cannot_both_take_the_last_slot(
    config: Config,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    one = replace(config, max_workers=1)
    supervisor = Supervisor(one)
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def go() -> None:
        barrier.wait()
        try:
            outcomes.append(start(one, supervisor).run_id)
        except UserError as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len([o for o in outcomes if o == "r1"]) == 1
    assert len([o for o in outcomes if "limit is 1" in o]) == 5, outcomes
    assert len(spawned) == 1


# --- views -----------------------------------------------------------------------------------


def test_a_queued_system_run_is_described_by_run_status_and_run_list(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system()
    run_id = start(config, supervisor).run_id
    view = runs.get_run(config, supervisor, run_id)
    assert (view.kind, view.state, view.fmu, view.fmi_type) == (
        "system",
        RunState.QUEUED,
        "loop.json",
        "CoSimulation",
    )
    page = runs.list_runs(config, supervisor)
    assert [(i.run_id, i.kind, i.fmu_name) for i in page.items] == [(run_id, "system", "loop")]


def test_a_finished_system_run_has_a_summary_and_a_tuning_hint(
    config: Config, supervisor: Supervisor, add_system_run: AddSystemRun
) -> None:
    run_id = add_system_run({"PI.Kp": 10.0})
    view = runs.get_run(config, supervisor, run_id)
    assert (view.kind, view.state, view.fmu, view.fmi_type) == (
        "system",
        RunState.FINISHED,
        "loop.json",
        "CoSimulation",
    )
    assert view.summary is not None and [v.name for v in view.summary.variables] == ["MSD.x"]
    assert view.t == 1 and view.t_stop == 1 and view.progress == 1
    assert view.hint is not None
    assert "run_plot or run_results on 'r1'" in view.hint and "run_compare" in view.hint
    assert "start_values={'<component>.<parameter>': v}" in view.hint


def test_a_stopped_system_run_keeps_its_partial_result_hint(
    config: Config, supervisor: Supervisor, add_system_run: AddSystemRun
) -> None:
    run_id = add_system_run(state=RunState.STOPPED)
    view = runs.get_run(config, supervisor, run_id)
    assert view.state is RunState.STOPPED and view.hint is not None
    assert "partial result is kept" in view.hint and "start_values" not in view.hint


def test_a_lost_worker_hint_points_to_testing_each_component_alone(
    config: Config, supervisor: Supervisor, add_system_run: AddSystemRun
) -> None:
    lost = RunError(code=ErrorCode.WORKER_LOST, message="The worker died.")
    view = runs.get_run(config, supervisor, add_system_run(state=RunState.FAILED, error=lost))
    assert view.error is not None and view.error.code is ErrorCode.WORKER_LOST
    assert view.hint is not None
    assert "aborts the worker" in view.hint and "run_start(fmu=...)" in view.hint
    other = RunError(code=ErrorCode.SIMULATION_ERROR, message="x")
    view = runs.get_run(config, supervisor, add_system_run(state=RunState.FAILED, error=other))
    assert view.hint is not None and "aborts the worker" not in view.hint


def test_a_system_files_start_time_can_be_overridden_to_zero(
    config: Config,
    supervisor: Supervisor,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    oms: OmsVersion,
    spawned: list[Path],
) -> None:
    write_system(loop_system(experiment={"start_time": 1, "stop_time": 11, "step_size": 0.01}))
    with pytest.raises(UserError, match=r"Can't run loop: start_time 1 can't be used"):
        start(config, supervisor)
    nothing_started(config, spawned)
    view = start(config, supervisor, start_time=0, stop_time=10)
    assert read_spec(config, view.run_id).experiment.start_time == 0
