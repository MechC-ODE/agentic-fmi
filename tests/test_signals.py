"""Tests for ``signals.py``: input name checks and the FMPy input array."""

from __future__ import annotations

import numpy as np
import pytest

from agentic_fmi.errors import UserError
from agentic_fmi.signals import check_inputs, to_input_array
from agentic_fmi.store.protocol import Constant, InputSignal, Ramp, Step, Table

REAL = {"u": "Real", "v": "Real"}


def _array(
    inputs: dict[str, InputSignal],
    types: dict[str, str] | None = None,
    start: float = 0.0,
    stop: float = 10.0,
) -> np.ndarray:
    result = to_input_array(inputs, types or REAL, start, stop)
    assert result is not None
    return result


def _rows(array: np.ndarray, field: str = "u") -> list[tuple[float, float]]:
    return [(float(t), float(v)) for t, v in zip(array["time"], array[field], strict=True)]


# --- check_inputs ---------------------------------------------------------------------------


def test_check_inputs_accepts_known_names() -> None:
    check_inputs({"u": Constant(value=1.0)}, ["u", "v"])
    check_inputs({}, [])


def test_check_inputs_unknown_name_lists_close_matches_and_inputs() -> None:
    with pytest.raises(UserError) as error:
        check_inputs({"uu": Constant(value=1.0)}, ["u", "v"])
    assert str(error.value) == "Unknown input 'uu'. Close matches: u. Inputs: u, v."


def test_check_inputs_without_close_match_omits_the_hint() -> None:
    with pytest.raises(UserError) as error:
        check_inputs({"zzz": Constant(value=1.0)}, ["u", "v"])
    assert str(error.value) == "Unknown input 'zzz'. Inputs: u, v."


def test_check_inputs_fmu_without_inputs() -> None:
    with pytest.raises(UserError, match="no inputs"):
        check_inputs({"u": Constant(value=1.0)}, [])


def test_check_inputs_reports_every_unknown_name_once() -> None:
    with pytest.raises(UserError) as error:
        check_inputs({"uu": Constant(value=1.0), "vv": Constant(value=1.0)}, ["u", "v"])
    message = str(error.value)
    assert "'uu'" in message
    assert "'vv'" in message
    assert message.count("Inputs:") == 1


def test_check_inputs_points_parameters_to_start_values() -> None:
    with pytest.raises(UserError, match="start_values"):
        check_inputs({"k": Constant(value=1.0)}, ["u"], non_input_variables=["k", "y"])


def test_check_inputs_message_stays_short_for_many_inputs() -> None:
    names = [f"subsystem.component_{i}.input_signal" for i in range(200)]
    with pytest.raises(UserError) as error:
        check_inputs({"missing": Constant(value=1.0)}, names)
    assert len(str(error.value)) <= 500
    assert "more" in str(error.value)


# --- to_input_array: shapes of signals --------------------------------------------------------


def test_no_inputs_gives_none() -> None:
    assert to_input_array({}, REAL, 0.0, 10.0) is None


def test_constant_has_start_and_stop_rows() -> None:
    array = _array({"u": Constant(value=2.5)}, start=1.0, stop=4.0)
    assert array.dtype.names == ("time", "u")
    assert _rows(array) == [(1.0, 2.5), (4.0, 2.5)]


def test_step_has_two_rows_at_the_step_time() -> None:
    array = _array({"u": Step(time=3.0, before=1.0, after=5.0)})
    assert _rows(array) == [(0.0, 1.0), (3.0, 1.0), (3.0, 5.0), (10.0, 5.0)]


def test_step_default_before_is_zero() -> None:
    array = _array({"u": Step(time=3.0, after=5.0)})
    assert _rows(array)[0] == (0.0, 0.0)


def test_step_at_start_time_keeps_only_the_value_after() -> None:
    array = _array({"u": Step(time=2.0, before=1.0, after=5.0)}, start=2.0, stop=4.0)
    assert _rows(array) == [(2.0, 5.0), (4.0, 5.0)]


def test_step_at_stop_time_has_both_rows() -> None:
    array = _array({"u": Step(time=10.0, before=1.0, after=5.0)})
    assert _rows(array) == [(0.0, 1.0), (10.0, 1.0), (10.0, 5.0)]


def test_step_after_stop_time_is_dropped() -> None:
    array = _array({"u": Step(time=50.0, before=1.0, after=5.0)})
    assert _rows(array) == [(0.0, 1.0), (10.0, 1.0)]


def test_ramp_is_continuous_and_held_outside() -> None:
    array = _array({"u": Ramp(start=2.0, end=4.0, before=1.0, after=5.0)})
    assert _rows(array) == [(0.0, 1.0), (2.0, 1.0), (4.0, 5.0), (10.0, 5.0)]


def test_ramp_starting_before_start_time_is_cut_at_start() -> None:
    array = _array({"u": Ramp(start=-2.0, end=2.0, before=0.0, after=4.0)})
    assert _rows(array) == [(0.0, 2.0), (2.0, 4.0), (10.0, 4.0)]


# --- to_input_array: tables -------------------------------------------------------------------


def test_table_linear_interpolates_between_points() -> None:
    table = Table(points=[(1.0, 0.0), (3.0, 4.0), (6.0, 4.0)])
    array = _array({"u": table})
    assert _rows(array) == [(0.0, 0.0), (1.0, 0.0), (3.0, 4.0), (6.0, 4.0), (10.0, 4.0)]


def test_table_linear_value_inside_a_gap_between_other_breakpoints() -> None:
    inputs: dict[str, InputSignal] = {
        "u": Table(points=[(0.0, 0.0), (10.0, 10.0)]),
        "v": Step(time=4.0, after=1.0),
    }
    array = _array(inputs)
    assert _rows(array, "u") == [(0.0, 0.0), (4.0, 4.0), (4.0, 4.0), (10.0, 10.0)]
    assert _rows(array, "v") == [(0.0, 0.0), (4.0, 0.0), (4.0, 1.0), (10.0, 1.0)]


def test_table_hold_jumps_at_every_change() -> None:
    table = Table(points=[(0.0, 1.0), (2.0, 3.0), (5.0, 3.0), (7.0, 0.0)], interpolation="hold")
    array = _array({"u": table})
    assert _rows(array) == [
        (0.0, 1.0),
        (2.0, 1.0),
        (2.0, 3.0),
        (5.0, 3.0),
        (7.0, 3.0),
        (7.0, 0.0),
        (10.0, 0.0),
    ]


def test_table_hold_holds_the_first_value_before_the_first_point() -> None:
    table = Table(points=[(4.0, 2.0), (6.0, 8.0)], interpolation="hold")
    rows = _rows(_array({"u": table}))
    assert rows[0] == (0.0, 2.0)
    assert rows[-1] == (10.0, 8.0)


def test_table_linear_duplicate_times_make_a_jump() -> None:
    table = Table(points=[(0.0, 0.0), (5.0, 2.0), (5.0, 8.0), (10.0, 8.0)])
    array = _array({"u": table})
    assert _rows(array) == [(0.0, 0.0), (5.0, 2.0), (5.0, 8.0), (10.0, 8.0)]


def test_table_hold_duplicate_times_use_the_last_value() -> None:
    table = Table(points=[(0.0, 1.0), (5.0, 2.0), (5.0, 8.0)], interpolation="hold")
    rows = _rows(_array({"u": table}))
    assert rows == [(0.0, 1.0), (5.0, 1.0), (5.0, 8.0), (10.0, 8.0)]


def test_table_single_point_is_a_constant() -> None:
    array = _array({"u": Table(points=[(3.0, 7.0)])})
    assert set(array["u"]) == {7.0}


def test_jump_in_one_signal_doubles_the_row_for_all_signals() -> None:
    inputs: dict[str, InputSignal] = {
        "u": Step(time=5.0, after=1.0),
        "v": Constant(value=2.0),
    }
    array = _array(inputs)
    assert list(array["time"]) == [0.0, 5.0, 5.0, 10.0]
    assert list(array["v"]) == [2.0, 2.0, 2.0, 2.0]


def test_event_rows_are_exactly_equal_times() -> None:
    """FMPy finds input events with ``np.diff(t) == 0``, so the doubled times must be identical."""
    array = _array({"u": Step(time=0.1 + 0.2, after=1.0)})
    assert int(np.sum(np.diff(array["time"]) == 0)) == 1


# --- to_input_array: dtypes -------------------------------------------------------------------


def test_field_dtypes_follow_variable_types() -> None:
    types = {
        "r": "Real",
        "f32": "Float32",
        "f64": "Float64",
        "i": "Integer",
        "e": "Enumeration",
        "i8": "Int8",
        "u64": "UInt64",
        "b": "Boolean",
    }
    inputs: dict[str, InputSignal] = {name: Constant(value=1.0) for name in types}
    array = _array(inputs, types)
    assert array.dtype.names == ("time", *types)
    assert array.dtype["time"] == np.float64
    for name in ("r", "f32", "f64"):
        assert array.dtype[name] == np.float64
    for name in ("i", "e", "i8", "u64"):
        assert array.dtype[name] == np.int32
    assert array.dtype["b"] == np.bool_


def test_integer_input_rounds_to_the_nearest_integer() -> None:
    array = _array({"i": Step(time=2.0, after=2.6)}, {"i": "Integer"})
    assert list(array["i"]) == [0, 0, 3, 3]


def test_boolean_input_treats_non_zero_as_true() -> None:
    array = _array({"b": Step(time=2.0, after=0.5)}, {"b": "Boolean"})
    assert list(array["b"]) == [False, False, True, True]


def test_array_is_indexable_like_fmpy_expects() -> None:
    array = _array({"u": Step(time=2.0, after=1.0), "v": Constant(value=3.0)})
    assert array.ndim == 1
    assert array.dtype.names is not None
    assert array.dtype.names[0] == "time"
    assert np.all(np.diff(array["time"]) >= 0)


# --- to_input_array: errors -------------------------------------------------------------------


def test_input_named_time_is_refused() -> None:
    with pytest.raises(UserError, match="time"):
        to_input_array({"time": Constant(value=1.0)}, {"time": "Real"}, 0.0, 1.0)


def test_string_input_is_refused() -> None:
    with pytest.raises(UserError, match="String"):
        to_input_array({"s": Constant(value=1.0)}, {"s": "String"}, 0.0, 1.0)


def test_input_without_a_known_type_is_refused() -> None:
    with pytest.raises(UserError, match="'u'"):
        to_input_array({"u": Constant(value=1.0)}, {}, 0.0, 1.0)
