"""Live system runs on OMSimulator (ADR 0010): the engine's t0 gate and its ``Live``
handle ``_OmsLive`` against a fake instance, then real workers on the pythonfmu PI + plant loop:
a mid-run gain change matches the exact Jacobi recurrence with the gains switched at that step,
a set on a connected input is rejected and the run goes on untouched, and sets on an output or
a fixed parameter are rejected while a tunable parameter is set.

The engine-neutral live contract (pause, steps, speed, sets, stop, timeout, idle limit) is held
for both engines by ``test_live_contract.py``.

Collaborators: ``agentic_fmi.engines.oms_engine`` (its private ``_step`` and ``_OmsLive``, which
import no OMSimulator), ``agentic_fmi.worker`` (as ``python -m agentic_fmi worker``; OMSimulator
runs only there, never in this process), the fixtures of ``tests/live/conftest.py``
(``pythonfmu``, ``new_live_system_run``, ``spawn``, ``await_status``), ``system_support`` (the
recurrence) and ``live_support`` (commands in the server's and the pane's way).
"""

from __future__ import annotations

import math
import subprocess
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from live_support import outcome_of, send, send_pane, system_spec
from system_support import PI_DEFAULTS, PLANT_DEFAULTS, jacobi_loop

from agentic_fmi.engines import oms_engine
from agentic_fmi.errors import SetRefused
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState, RunStatus

FmuFactory = Callable[[str, str], Path]
Pythonfmu = Callable[[str], Path]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
AwaitStatus = Callable[..., RunStatus]

OK = "Status.ok"
"""Stands in for ``OMSimulator.capi.Status.ok``."""
LOOP = [("PI.u", "P.F"), ("P.x", "PI.y")]
OUTPUTS = ["PI.u", "P.x", "PI.y", "P.F"]
H, STOP = 0.01, 1.0
STEPS = round(STOP / H)
FAST = 1000.0
REFUSAL = (
    "Cannot set variable 'u' after the FMU is initialized. Only variables with causality='input' "
    "or causality='parameter' and variability='tunable' can be set at this stage."
)


# --- the engine against a fake instance ---------------------------------------------------------


class FakeInstance:
    """An ``InstantiatedModel`` stand-in: ``t += h`` per ``doStep``; ``getValue`` answers
    ``values`` (else the time); ``setValue`` records, or refuses names in ``refuse`` as
    OMSimulator does (a ``RuntimeWarning`` and ``None``)."""

    def __init__(self, h: float) -> None:
        self.t, self.h, self.steps = 0.0, h, 0
        self.values: dict[str, Any] = {}
        self.sets: list[tuple[str, Any]] = []
        self.refuse: set[str] = set()

    def getTime(self) -> float:
        return self.t

    def doStep(self) -> None:
        self.t += self.h
        self.steps += 1

    def getValue(self, cref: str) -> Any:
        return self.values.get(cref, self.t)

    def setValue(self, cref: str, value: Any) -> Any:
        if cref in self.refuse:
            warnings.warn(REFUSAL, RuntimeWarning, stacklevel=2)
            return None
        self.sets.append((cref, value))
        return OK


def _crefs() -> tuple[Callable[..., str], list[str]]:
    """A ``CRef`` stand-in that joins its names, and the list of what it made."""
    made: list[str] = []

    def cref(*names: str) -> str:
        made.append(".".join(names))
        return made[-1]

    return cref, made


def _live_spec(fmu: FmuFactory, live: bool = True) -> Any:
    spec = system_spec("r1", {"M": fmu("2.0", "Feedthrough")}, [], ["M.Int32_output"])
    return spec.model_copy(update={"live": live, "idle_limit_s": 60.0 if live else None})


def _live(inst: FakeInstance) -> oms_engine._OmsLive:
    return oms_engine._OmsLive(inst, _crefs()[0], OK)


def test_a_live_system_gates_at_start_time_before_its_first_step(fmu: FmuFactory) -> None:
    seen: list[tuple[float, int, list[float | None]]] = []
    inst = FakeInstance(H)

    def gate(t: float, live: Any) -> bool:
        seen.append((t, inst.steps, live.get(["M.Int32_output"])))
        return True

    columns, stopped = oms_engine._step(inst, [], _live_spec(fmu), gate, _live(inst))
    assert not stopped and len(columns[0]) == STEPS + 1
    assert seen[0] == (0.0, 0, [0.0]) and len(seen) == STEPS + 1
    assert seen[1][:2] == (pytest.approx(H), 1)


def test_a_stop_at_start_time_returns_row_0_as_stopped(fmu: FmuFactory) -> None:
    inst = FakeInstance(H)
    columns, stopped = oms_engine._step(
        inst, ["M.Int32_output"], _live_spec(fmu), lambda t, _live: False, _live(inst)
    )
    assert stopped and inst.steps == 0
    assert [list(column) for column in columns] == [[0.0], [0.0]]


def test_a_batch_system_is_not_gated_at_start_time(fmu: FmuFactory) -> None:
    seen: list[float] = []
    inst = FakeInstance(H)

    def gate(t: float, _live: object) -> bool:
        seen.append(t)
        return True

    oms_engine._step(inst, [], _live_spec(fmu, live=False), gate, _live(inst))
    assert seen[0] == pytest.approx(H) and len(seen) == STEPS


def test_live_get_reads_floats_and_makes_each_cref_once() -> None:
    inst = FakeInstance(H)
    inst.values = {"default.PI.u": 2, "default.B.on": True, "default.P.x": math.nan}
    cref, made = _crefs()
    live = oms_engine._OmsLive(inst, cref, OK)
    names = ["PI.u", "B.on", "P.x", "body.v[1]"]
    assert live.get(names) == [2.0, 1.0, None, 0.0]
    assert live.get(names[:2]) == [2.0, 1.0]
    assert made == ["default.PI.u", "default.B.on", "default.P.x", "default.body.v[1]"]


def test_live_set_sets_in_order_and_raises_naming_a_refused_variable() -> None:
    inst = FakeInstance(H)
    cref, _made = _crefs()
    live = oms_engine._OmsLive(inst, cref, OK)
    live.set({"PI.Kp": 4.0, "P.d": 0.5, "M.Int32_input": 7, "M.Boolean_input": True})
    assert inst.sets == [
        ("default.PI.Kp", 4.0),
        ("default.P.d", 0.5),
        ("default.M.Int32_input", 7),
        ("default.M.Boolean_input", True),
    ]
    inst.refuse = {"default.PI.u"}
    with pytest.raises(
        SetRefused, match=r"did not set PI\.u = 1\.0: Only variables with"
    ) as raised:
        live.set({"PI.Kp": 5.0, "PI.u": 1.0, "P.d": 0.5})
    assert inst.sets[-1] == ("default.PI.Kp", 5.0)  # values before the refusal stay set
    assert raised.value.applied == {"PI.Kp": 5.0}  # and the refusal lists them, not the later ones
    with pytest.raises(SetRefused) as first:
        live.set({"PI.u": 1.0})
    assert first.value.applied == {}


# --- real workers on the PI + plant loop --------------------------------------------------------


def switched_loop(
    steps: int,
    h: float,
    pi: Mapping[str, float],
    plant: Mapping[str, float],
    at: int,
    gains: Mapping[str, float],
) -> dict[str, np.ndarray]:
    """``system_support.jacobi_loop`` with the PI's ``gains`` changed after step ``at``: a set at
    ``t = at*h`` acts in the step from ``at`` to ``at + 1`` and after."""
    a = math.exp(-h / plant["tau"])
    u, x, integral = np.zeros(steps + 1), np.zeros(steps + 1), 0.0
    for k in range(steps):
        now = {**pi, **gains} if k >= at else pi
        error = now["r"] - x[k]
        integral += error * h
        u[k + 1] = now["Kp"] * error + now["Ki"] * integral
        x[k + 1] = a * x[k] + plant["K"] * (1.0 - a) * u[k]
    return {"PI.u": u, "PI.y": x, "P.x": x, "P.F": u}


def test_the_switched_recurrence_without_a_switch_is_the_jacobi_loop() -> None:
    plain = jacobi_loop(STEPS, H, PI_DEFAULTS, PLANT_DEFAULTS)
    never = switched_loop(STEPS, H, PI_DEFAULTS, PLANT_DEFAULTS, STEPS, {"Kp": 4.0})
    for name in OUTPUTS:
        assert np.array_equal(plain[name], never[name]), name


def _loop(pythonfmu: Pythonfmu) -> dict[str, Path]:
    return {"PI": pythonfmu("PIController"), "P": pythonfmu("Plant")}


def _paused(status: RunStatus) -> bool:
    return status.state is RunState.PAUSED


def _finish(run_path: Path, child: subprocess.Popen[bytes]) -> RunStatus:
    assert child.wait(timeout=30) == 0, (run_path / runstore.LOG_FILE).read_text()
    return runstore.read_status(run_path)


def _assert_matches(result: np.ndarray, expected: Mapping[str, np.ndarray]) -> None:
    assert len(result) == STEPS + 1
    for name in OUTPUTS:
        assert np.max(np.abs(result[name] - expected[name])) < 1e-12, name


@pytest.mark.oms
def test_a_mid_run_gain_change_matches_the_recurrence_switched_at_that_step(
    pythonfmu: Pythonfmu, new_live_system_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    at, gains = 50, {"Kp": 4.0, "Ki": 30.0}
    run_path = new_live_system_run(_loop(pythonfmu), connections=LOOP, outputs=OUTPUTS)
    child = spawn(run_path)
    await_status(run_path, _paused)
    assert outcome_of(run_path, send(run_path, "step", n=at)).t == pytest.approx(at * H)
    values = {f"PI.{name}": value for name, value in gains.items()}
    applied = outcome_of(run_path, send(run_path, "set", values=values))
    assert (applied.status, applied.message) == ("applied", None)
    assert applied.t == pytest.approx(at * H)
    assert outcome_of(run_path, send(run_path, "resume", speed=FAST)).status == "applied"
    assert _finish(run_path, child).state is RunState.FINISHED
    result = runstore.read_result(run_path)
    _assert_matches(result, switched_loop(STEPS, H, PI_DEFAULTS, PLANT_DEFAULTS, at, gains))
    unswitched = jacobi_loop(STEPS, H, PI_DEFAULTS, PLANT_DEFAULTS)
    # x reaches the new gains only through u: unchanged up to the row after the step after the set
    assert np.max(np.abs(result["P.x"][: at + 2] - unswitched["P.x"][: at + 2])) < 1e-12
    assert np.max(np.abs(result["PI.u"] - unswitched["PI.u"])) > 0.1  # the switch shows


@pytest.mark.oms
def test_a_set_on_a_connected_input_is_rejected_and_the_run_goes_on(
    pythonfmu: Pythonfmu, new_live_system_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = new_live_system_run(_loop(pythonfmu), connections=LOOP, outputs=OUTPUTS)
    child = spawn(run_path)
    await_status(run_path, _paused)
    assert outcome_of(run_path, send(run_path, "step", n=10)).status == "applied"
    pulse = send_pane(run_path, "set", values={"P.F": 5.0})  # the server would refuse it first
    rejected = outcome_of(run_path, pulse)
    assert (rejected.status, rejected.source) == ("rejected", "pane")
    assert "P.F is an input driven by the connection from PI.u" in (rejected.message or "")
    assert runstore.read_status(run_path).state is RunState.PAUSED
    assert outcome_of(run_path, send(run_path, "resume", speed=FAST)).status == "applied"
    assert _finish(run_path, child).state is RunState.FINISHED
    _assert_matches(
        runstore.read_result(run_path), jacobi_loop(STEPS, H, PI_DEFAULTS, PLANT_DEFAULTS)
    )


@pytest.mark.oms
def test_sets_on_an_output_or_a_fixed_parameter_are_rejected(
    pythonfmu: Pythonfmu,
    fmu: FmuFactory,
    new_live_system_run: RunBuilder,
    spawn: Spawn,
    await_status: AwaitStatus,
) -> None:
    components = {**_loop(pythonfmu), "F": fmu("2.0", "Feedthrough")}
    outputs = [*OUTPUTS, "F.Float64_continuous_output"]
    run_path = new_live_system_run(components, connections=LOOP, outputs=outputs)
    child = spawn(run_path)
    await_status(run_path, _paused)
    output = outcome_of(run_path, send_pane(run_path, "set", values={"PI.u": 3.0}))
    assert output.status == "rejected" and "PI.u is an output" in (output.message or "")
    fixed = outcome_of(
        run_path, send_pane(run_path, "set", values={"F.Float64_fixed_parameter": 1.0})
    )
    assert fixed.status == "rejected" and "fixed parameter" in (fixed.message or "")
    tunable = {"F.Float64_tunable_parameter": 1.5, "F.Float64_continuous_input": 0.25}
    assert outcome_of(run_path, send(run_path, "set", values=tunable)).status == "applied"
    assert outcome_of(run_path, send(run_path, "resume", speed=FAST)).status == "applied"
    status = _finish(run_path, child)
    assert status.state is RunState.FINISHED and status.error is None
    result = runstore.read_result(run_path)
    _assert_matches(result, jacobi_loop(STEPS, H, PI_DEFAULTS, PLANT_DEFAULTS))
    assert result["F.Float64_continuous_output"][0] == 0.0
    assert np.all(result["F.Float64_continuous_output"][1:] == 0.25)
