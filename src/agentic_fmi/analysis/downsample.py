"""Largest-Triangle-Three-Buckets (LTTB) downsampling that keeps what matters to the eye and to
Claude: the endpoints and the extrema.

Collaborators: ``runs.py`` (``run_results``), ``plots.py`` callers (to bound plotted points).
"""

from __future__ import annotations

import numpy as np

_MIN_POINTS = 4
"""The smallest ``n_out``: both endpoints plus the minimum and maximum."""


def lttb_indices(t: np.ndarray, y: np.ndarray, n_out: int) -> np.ndarray:
    """Sorted, unique indices of at most ``n_out`` points chosen by LTTB.

    Guarantees: the first and last index, and the first argmin and argmax of the finite ``y``
    values, are always included (they replace the bucket picks nearest to them). Duplicate
    times (events) are allowed; a zero-width triangle simply scores zero. NaN values are never
    picked unless all are NaN. If ``n_out >= len(t)`` all indices are returned.

    Raises:
        ValueError: ``n_out < 4``, or ``t`` and ``y`` differ in length.
    """
    if n_out < _MIN_POINTS:
        raise ValueError(f"n_out must be at least {_MIN_POINTS}, got {n_out}")
    if t.shape != y.shape:
        raise ValueError(f"t and y differ in length: {t.shape} vs {y.shape}")
    n = t.size
    if n_out >= n:
        return np.arange(n)
    finite = np.flatnonzero(np.isfinite(y))
    if finite.size == 0:
        return np.unique(np.linspace(0, n - 1, n_out).round().astype(np.intp))
    # A non-finite first or last value is still returned, so it takes one of the n_out slots.
    missing = [i for i in (0, n - 1) if i not in (finite[0], finite[-1])]
    picks = _lttb_finite(t[finite], y[finite], n_out - len(missing), _kept_ends(finite, n))
    return np.unique(np.concatenate([finite[picks], np.array(missing, dtype=np.intp)]))


def _kept_ends(finite: np.ndarray, n: int) -> set[int]:
    """Positions among the finite points that are the first or last index of the series."""
    ends = {0} if finite[0] == 0 else set()
    if finite[-1] == n - 1:
        ends.add(finite.size - 1)
    return ends


def _lttb_finite(t: np.ndarray, y: np.ndarray, n_out: int, protected: set[int]) -> np.ndarray:
    """LTTB on the finite points, then force the extrema in; returns positions into ``t``/``y``.

    ``protected`` holds positions that may not be replaced (the series' own endpoints).
    """
    if t.size <= n_out:
        return np.arange(t.size)
    picks = _lttb_core(t, y, n_out) if n_out > 2 else np.array([0, t.size - 1])
    for extreme in (int(np.argmin(y)), int(np.argmax(y))):
        picks = _force_in(picks, extreme, protected)
    return picks


def _force_in(picks: np.ndarray, position: int, protected: set[int]) -> np.ndarray:
    """Make sure ``position`` is picked, replacing the nearest pick that isn't protected.

    ``protected`` grows by ``position`` so a later extreme can't push it out again.
    """
    protected.add(position)
    if position in picks:
        return picks
    candidates = [i for i, pick in enumerate(picks) if int(pick) not in protected]
    nearest = min(candidates, key=lambda i: abs(int(picks[i]) - position))
    replaced = picks.copy()
    replaced[nearest] = position
    return np.sort(replaced)


def _lttb_core(t: np.ndarray, y: np.ndarray, n_out: int) -> np.ndarray:
    """Plain LTTB: the endpoints plus one pick per bucket. Needs ``t.size > n_out >= 3``."""
    n_buckets = n_out - 2
    edges = (1 + np.arange(n_buckets + 1) * ((t.size - 2) / n_buckets)).astype(np.intp)
    edges[-1] = t.size - 1
    picks = np.empty(n_out, dtype=np.intp)
    picks[0], picks[-1] = 0, t.size - 1
    anchor = 0
    for bucket in range(n_buckets):
        lo, hi = edges[bucket], edges[bucket + 1]
        if bucket + 1 < n_buckets:
            next_lo, next_hi = edges[bucket + 1], edges[bucket + 2]
            target_t, target_y = t[next_lo:next_hi].mean(), y[next_lo:next_hi].mean()
        else:
            target_t, target_y = t[-1], y[-1]
        areas = np.abs(
            (t[anchor] - target_t) * (y[lo:hi] - y[anchor])
            - (t[anchor] - t[lo:hi]) * (target_y - y[anchor])
        )
        anchor = lo + int(np.argmax(areas))
        picks[bucket + 1] = anchor
    return picks
