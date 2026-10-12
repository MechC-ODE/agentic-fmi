"""Tests for ``analysis/compare.py``: spec differences and series comparison across time grids."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from agentic_fmi.analysis.compare import (
    Comparison,
    compare_runs,
    diff_specs,
    series_difference,
)
from agentic_fmi.errors import UserError
from agentic_fmi.store.protocol import (
    Constant,
    Experiment,
    FmuRef,
    InputSignal,
    RunSpec,
    Step,
    Versions,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _spec(
    run_id: str,
    *,
    sha: str = SHA_A,
    fmi_type: str = "CoSimulation",
    stop_time: float = 3.0,
    start_values: dict[str, bool | int | float | str] | None = None,
    inputs: dict[str, InputSignal] | None = None,
    numpy_version: str = "2.0.0",
    created_at: datetime | None = None,
    outputs: list[str] | None = None,
) -> RunSpec:
    return RunSpec.model_validate(
        {
            "run_id": run_id,
            "created_at": created_at or datetime(2026, 1, 1, tzinfo=UTC),
            "fmu": FmuRef(
                path="/ws/BouncingBall.fmu",
                sha256=sha,
                size_bytes=1000 if sha == SHA_A else 2000,
                fmi_version="2.0",
                model_name="BouncingBall",
            ),
            "fmi_type": fmi_type,
            "experiment": Experiment(start_time=0.0, stop_time=stop_time, output_interval=0.1),
            "start_values": start_values or {},
            "inputs": inputs or {},
            "outputs": outputs or ["h"],
            "timeout_s": 60.0,
            "estimated_steps": 100.0 * stop_time,
            "versions": Versions(
                agentic_fmi="0.1.0",
                fmpy="0.3.32",
                numpy=numpy_version,
                python="3.12.0",
                platform="x",
            ),
        }
    )


def _result(t: np.ndarray, **columns: np.ndarray) -> np.ndarray:
    dtype = [("time", np.float64), *((name, np.float64) for name in columns)]
    result = np.empty(t.size, dtype=dtype)
    result["time"] = t
    for name, column in columns.items():
        result[name] = column
    return result


def _differences(comparison: Comparison) -> dict[str, dict[str, object]]:
    return {d.field: dict(d.values) for d in comparison.spec_differences}


# --- diff_specs -------------------------------------------------------------------------------


def test_diff_specs_reports_start_value_differences() -> None:
    specs = [_spec("r1", start_values={"e": 0.5}), _spec("r2", start_values={"e": 0.8})]
    differences = diff_specs(specs)
    assert [d.field for d in differences] == ["start_values.e"]
    assert differences[0].values == {"r1": 0.5, "r2": 0.8}


def test_diff_specs_identical_specs_have_no_differences() -> None:
    assert diff_specs([_spec("r1"), _spec("r2")]) == []


def test_diff_specs_ignores_identity_timestamps_and_derived_fields() -> None:
    later = datetime(2026, 6, 1, tzinfo=UTC)
    specs = [_spec("r1"), _spec("r2", created_at=later, outputs=["h", "v"])]
    assert diff_specs(specs) == []


def test_diff_specs_start_value_missing_in_one_run_is_none() -> None:
    specs = [_spec("r1"), _spec("r2", start_values={"e": 0.8})]
    (difference,) = diff_specs(specs)
    assert difference.values == {"r1": None, "r2": 0.8}


def test_diff_specs_experiment_fmu_and_type() -> None:
    specs = [_spec("r1"), _spec("r2", sha=SHA_B, fmi_type="ModelExchange", stop_time=5.0)]
    fields = [d.field for d in diff_specs(specs)]
    assert "experiment.stop_time" in fields
    assert "fmu.sha256" in fields
    assert "fmi_type" in fields
    assert "fmu.size_bytes" not in fields
    assert "estimated_steps" not in fields


def test_diff_specs_input_signals_are_compared_as_a_whole() -> None:
    specs = [
        _spec("r1", inputs={"u": Constant(value=1.0)}),
        _spec("r2", inputs={"u": Step(time=1.0, after=2.0)}),
        _spec("r3", inputs={"u": Constant(value=1.0)}),
    ]
    (difference,) = diff_specs(specs)
    assert difference.field == "inputs.u"
    assert difference.values["r1"] == difference.values["r3"]
    assert difference.values["r1"] != difference.values["r2"]
    assert isinstance(difference.values["r2"], str)
    assert '"kind":"step"' in str(difference.values["r2"])


def test_diff_specs_lists_tool_version_differences() -> None:
    specs = [_spec("r1"), _spec("r2", numpy_version="2.1.0")]
    (difference,) = diff_specs(specs)
    assert difference.field == "versions.numpy"


def test_diff_specs_does_not_confuse_true_and_one() -> None:
    specs = [_spec("r1", start_values={"flag": True}), _spec("r2", start_values={"flag": 1})]
    assert [d.field for d in diff_specs(specs)] == ["start_values.flag"]
    same = [_spec("r1", start_values={"x": 1}), _spec("r2", start_values={"x": 1.0})]
    assert diff_specs(same) == []


# --- series_difference ------------------------------------------------------------------------


def test_series_difference_identical_series_is_zero() -> None:
    t = np.linspace(0.0, 1.0, 11)
    assert series_difference(t, t**2, t, t**2) == (0.0, 0.0)


def test_series_difference_constant_offset() -> None:
    t = np.linspace(0.0, 1.0, 11)
    max_abs, rms = series_difference(t, t, t, t + 0.5)
    assert max_abs == pytest.approx(0.5)
    assert rms == pytest.approx(0.5)


def test_series_difference_interpolates_onto_the_base_grid() -> None:
    base_t = np.linspace(0.0, 1.0, 11)
    other_t = np.linspace(0.0, 1.0, 4)  # a much coarser grid
    max_abs, rms = series_difference(base_t, 2 * base_t, other_t, 2 * other_t)
    assert max_abs == pytest.approx(0.0, abs=1e-12)
    assert rms == pytest.approx(0.0, abs=1e-12)


def test_series_difference_uses_only_the_overlapping_range() -> None:
    base_t = np.linspace(0.0, 10.0, 101)
    other_t = np.linspace(0.0, 5.0, 51)
    max_abs, _ = series_difference(base_t, np.zeros(101), other_t, np.ones(51))
    assert max_abs == pytest.approx(1.0)
    # The base is much larger after t=5; had that been compared against a held value, the
    # difference would not be 1.
    max_abs, _ = series_difference(base_t, base_t, other_t, other_t)
    assert max_abs == pytest.approx(0.0, abs=1e-12)


def test_series_difference_no_overlap_is_none() -> None:
    a = np.linspace(0.0, 1.0, 5)
    b = np.linspace(2.0, 3.0, 5)
    assert series_difference(a, a, b, b) == (None, None)


def test_series_difference_empty_series_is_none() -> None:
    t = np.linspace(0.0, 1.0, 5)
    assert series_difference(np.empty(0), np.empty(0), t, t) == (None, None)


def test_series_difference_collapses_duplicate_times_to_the_post_event_value() -> None:
    # The other run has a jump at t=1 (0 before, 10 after); the base samples t=1 once.
    other_t = np.array([0.0, 1.0, 1.0, 2.0])
    other_y = np.array([0.0, 0.0, 10.0, 10.0])
    base_t = np.array([0.0, 1.0, 2.0])
    max_abs, _ = series_difference(base_t, np.array([0.0, 10.0, 10.0]), other_t, other_y)
    assert max_abs == pytest.approx(0.0, abs=1e-12)


def test_series_difference_collapses_duplicate_base_times_too() -> None:
    base_t = np.array([0.0, 1.0, 1.0, 2.0])
    base_y = np.array([0.0, 0.0, 10.0, 10.0])
    other_t = np.array([0.0, 1.0, 2.0])
    max_abs, _ = series_difference(base_t, base_y, other_t, np.array([0.0, 10.0, 10.0]))
    assert max_abs == pytest.approx(0.0, abs=1e-12)


def test_series_difference_skips_non_finite_samples() -> None:
    t = np.linspace(0.0, 1.0, 5)
    y = np.array([0.0, np.nan, 0.0, 0.0, 0.0])
    other = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    max_abs, rms = series_difference(t, y, t, other)
    assert max_abs == pytest.approx(1.0)
    assert rms == pytest.approx(1.0)


def test_series_difference_all_nan_is_none() -> None:
    t = np.linspace(0.0, 1.0, 5)
    assert series_difference(t, np.full(5, np.nan), t, np.zeros(5)) == (None, None)


# --- compare_runs -----------------------------------------------------------------------------


def test_compare_runs_with_different_time_grids_and_specs() -> None:
    cs_t = np.linspace(0.0, 3.0, 31)  # Co-Simulation: output_interval 0.1
    me_t = np.linspace(0.0, 3.0, 301)  # Model Exchange: finer, plus an event
    specs = [
        _spec("r1", start_values={"e": 0.5}),
        _spec("r2", fmi_type="ModelExchange", start_values={"e": 0.8}),
    ]
    results = {"r1": _result(cs_t, h=cs_t), "r2": _result(me_t, h=me_t + 0.25)}
    comparison = compare_runs(specs, results, ["h"], {"h": "m"})
    assert comparison.baseline == "r1"
    assert comparison.runs == ["r1", "r2"]
    assert _differences(comparison) == {
        "fmi_type": {"r1": "CoSimulation", "r2": "ModelExchange"},
        "start_values.e": {"r1": 0.5, "r2": 0.8},
    }
    (variable,) = comparison.variables
    assert variable.name == "h"
    assert variable.unit == "m"
    base, other = variable.runs
    assert (base.run_id, base.max_abs_diff, base.rms_diff) == ("r1", None, None)
    assert (base.min, base.max, base.final) == (0.0, 3.0, 3.0)
    assert other.max_abs_diff == pytest.approx(0.25, rel=1e-5)
    assert other.rms_diff == pytest.approx(0.25, rel=1e-5)
    assert (other.min, other.max, other.final) == (0.25, 3.25, 3.25)
    assert any("different time grid" in note for note in comparison.notes)


def test_compare_runs_same_grid_has_no_grid_note() -> None:
    t = np.linspace(0.0, 3.0, 31)
    specs = [_spec("r1"), _spec("r2")]
    results = {"r1": _result(t, h=t), "r2": _result(t.copy(), h=t)}
    assert compare_runs(specs, results, ["h"]).notes == []


def test_compare_runs_notes_a_different_time_range() -> None:
    specs = [_spec("r1"), _spec("r2", stop_time=1.5)]
    results = {
        "r1": _result(np.linspace(0.0, 3.0, 31), h=np.linspace(0.0, 3.0, 31)),
        "r2": _result(np.linspace(0.0, 1.5, 16), h=np.linspace(0.0, 1.5, 16)),
    }
    comparison = compare_runs(specs, results, ["h"])
    assert any("overlap" in note and "r2" in note for note in comparison.notes)
    assert comparison.variables[0].runs[1].max_abs_diff == pytest.approx(0.0, abs=1e-12)


def test_compare_runs_notes_different_fmus() -> None:
    t = np.linspace(0.0, 1.0, 11)
    specs = [_spec("r1"), _spec("r2", sha=SHA_B)]
    results = {"r1": _result(t, h=t), "r2": _result(t, h=t)}
    comparison = compare_runs(specs, results, ["h"])
    assert any("different FMUs" in note for note in comparison.notes)
    assert "fmu.sha256" in _differences(comparison)


def test_compare_runs_variable_missing_in_one_run_is_noted_not_failed() -> None:
    t = np.linspace(0.0, 1.0, 11)
    specs = [_spec("r1"), _spec("r2")]
    results = {"r1": _result(t, h=t, v=t), "r2": _result(t, h=t)}
    comparison = compare_runs(specs, results, ["h", "v"])
    by_name = {v.name: v for v in comparison.variables}
    assert [r.run_id for r in by_name["v"].runs] == ["r1"]
    assert [r.run_id for r in by_name["h"].runs] == ["r1", "r2"]
    assert any("'v' is missing in r2" in note for note in comparison.notes)


def test_compare_runs_variable_in_no_run_is_a_note() -> None:
    t = np.linspace(0.0, 1.0, 11)
    specs = [_spec("r1"), _spec("r2")]
    results = {"r1": _result(t, h=t), "r2": _result(t, h=t)}
    comparison = compare_runs(specs, results, ["h", "nope"])
    assert [v.name for v in comparison.variables] == ["h"]
    assert any("'nope'" in note for note in comparison.notes)


def test_compare_runs_baseline_missing_the_variable_uses_the_first_run_with_it() -> None:
    t = np.linspace(0.0, 1.0, 11)
    specs = [_spec("r1"), _spec("r2"), _spec("r3")]
    results = {"r1": _result(t, h=t), "r2": _result(t, h=t, v=t), "r3": _result(t, h=t, v=t + 1)}
    comparison = compare_runs(specs, results, ["v"])
    runs = comparison.variables[0].runs
    assert [r.run_id for r in runs] == ["r2", "r3"]
    assert runs[0].max_abs_diff is None
    assert runs[1].max_abs_diff == pytest.approx(1.0)


def test_compare_runs_rounds_and_serializes_without_non_finite_numbers() -> None:
    t = np.linspace(0.0, 1.0, 11)
    specs = [_spec("r1"), _spec("r2")]
    results = {"r1": _result(t, h=t / 3), "r2": _result(t, h=np.where(t > 0.5, np.nan, t / 7))}
    comparison = compare_runs(specs, results, ["h"], digits=3)
    text = comparison.model_dump_json()
    assert "NaN" not in text
    assert "Infinity" not in text
    other = comparison.variables[0].runs[1]
    assert other.final is None
    assert other.max == pytest.approx(0.5 / 7, rel=1e-2)
    assert len(str(other.max_abs_diff).replace(".", "").lstrip("0").split("e")[0]) <= 3


def test_compare_runs_single_run() -> None:
    t = np.linspace(0.0, 1.0, 11)
    comparison = compare_runs([_spec("r1")], {"r1": _result(t, h=t)}, ["h"])
    assert comparison.baseline == "r1"
    assert comparison.spec_differences == []
    assert comparison.variables[0].runs[0].max_abs_diff is None


def test_compare_runs_without_specs_is_a_user_error() -> None:
    with pytest.raises(UserError, match="at least one run"):
        compare_runs([], {}, ["h"])


def test_compare_runs_run_without_result_is_a_user_error() -> None:
    t = np.linspace(0.0, 1.0, 11)
    with pytest.raises(UserError, match="r2"):
        compare_runs([_spec("r1"), _spec("r2")], {"r1": _result(t, h=t)}, ["h"])
