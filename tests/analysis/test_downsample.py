"""Tests for ``analysis/downsample.py``: LTTB keeps the endpoints and the global extrema."""

from __future__ import annotations

import numpy as np
import pytest

from agentic_fmi.analysis.downsample import lttb_indices


def _signal(n: int = 5000) -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, 10.0, n)
    y = np.sin(3 * t) + 0.3 * np.sin(41 * t)
    return t, y


def _check_invariants(t: np.ndarray, y: np.ndarray, n_out: int) -> np.ndarray:
    keep = lttb_indices(t, y, n_out)
    assert len(keep) <= n_out
    assert np.all(np.diff(keep) > 0), "indices must be sorted and unique"
    assert keep[0] == 0
    assert keep[-1] == len(t) - 1
    finite = np.flatnonzero(np.isfinite(y))
    if finite.size:
        assert finite[np.argmin(y[finite])] in keep
        assert finite[np.argmax(y[finite])] in keep
    return keep


@pytest.mark.parametrize("n_out", [4, 5, 10, 100, 500])
def test_keeps_endpoints_and_extrema(n_out: int) -> None:
    t, y = _signal()
    keep = _check_invariants(t, y, n_out)
    assert len(keep) == n_out


def test_spike_is_never_lost() -> None:
    t = np.linspace(0.0, 1.0, 10_000)
    y = np.zeros_like(t)
    y[7_321] = 50.0
    y[2_222] = -30.0
    keep = _check_invariants(t, y, 100)
    assert 7_321 in keep
    assert 2_222 in keep


def test_returns_everything_when_n_out_is_large_enough() -> None:
    t, y = _signal(50)
    assert list(lttb_indices(t, y, 50)) == list(range(50))
    assert list(lttb_indices(t, y, 500)) == list(range(50))


def test_minimum_request_gives_endpoints_and_extrema() -> None:
    t, y = _signal(1000)
    keep = _check_invariants(t, y, 4)
    assert set(keep) == {0, 999, int(np.argmin(y)), int(np.argmax(y))}


def test_constant_series_keeps_endpoints() -> None:
    t = np.linspace(0.0, 1.0, 1000)
    keep = _check_invariants(t, np.full_like(t, 3.0), 20)
    assert len(keep) <= 20


def test_duplicate_times_are_tolerated() -> None:
    """A bouncing ball has events: the same time twice with a jump in the value."""
    t = np.repeat(np.linspace(0.0, 10.0, 1000), 2)
    y = np.where(np.arange(t.size) % 2 == 0, 0.0, 1.0) + np.sin(t)
    keep = _check_invariants(t, y, 100)
    assert len(keep) == 100


def test_all_times_equal() -> None:
    t = np.zeros(300)
    y = np.arange(300, dtype=float)
    _check_invariants(t, y, 10)


def test_nan_values_are_never_picked() -> None:
    t, y = _signal(2000)
    y[100:400] = np.nan
    y[1500] = np.nan
    keep = _check_invariants(t, y, 100)
    inside = keep[(keep >= 100) & (keep < 400)]
    assert inside.size == 0
    assert 1500 not in keep


def test_nan_at_the_ends_still_returns_the_endpoints() -> None:
    t, y = _signal(2000)
    y[:10] = np.nan
    y[-5:] = np.nan
    keep = _check_invariants(t, y, 50)
    assert len(keep) <= 50


def test_nan_at_both_ends_with_the_smallest_budget() -> None:
    t, y = _signal(500)
    y[0] = np.nan
    y[-1] = np.nan
    keep = _check_invariants(t, y, 4)
    assert len(keep) == 4


def test_infinite_values_are_ignored_like_nan() -> None:
    t, y = _signal(1000)
    y[500] = np.inf
    y[501] = -np.inf
    keep = _check_invariants(t, y, 40)
    assert 500 not in keep
    assert 501 not in keep


def test_all_nan_still_returns_spread_indices_with_endpoints() -> None:
    t = np.linspace(0.0, 1.0, 1000)
    keep = lttb_indices(t, np.full_like(t, np.nan), 10)
    assert keep[0] == 0
    assert keep[-1] == 999
    assert len(keep) == 10


def test_single_finite_value_among_nan() -> None:
    t = np.linspace(0.0, 1.0, 100)
    y = np.full_like(t, np.nan)
    y[40] = 2.0
    keep = lttb_indices(t, y, 10)
    assert set(keep) == {0, 40, 99}


def test_rejects_too_few_points() -> None:
    t, y = _signal(100)
    with pytest.raises(ValueError, match="at least 4"):
        lttb_indices(t, y, 3)


def test_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="differ in length"):
        lttb_indices(np.arange(10.0), np.arange(11.0), 5)


def test_empty_input_is_returned_as_is() -> None:
    assert lttb_indices(np.empty(0), np.empty(0), 10).size == 0
