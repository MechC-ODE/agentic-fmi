"""Unit tests for ``engines/oms_engine.py`` without OMSimulator: the stepping loop, the checked
parameter set, the recordable outputs and the result layout, against fakes and Reference FMUs.

Collaborators: ``agentic_fmi.engines.oms_engine`` (its private helpers; ``simulate`` itself runs
only in workers, see ``tests/worker/test_worker_system.py`` and
``tests/worker/test_engine_contract.py``), the shared ``fmu`` fixture. The engine module imports
OMSimulator only inside ``simulate``, so importing it here loads nothing.
"""

from __future__ import annotations

import shutil
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from agentic_fmi.engines import oms_engine
from agentic_fmi.fmu.inspect import read_description, sha256_file
from agentic_fmi.store.protocol import (
    ComponentSpec,
    Experiment,
    FileRef,
    FmuRef,
    MasterAlgorithm,
    OmsVersion,
    SystemSpec,
    Versions,
    utc_now,
)
from agentic_fmi.store.runstore import SYSTEM_FMUS_DIR

FmuFactory = Callable[[str, str], Path]
OK = "Status.ok"
"""Stands in for ``OMSimulator.capi.Status.ok``."""


class FakeInstance:
    """An ``InstantiatedModel`` stand-in: ``t += h`` per ``doStep`` (``stall_at``: from then on
    the clock stops), ``getValue`` returns the time, ``setValue`` as configured."""

    def __init__(self, h: float, start: float = 0.0, stall_at: float | None = None) -> None:
        self.t, self.h, self.stall_at = start, h, stall_at
        self.steps = 0
        self.set_result: Any = OK
        self.warn = False

    def getTime(self) -> float:
        return self.t

    def doStep(self) -> None:
        if self.stall_at is None or self.t < self.stall_at:
            self.t += self.h
        self.steps += 1

    def getValue(self, cref: Any) -> float:
        return self.t

    def setValue(self, cref: Any, value: Any) -> Any:
        if self.warn:
            warnings.warn(
                "Cannot set variable 'x'. Variable 'x' is local.", RuntimeWarning, stacklevel=2
            )
            return None
        return self.set_result


def _spec(
    components: dict[str, Path],
    outputs: Sequence[str],
    *,
    h: float = 0.1,
    start: float = 0.0,
    stop: float = 1.0,
) -> SystemSpec:
    refs = {}
    for name, path in components.items():
        md = read_description(path)
        refs[name] = ComponentSpec(
            fmu=FmuRef(
                path=str(path),
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
                fmi_version=md.fmiVersion,
                model_name=md.modelName,
            )
        )
    return SystemSpec(
        run_id="r1",
        created_at=utc_now(),
        name="test",
        system=FileRef(path="/nowhere/test.json", sha256="0" * 64, size_bytes=0),
        components=refs,
        master=MasterAlgorithm(step_size=h),
        experiment=Experiment(start_time=start, stop_time=stop, output_interval=h),
        outputs=list(outputs),
        timeout_s=60.0,
        versions=Versions.current(),
        omsimulator=OmsVersion(version="3.0.0.post198", sha256="0" * 64),
    )


def _bb_spec(fmu: FmuFactory, **experiment: float) -> SystemSpec:
    return _spec({"B": fmu("2.0", "BouncingBall")}, ["B.h"], **experiment)


def test_step_records_one_row_per_step_without_a_sliver(fmu: FmuFactory) -> None:
    seen: list[float] = []

    def gate(t: float, _live: object) -> bool:
        seen.append(t)
        return True

    inst = FakeInstance(h=0.1)
    columns, stopped = oms_engine._step(inst, ["c"], _bb_spec(fmu), gate, None)
    assert not stopped
    assert inst.steps == 10 and len(columns[0]) == 11  # row 0 plus one per step
    assert list(columns[0]) == list(columns[1])
    assert seen == list(columns[0])[1:]  # the gate after every step, not at t = 0
    assert columns[0][-1] == pytest.approx(1.0)


def test_gate_refusal_stops_unless_the_last_step_was_done(fmu: FmuFactory) -> None:
    columns, stopped = oms_engine._step(
        FakeInstance(h=0.1), [], _bb_spec(fmu), lambda t, _live: t < 0.3, None
    )
    assert stopped and len(columns[0]) == 4 and columns[0][-1] == pytest.approx(0.3)
    _, stopped = oms_engine._step(
        FakeInstance(h=0.1), [], _bb_spec(fmu), lambda t, _live: t < 0.95, None
    )
    assert not stopped  # refused only after the final step: the run is complete


def test_a_step_without_progress_raises(fmu: FmuFactory) -> None:
    inst = FakeInstance(h=0.1, stall_at=0.55)
    with pytest.raises(RuntimeError, match=r"no progress at t=0\.6"):
        oms_engine._step(inst, [], _bb_spec(fmu), lambda t, _live: True, None)


def test_a_clock_not_at_start_time_raises(fmu: FmuFactory) -> None:
    with pytest.raises(RuntimeError, match="start the run at 0"):
        oms_engine._step(
            FakeInstance(h=0.1), [], _bb_spec(fmu, start=0.5), lambda t, _live: True, None
        )


def test_set_parameter_accepts_ok_and_refuses_warnings_and_other_statuses() -> None:
    inst = FakeInstance(h=0.1)
    oms_engine._set_parameter(inst, "cref", "B.e", 0.5, OK)
    inst.warn = True
    with pytest.raises(RuntimeError, match=r"did not set B\.x = 1\.0: Variable 'x' is local"):
        oms_engine._set_parameter(inst, "cref", "B.x", 1.0, OK)
    inst.warn, inst.set_result = False, "Status.error"
    with pytest.raises(RuntimeError, match=r"status Status\.error"):
        oms_engine._set_parameter(inst, "cref", "B.e", 0.5, OK)


def test_recordable_outputs_drop_strings_binaries_and_duplicates(
    fmu: FmuFactory, tmp_path: Path
) -> None:
    fmus = tmp_path / SYSTEM_FMUS_DIR
    fmus.mkdir()
    feedthrough, ball = fmu("3.0", "Feedthrough"), fmu("2.0", "BouncingBall")
    shutil.copy2(feedthrough, fmus / "F.fmu")
    shutil.copy2(ball, fmus / "B.fmu")
    requested = [
        "B.h",
        "F.String_output",
        "F.Binary_output",
        "F.Int32_output",
        "B.h",
        "B.v",
        "F.Float64_continuous_output",
    ]
    spec = _spec({"F": feedthrough, "B": ball}, requested)
    outputs, units = oms_engine._recordable_outputs(spec, fmus)
    assert outputs == ["B.h", "F.Int32_output", "B.v", "F.Float64_continuous_output"]
    assert units == {
        "B.h": "m",
        "F.Int32_output": None,
        "B.v": "m/s",
        "F.Float64_continuous_output": None,
    }


def test_result_is_time_then_outputs_as_float64() -> None:
    from array import array

    columns = [array("d", [0.0, 0.1]), array("d", [1.0, 2.0]), array("d", [3.0, 4.0])]
    result = oms_engine._result(["A.y", "B.x"], columns)
    assert result.dtype.names == ("time", "A.y", "B.x")
    assert all(result.dtype[name] == np.float64 for name in result.dtype.names)
    assert result["B.x"].tolist() == [3.0, 4.0]
    empty = oms_engine._result([], [array("d")])
    assert empty.dtype.names == ("time",) and len(empty) == 0


def test_split_keeps_dots_in_variable_names() -> None:
    assert oms_engine._split("PI.u") == ("PI", "u")
    assert oms_engine._split("body.frame_a.r_0[1]") == ("body", "frame_a.r_0[1]")
