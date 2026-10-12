"""Per-variable statistics of a result array, and the rounding every tool output uses.

Collaborators: ``worker.py`` writes ``summary.json`` with :func:`summarize`; ``runs.py`` and
``analysis/compare.py`` use :func:`round_sig` for every float they return.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

from agentic_fmi.store.protocol import VariableSummary

TIME_COLUMN = "time"
"""The first column of every result array."""


def round_sig(value: float, digits: int = 6) -> float | None:
    """Round to ``digits`` significant digits; ``None`` for NaN or infinity."""
    number = float(value)
    if not math.isfinite(number):
        return None
    return float(f"{number:.{digits - 1}e}") + 0.0  # + 0.0 turns -0.0 into 0.0


def summarize(
    result: np.ndarray, units: Mapping[str, str | None] | None = None, digits: int = 6
) -> list[VariableSummary]:
    """One :class:`VariableSummary` per non-time column, in column order.

    min, max, final and the times of the first min and max, rounded with :func:`round_sig`.
    Booleans count as 0/1. Columns that are all non-finite get nulls.
    """
    names = result.dtype.names or ()
    time = result[TIME_COLUMN].astype(np.float64)
    unit_of = units or {}
    return [
        _summarize_column(name, time, result[name], unit_of.get(name), digits)
        for name in names
        if name != TIME_COLUMN
    ]


def _summarize_column(
    name: str, time: np.ndarray, column: np.ndarray, unit: str | None, digits: int
) -> VariableSummary:
    values = column.astype(np.float64)
    finite = np.flatnonzero(np.isfinite(values))
    if finite.size == 0:
        return VariableSummary(name=name, unit=unit, final=_final(values, digits))
    lowest = finite[int(np.argmin(values[finite]))]
    highest = finite[int(np.argmax(values[finite]))]
    return VariableSummary(
        name=name,
        unit=unit,
        min=round_sig(values[lowest], digits),
        max=round_sig(values[highest], digits),
        final=_final(values, digits),
        min_at=round_sig(time[lowest], digits),
        max_at=round_sig(time[highest], digits),
    )


def _final(values: np.ndarray, digits: int) -> float | None:
    return round_sig(values[-1], digits) if values.size else None
