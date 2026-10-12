"""Tests for ``analysis/summary.py``: significant-digit rounding and per-variable statistics."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from agentic_fmi.analysis.summary import round_sig, summarize
from agentic_fmi.store.protocol import RunState, RunSummary


def _result(**columns: np.ndarray) -> np.ndarray:
    n = len(next(iter(columns.values())))
    dtype = [("time", np.float64), *((name, col.dtype) for name, col in columns.items())]
    result = np.empty(n, dtype=dtype)
    result["time"] = np.linspace(0.0, 1.0, n)
    for name, column in columns.items():
        result[name] = column
    return result


# --- round_sig --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (123456.789, 123457.0),
        (0.000123456789, 0.000123457),
        (-9.87654321, -9.87654),
        (1e300, 1e300),
        (1.23456789e-300, 1.23457e-300),
        (0.0, 0.0),
        (5, 5.0),
    ],
)
def test_round_sig_keeps_six_significant_digits(value: float, expected: float) -> None:
    assert round_sig(value) == pytest.approx(expected, rel=1e-12)


def test_round_sig_digits_argument() -> None:
    assert round_sig(math.pi, 3) == 3.14
    assert round_sig(math.pi, 1) == 3.0


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, np.nan, np.float64("inf")])
def test_round_sig_maps_non_finite_to_none(value: float) -> None:
    assert round_sig(value) is None


def test_round_sig_accepts_numpy_scalars_and_returns_python_floats() -> None:
    rounded = round_sig(np.float32(0.1))
    assert type(rounded) is float
    assert rounded == pytest.approx(0.1, rel=1e-6)


def test_round_sig_normalizes_negative_zero() -> None:
    assert math.copysign(1.0, round_sig(-0.0) or 0.0) == 1.0


# --- summarize --------------------------------------------------------------------------------


def test_summarize_min_max_final_and_times() -> None:
    result = _result(h=np.array([1.0, 5.0, -2.0, 3.0, 0.5]))
    (summary,) = summarize(result, {"h": "m"})
    assert summary.name == "h"
    assert summary.unit == "m"
    assert (summary.min, summary.max, summary.final) == (-2.0, 5.0, 0.5)
    assert summary.min_at == 0.5
    assert summary.max_at == 0.25


def test_summarize_reports_the_first_extremum() -> None:
    result = _result(x=np.array([2.0, 7.0, 7.0, 2.0]))
    (summary,) = summarize(result)
    assert summary.max_at == pytest.approx(1 / 3, rel=1e-5)
    assert summary.min_at == 0.0


def test_summarize_keeps_column_order_and_skips_time() -> None:
    result = _result(b=np.arange(3.0), a=np.arange(3.0), c=np.arange(3.0))
    assert [s.name for s in summarize(result)] == ["b", "a", "c"]


def test_summarize_unit_defaults_to_none() -> None:
    result = _result(a=np.arange(3.0), b=np.arange(3.0))
    summaries = summarize(result, {"a": "m"})
    assert [s.unit for s in summaries] == ["m", None]


def test_summarize_booleans_and_integers_count_as_numbers() -> None:
    result = _result(
        flag=np.array([False, True, True, False]), n=np.array([1, 2, 3, 4], dtype=np.int32)
    )
    flag, count = summarize(result)
    assert (flag.min, flag.max, flag.final) == (0.0, 1.0, 0.0)
    assert (count.min, count.max, count.final) == (1.0, 4.0, 4.0)


def test_summarize_ignores_nan_in_min_max_but_final_is_the_last_row() -> None:
    result = _result(x=np.array([np.nan, 2.0, 1.0, np.nan]))
    (summary,) = summarize(result)
    assert (summary.min, summary.max) == (1.0, 2.0)
    assert summary.final is None
    assert summary.min_at == pytest.approx(2 / 3, rel=1e-5)


def test_summarize_all_non_finite_column_is_all_nulls() -> None:
    result = _result(x=np.array([np.nan, np.inf, -np.inf]))
    (summary,) = summarize(result)
    assert summary.model_dump(exclude={"name", "unit"}) == {
        "min": None,
        "max": None,
        "final": None,
        "min_at": None,
        "max_at": None,
    }


def test_summarize_rounds_to_the_requested_digits() -> None:
    result = _result(x=np.array([1.23456789, 9.87654321]))
    (summary,) = summarize(result, digits=3)
    assert (summary.min, summary.max, summary.final) == (1.23, 9.88, 9.88)
    (default,) = summarize(result)
    assert default.min == 1.23457


def test_summarize_single_row() -> None:
    result = _result(x=np.array([4.0]))
    (summary,) = summarize(result)
    assert (summary.min, summary.max, summary.final) == (4.0, 4.0, 4.0)


def test_summarize_empty_result_gives_null_summaries() -> None:
    (summary,) = summarize(_result(x=np.empty(0)))
    assert summary.min is None
    assert summary.final is None


def test_summarize_output_is_json_safe_and_validates_in_a_run_summary() -> None:
    result = _result(x=np.array([np.nan, 1.0, np.inf, 2.0]), y=np.array([1e-9, 3.0, 2.0, 1.0]))
    variables = summarize(result)
    document = RunSummary(
        run_id="r1",
        state=RunState.FINISHED,
        t_end=1.0,
        n_points=4,
        wall_time_s=0.1,
        columns=["x", "y"],
        variables=variables,
    )
    text = document.model_dump_json()
    json.loads(text, parse_constant=lambda name: pytest.fail(f"non-finite constant {name}"))
