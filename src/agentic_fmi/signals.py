"""The inputs DSL of ``run_start``: check signal names and turn signals into FMPy's input array.

Signals are the protocol models ``Constant``, ``Step``, ``Ramp`` and ``Table``
(``store/protocol.py``), stored in ``spec.json`` exactly as given. Pure numpy; no FMPy import.

Every signal is first reduced to *knots*: ``(time, value just before, value just after)``. Values
are linear between knots (``after`` of one knot to ``before`` of the next) and held outside them,
so a jump is a knot whose two values differ.

Collaborators: ``runs.py`` calls :func:`check_inputs` before a run is created;
``engines/fmpy_engine.py`` calls :func:`to_input_array` inside the worker.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Mapping, Sequence
from typing import NamedTuple

import numpy as np

from agentic_fmi.config import Limits
from agentic_fmi.errors import UserError
from agentic_fmi.store.protocol import Constant, InputSignal, Ramp, Step, Table

_FLOAT_TYPES = frozenset({"Real", "Float32", "Float64"})
_INT_TYPES = frozenset(
    {"Integer", "Enumeration"}
    | {f"{prefix}Int{bits}" for prefix in ("", "U") for bits in (8, 16, 32, 64)}
)
_BOOL_TYPE = "Boolean"
_TIME_FIELD = "time"
_MAX_LISTED_NAMES = 10
_MAX_CLOSE_MATCHES = 3
_MESSAGE_MAX_CHARS = Limits().message_max_chars


class _Knot(NamedTuple):
    """A signal's value just before and just after ``t``."""

    t: float
    before: float
    after: float


def check_inputs(
    inputs: Mapping[str, InputSignal],
    input_names: Sequence[str],
    non_input_variables: Collection[str] = (),
) -> None:
    """Check that every key names an input variable of the FMU.

    ``non_input_variables`` is optional: the FMU's other variable names, so that a signal given
    for a parameter or output gets a pointer to ``start_values`` instead of a plain "unknown".

    Raises:
        UserError: unknown name ("Unknown input 'uu'. Close matches: u. Inputs: u, v.") or a
            variable that exists but isn't an input (suggest start_values for parameters).
    """
    valid = set(input_names)
    problems = [
        _problem(name, input_names, name in non_input_variables)
        for name in inputs
        if name not in valid
    ]
    if problems:
        tail = f"Inputs: {_listing(input_names)}." if input_names else "This FMU has no inputs."
        raise UserError(_clip(" ".join([*problems, tail])))


def _problem(name: str, input_names: Sequence[str], is_variable: bool) -> str:
    """One sentence about a name that is not an input."""
    if is_variable:
        return (
            f"'{name}' is a variable of this FMU but not an input. Set parameters and other "
            "start values with start_values."
        )
    close = difflib.get_close_matches(name, input_names, n=_MAX_CLOSE_MATCHES)
    hint = f" Close matches: {', '.join(close)}." if close else ""
    return f"Unknown input '{name}'.{hint}"


def _listing(names: Sequence[str]) -> str:
    """Comma-separated names, cut after a few."""
    shown = ", ".join(names[:_MAX_LISTED_NAMES])
    extra = len(names) - _MAX_LISTED_NAMES
    return f"{shown}, ... (+{extra} more)" if extra > 0 else shown


def _clip(message: str) -> str:
    if len(message) <= _MESSAGE_MAX_CHARS:
        return message
    return message[: _MESSAGE_MAX_CHARS - 3] + "..."


def to_input_array(
    inputs: Mapping[str, InputSignal],
    variable_types: Mapping[str, str],
    start_time: float,
    stop_time: float,
) -> np.ndarray | None:
    """Build FMPy's structured input array (``time`` first, one field per input), or ``None``.

    The time grid is the union of all breakpoints plus ``start_time`` and ``stop_time``.
    Discontinuities (step times, table ``hold`` jumps) appear as two rows with the same time,
    before then after, which is how FMPy detects input events; a jump exactly at ``start_time``
    keeps only the "after" row, because FMPy holds the first row at the start. Values are
    linearly interpolated between breakpoints and held outside them. Field dtypes follow
    ``variable_types``: float64 for ``Real``/``Float32``/``Float64``, int32 for integer and
    enumeration types (rounded to the nearest integer; FMPy holds discrete inputs between rows),
    bool for ``Boolean`` (non-zero is true).

    Raises:
        UserError: an input is called ``time`` or has a type signals can't drive (``String``).
    """
    if not inputs:
        return None
    if _TIME_FIELD in inputs:
        raise UserError(
            "An input named 'time' can't be driven by a signal, because the input array uses "
            "that name for its time column. Rename or drop it."
        )
    knots = {name: _knots(signal, start_time) for name, signal in inputs.items()}
    times, after = _time_rows(knots.values(), start_time, stop_time)
    dtypes = {name: _field_dtype(name, variable_types) for name in inputs}
    array = np.empty(times.size, dtype=[(_TIME_FIELD, np.float64), *dtypes.items()])
    array[_TIME_FIELD] = times
    for name, signal_knots in knots.items():
        array[name] = _cast(_evaluate(signal_knots, times, after), dtypes[name])
    return array


def _field_dtype(name: str, variable_types: Mapping[str, str]) -> np.dtype[np.generic]:
    type_name = variable_types.get(name)
    if type_name in _FLOAT_TYPES:
        return np.dtype(np.float64)
    if type_name in _INT_TYPES:
        return np.dtype(np.int32)
    if type_name == _BOOL_TYPE:
        return np.dtype(np.bool_)
    raise UserError(
        f"Input '{name}' has type {type_name!r}, which a signal can't drive. Signals support "
        "Real/Float, Integer, Boolean and Enumeration inputs."
    )


def _cast(values: np.ndarray, dtype: np.dtype[np.generic]) -> np.ndarray:
    if dtype == np.bool_:
        return values != 0.0
    if dtype.kind == "i":
        return np.rint(values).astype(dtype)
    return values


def _knots(signal: InputSignal, start_time: float) -> list[_Knot]:
    """The knots of one signal; a constant is a single knot at ``start_time``."""
    if isinstance(signal, Constant):
        return [_Knot(start_time, signal.value, signal.value)]
    if isinstance(signal, Step):
        return [_Knot(signal.time, signal.before, signal.after)]
    if isinstance(signal, Ramp):
        return [
            _Knot(signal.start, signal.before, signal.before),
            _Knot(signal.end, signal.after, signal.after),
        ]
    return _table_knots(signal)


def _table_knots(table: Table) -> list[_Knot]:
    """Points sharing a time collapse into one knot: first value before, last value after.

    In ``hold`` mode the value before a knot is the previous knot's value held, so every change
    is a jump.
    """
    groups: dict[float, list[float]] = {}
    for t, value in table.points:
        groups.setdefault(t, []).append(value)
    hold = table.interpolation == "hold"
    knots: list[_Knot] = []
    for t, values in groups.items():
        before = knots[-1].after if hold and knots else values[0]
        knots.append(_Knot(t, before, values[-1]))
    return knots


def _time_rows(
    all_knots: Collection[list[_Knot]], start_time: float, stop_time: float
) -> tuple[np.ndarray, np.ndarray]:
    """Row times (jumps doubled) and, per row, whether it carries the value after the jump."""
    times = {start_time, stop_time}
    jumps: set[float] = set()
    for knots in all_knots:
        for knot in knots:
            if start_time <= knot.t <= stop_time:
                times.add(knot.t)
                if knot.before != knot.after and knot.t > start_time:
                    jumps.add(knot.t)
    unique = np.array(sorted(times), dtype=np.float64)
    counts = np.where(np.isin(unique, list(jumps)), 2, 1)
    row_times = np.repeat(unique, counts)
    after = np.ones(row_times.size, dtype=np.bool_)
    after[(np.cumsum(counts) - counts)[counts == 2]] = False
    return row_times, after


def _evaluate(knots: Sequence[_Knot], times: np.ndarray, after: np.ndarray) -> np.ndarray:
    """Signal values on the rows: interpolate, then take before/after values at knot times."""
    knot_t = np.array([k.t for k in knots], dtype=np.float64)
    before = np.array([k.before for k in knots], dtype=np.float64)
    after_values = np.array([k.after for k in knots], dtype=np.float64)
    xs = np.repeat(knot_t, 2)
    ys = np.column_stack([before, after_values]).ravel()
    values = np.interp(times, xs, ys)
    position = np.minimum(np.searchsorted(knot_t, times), knot_t.size - 1)
    at_knot = knot_t[position] == times
    values[at_knot] = np.where(
        after[at_knot], after_values[position[at_knot]], before[position[at_knot]]
    )
    return values
