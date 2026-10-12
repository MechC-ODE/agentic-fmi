"""Tests for ``engines/fmpy_engine.py``: one batch simulation of an extracted Reference FMU.

Collaborators: ``agentic_fmi.engines.fmpy_engine`` (under test), the ``extracted`` and
``engine_spec`` fixtures (``tests/engines/conftest.py``) and the shared ``fmu`` fixture. The
engine loads FMU binaries, which is allowed here: the tests run it on private copies, never in
the MCP server. Worker-level behaviour (timeout, signals, files) is in ``tests/worker/``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from agentic_fmi.engines import fmpy_engine
from agentic_fmi.store.protocol import RunSpec, Step

FmuFactory = Callable[[str, str], Path]
Extract = Callable[[Path], Path]
SpecBuilder = Callable[..., RunSpec]

VARIANTS = [
    ("2.0", "CoSimulation"),
    ("2.0", "ModelExchange"),
    ("3.0", "CoSimulation"),
    ("3.0", "ModelExchange"),
]


def _always(_t: float, _live: object) -> bool:
    return True


def _first_apex(result: np.ndarray) -> float:
    """Height of the first bounce apex: the first interior local maximum of ``h``."""
    h = result["h"]
    peaks = [i for i in range(1, len(h) - 1) if h[i] > h[i - 1] and h[i] >= h[i + 1]]
    assert peaks, "no bounce apex in the result"
    return float(h[peaks[0]])


@pytest.mark.parametrize(("version", "fmi_type"), VARIANTS)
def test_bouncing_ball_runs_to_stop_time(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder, version: str, fmi_type: str
) -> None:
    path = fmu(version, "BouncingBall")
    spec = engine_spec(path, fmi_type, outputs=["v", "h"])
    outcome = fmpy_engine.simulate(spec, extracted(path), _always)
    result = outcome.result
    assert result.dtype.names == ("time", "v", "h")  # the requested order, not FMPy's
    assert result["time"][0] == 0.0
    assert result["time"][-1] == pytest.approx(3.0)
    assert result["h"][0] == pytest.approx(1.0)
    assert not outcome.stopped
    assert dict(outcome.units) == {"v": "m/s", "h": "m"}


@pytest.mark.parametrize(("version", "fmi_type"), VARIANTS)
def test_restitution_start_value_changes_apex_height(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder, version: str, fmi_type: str
) -> None:
    path = fmu(version, "BouncingBall")
    default = fmpy_engine.simulate(engine_spec(path, fmi_type), extracted(path), _always)
    softer = fmpy_engine.simulate(
        engine_spec(path, fmi_type, start_values={"e": 0.5}), extracted(path), _always
    )
    # Dropped from h = 1, the first apex is e**2: 0.49 by default, 0.25 with e = 0.5.
    assert _first_apex(default.result) == pytest.approx(0.49, abs=0.02)
    assert _first_apex(softer.result) == pytest.approx(0.25, abs=0.02)


def test_gate_returning_false_stops_before_stop_time(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("3.0", "BouncingBall")
    seen: list[float] = []

    def gate(t: float, _live: object) -> bool:
        seen.append(t)
        return t < 1.0

    outcome = fmpy_engine.simulate(engine_spec(path, "CoSimulation"), extracted(path), gate)
    assert outcome.stopped
    assert seen == sorted(seen) and seen[-1] >= 1.0
    assert 0.9 < outcome.result["time"][-1] <= seen[-1] + 1e-9


def test_gate_refusing_only_at_stop_time_is_not_a_stop(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("2.0", "BouncingBall")
    outcome = fmpy_engine.simulate(
        engine_spec(path, "CoSimulation"), extracted(path), lambda t, _live: t < 3.0 - 1e-9
    )
    assert not outcome.stopped
    assert outcome.result["time"][-1] == pytest.approx(3.0)


@pytest.mark.parametrize(("version", "fmi_type"), VARIANTS)
def test_step_input_shows_up_in_feedthrough_output(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder, version: str, fmi_type: str
) -> None:
    path = fmu(version, "Feedthrough")
    spec = engine_spec(
        path,
        fmi_type,
        outputs=["Float64_continuous_output"],
        inputs={"Float64_continuous_input": Step(time=1.0, before=0.5, after=2.5)},
    )
    result = fmpy_engine.simulate(spec, extracted(path), _always).result
    time, output = result["time"], result["Float64_continuous_output"]
    assert np.all(output[time < 0.99] == 0.5)
    assert np.all(output[time > 1.01] == 2.5)


def test_outputs_keep_order_and_types_and_skip_strings(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("3.0", "Feedthrough")
    outputs = ["Int32_output", "String_output", "Boolean_output", "Float64_continuous_output"]
    outcome = fmpy_engine.simulate(engine_spec(path, outputs=outputs), extracted(path), _always)
    dtype = outcome.result.dtype
    assert dtype.names == ("time", "Int32_output", "Boolean_output", "Float64_continuous_output")
    assert dtype["Int32_output"] == np.int32
    assert dtype["Boolean_output"] == np.bool_
    assert set(outcome.units) == set(dtype.names[1:])


def test_array_output_becomes_one_column_per_element(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("3.0", "StateSpace")
    outcome = fmpy_engine.simulate(engine_spec(path, outputs=["y"]), extracted(path), _always)
    assert outcome.result.dtype.names == ("time", "y[1]", "y[2]", "y[3]")
    assert [outcome.result[f"y[{i}]"][0] for i in (1, 2, 3)] == [1.0, 2.0, 3.0]


def test_empty_outputs_record_time_only(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("2.0", "BouncingBall")
    outcome = fmpy_engine.simulate(engine_spec(path, outputs=[]), extracted(path), _always)
    assert outcome.result.dtype.names == ("time",)
    assert len(outcome.result) > 1


def test_euler_solver_with_fixed_step_runs_model_exchange(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("3.0", "VanDerPol")
    spec = engine_spec(
        path, "ModelExchange", solver="Euler", step_size=1e-3, output_interval=0.01, stop_time=1
    )
    result = fmpy_engine.simulate(spec, extracted(path), _always).result
    assert result["time"][-1] == pytest.approx(1.0)
    assert np.all(np.isfinite(result["x0"]))


def test_fmpy_errors_propagate(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder
) -> None:
    path = fmu("2.0", "BouncingBall")
    spec = engine_spec(path, start_values={"no_such_variable": 1.0})
    with pytest.raises(Exception, match="no_such_variable"):
        fmpy_engine.simulate(spec, extracted(path), _always)


def test_simulate_fmu_gets_the_spec_and_never_timeout_or_remoting(
    fmu: FmuFactory, extracted: Extract, engine_spec: SpecBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fmu("3.0", "BouncingBall")
    fmu_dir = extracted(path)
    spec = engine_spec(
        path,
        "ModelExchange",
        outputs=["h", "h"],
        start_values={"e": 0.6},
        relative_tolerance=1e-6,
        step_size=1e-3,
    )
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_simulate_fmu(*args: Any, **kwargs: Any) -> np.ndarray:
        calls.append((args, kwargs))
        return np.array([(0.0, 1.0)], dtype=[("time", np.float64), ("h", np.float64)])

    monkeypatch.setattr(fmpy_engine, "simulate_fmu", fake_simulate_fmu)
    fmpy_engine.simulate(spec, fmu_dir, _always)
    [(args, kwargs)] = calls
    experiment = spec.experiment
    assert args == (str(fmu_dir),)
    assert kwargs["remote_platform"] is None
    assert kwargs.get("timeout") is None
    assert kwargs["fmi_type"] == "ModelExchange" and kwargs["solver"] == "CVode"
    assert (kwargs["start_time"], kwargs["stop_time"]) == (0.0, 3.0)
    assert kwargs["output_interval"] == experiment.output_interval
    assert (kwargs["relative_tolerance"], kwargs["step_size"]) == (1e-6, 1e-3)
    assert kwargs["start_values"] == {"e": 0.6}
    assert kwargs["output"] == ["h"]
    assert kwargs["input"] is None
    assert kwargs["fmu_instance"] is None  # batch runs: FMPy makes and frees the instance
    assert kwargs["step_finished"](0.5, SimpleNamespace(fmu=None)) is True


def test_simulation_creates_no_temp_dirs(
    fmu: FmuFactory,
    extracted: Extract,
    engine_spec: SpecBuilder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = fmu("2.0", "BouncingBall")
    fmu_dir = extracted(path)
    private = tmp_path / "tmp"  # where tempfile.mkdtemp (and FMPy) would put theirs; not the
    private.mkdir()  # shared /tmp, so other processes using it can't fail this test
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    fmpy_engine.simulate(engine_spec(path, "ModelExchange"), fmu_dir, _always)
    with pytest.raises(Exception, match="nope"):
        fmpy_engine.simulate(engine_spec(path, start_values={"nope": 1}), fmu_dir, _always)
    assert list(private.iterdir()) == []
