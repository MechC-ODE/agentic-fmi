"""The shared live rules (ADR 0010, ``live.py``): what a ``set`` may change (``settable``,
``check_sets``, ``driven_inputs``), which outputs ``live.json`` carries (``live_columns``), and the
``live.json`` ring buffer (``LiveBuffer``): change-only snapshots, rounding, the size cap, the
worst case the limits allow, the history kept for every column and the ``chart`` selection.

Collaborators: ``agentic_fmi.live`` (under test), ``agentic_fmi.fmu.inspect.read_description``
(XML only), ``live_support`` (descriptions, a ``SystemSpec``), the Reference FMUs through the
``fmu`` fixture and the ``new_live_run`` fixture (a ``RunSpec``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from live_support import descriptions_of, system_spec

from agentic_fmi.config import Limits
from agentic_fmi.fmu.inspect import read_description
from agentic_fmi.live import (
    INPUTS_TABLE,
    LiveBuffer,
    check_sets,
    driven_inputs,
    live_columns,
    settable,
)
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import dump_json
from agentic_fmi.store.protocol import Constant, RunSpec

FmuFactory = Callable[[str, str], Path]


def _variable(path: Path, name: str) -> Any:
    return next(v for v in read_description(path).modelVariables if v.name == name)


def _spec(new_live_run: Callable[..., Path], path: Path, **options: Any) -> RunSpec:
    return runstore.read_spec(new_live_run(path, **options))


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_settable_allows_undriven_inputs_and_tunable_parameters(
    fmu: FmuFactory, version: str
) -> None:
    path = fmu(version, "Feedthrough")
    assert settable("x", _variable(path, "Float64_tunable_parameter"), 2, None) == 2.0
    assert isinstance(settable("x", _variable(path, "Float64_tunable_parameter"), 2, None), float)
    assert settable("x", _variable(path, "Float64_continuous_input"), -1.5, None) == -1.5
    assert settable("x", _variable(path, "Int32_input"), 3.0, None) == 3
    assert settable("x", _variable(path, "Boolean_input"), True, None) is True
    assert settable("x", _variable(path, "Enumeration_input"), 2, None) == 2


@pytest.mark.parametrize(
    ("version", "name", "value", "message"),
    [
        ("2.0", "Float64_fixed_parameter", 1.0, "fixed parameter"),
        ("2.0", "Float64_continuous_output", 1.0, "an output"),
        ("2.0", "time", 1.0, "independent variable"),
        ("2.0", "String_input", 1.0, "String variable"),
        ("3.0", "Binary_input", 1.0, "Binary variable"),
        ("2.0", "Int32_input", 1.5, "needs a whole number"),
        ("2.0", "Boolean_input", 1, "needs true or false"),
        ("2.0", "Float64_tunable_parameter", True, "needs a number"),
        ("3.0", "Int8_input", 128, "doesn't fit the Int8 range"),
        ("3.0", "UInt64_input", -1, "doesn't fit the UInt64 range"),
        ("2.0", "Int32_input", 2**31, "doesn't fit the Integer range"),
    ],
)
def test_settable_refuses_with_the_reason(
    fmu: FmuFactory, version: str, name: str, value: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        settable(name, _variable(fmu(version, "Feedthrough"), name), value, None)


def test_settable_refuses_constants_locals_structural_parameters_and_arrays(
    fmu: FmuFactory,
) -> None:
    ball, space = fmu("3.0", "BouncingBall"), fmu("3.0", "StateSpace")
    for path, name, message in [
        (ball, "v_min", "a constant"),
        (ball, "der(h)", "a local variable"),
        (space, "m", "a structural parameter"),
        (space, "A", "an array variable"),
    ]:
        with pytest.raises(ValueError, match=message):
            settable(name, _variable(path, name), 1.0, None)


def test_settable_checks_the_declared_range(fmu: FmuFactory) -> None:
    e = _variable(fmu("2.0", "BouncingBall"), "e")
    assert settable("e", e, 0.5, None) == 0.5 and settable("e", e, 1, None) == 1.0
    with pytest.raises(ValueError, match=r"outside its declared range \[0.5, 1\]"):
        settable("e", e, 0.49, None)


def test_driven_inputs_are_refused_with_their_driver(
    fmu: FmuFactory, new_live_run: Callable[..., Path]
) -> None:
    path = fmu("2.0", "Feedthrough")
    spec = _spec(new_live_run, path, inputs={"Float64_continuous_input": Constant(value=1.0)})
    assert driven_inputs(spec) == {"Float64_continuous_input": INPUTS_TABLE}
    with pytest.raises(ValueError, match="driven by the run's inputs table"):
        check_sets(spec, descriptions_of(path), {"Float64_continuous_input": 2.0}, Limits())
    system = system_spec(
        "r9",
        {"A": path, "B": path},
        [("A.Float64_continuous_output", "B.Float64_continuous_input")],
        ["B.Float64_continuous_output"],
    )
    descriptions = {"A": read_description(path), "B": read_description(path)}
    assert driven_inputs(system) == {"B.Float64_continuous_input": "A.Float64_continuous_output"}
    with pytest.raises(ValueError, match=r"the connection from A.Float64_continuous_output"):
        check_sets(system, descriptions, {"B.Float64_continuous_input": 2.0}, Limits())
    assert check_sets(system, descriptions, {"A.Float64_continuous_input": 2}, Limits()) == {
        "A.Float64_continuous_input": 2.0
    }
    with pytest.raises(ValueError, match="component among A, B"):
        check_sets(system, descriptions, {"C.x": 1.0}, Limits())
    with pytest.raises(ValueError, match=r"Close matches: A.Float64_tunable_parameter"):
        check_sets(system, descriptions, {"A.Float64_tunable_paramter": 1.0}, Limits())


def test_check_sets_limits_the_number_of_names(
    fmu: FmuFactory, new_live_run: Callable[..., Path]
) -> None:
    path = fmu("2.0", "BouncingBall")
    spec, descriptions = _spec(new_live_run, path), descriptions_of(path)
    with pytest.raises(ValueError, match="at least one"):
        check_sets(spec, descriptions, {}, Limits())
    with pytest.raises(ValueError, match="at most 1 variables"):
        check_sets(
            spec, descriptions, {"e": 0.6, "h": 1.0}, replace(Limits(), live_set_max_values=1)
        )


def test_live_columns_are_the_scalar_recorded_outputs_with_units(
    fmu: FmuFactory, new_live_run: Callable[..., Path]
) -> None:
    ball = fmu("3.0", "BouncingBall")
    spec = _spec(new_live_run, ball, outputs=["v", "h", "v", "e", "nope"])
    assert live_columns(spec, descriptions_of(ball), Limits()) == [
        ("v", "m/s"),
        ("h", "m"),
        ("e", None),
    ]
    through = fmu("3.0", "Feedthrough")
    spec = _spec(new_live_run, through)
    names = [name for name, _ in live_columns(spec, descriptions_of(through), Limits())]
    assert "String_output" not in names and "Binary_output" not in names
    assert "Float32_continuous_output" in names
    capped = live_columns(spec, descriptions_of(through), replace(Limits(), live_max_columns=2))
    assert [name for name, _ in capped] == names[:2]
    space = fmu("3.0", "StateSpace")
    assert live_columns(_spec(new_live_run, space), descriptions_of(space), Limits()) == []


def test_the_buffer_snapshots_only_on_change_rounded_and_numbered() -> None:
    buffer = LiveBuffer(
        "r3", [("h", "m"), ("v", "m/s"), ("x", None)], replace(Limits(), live_variables=2)
    )
    assert buffer.charted == ("h", "v") and buffer.names == ("h", "v", "x")
    assert buffer.snapshot() is None
    buffer.push(0.0, [1.0, 0.0, 5.0])
    assert buffer.snapshot() is None  # no full sample yet: nothing to show
    assert buffer.due(10.0)
    buffer.record(0.123456789, [1.23456789, None, float("nan")], now=10.0)
    assert not buffer.due(10.1) and buffer.due(10.25)
    first = buffer.snapshot()
    assert first is not None and first.seq == 1 and first.t == 0.123456789
    assert [(c.name, c.unit, c.value) for c in first.values] == [
        ("h", "m", 1.23457),
        ("v", "m/s", None),
        ("x", None, None),
    ]
    assert first.time == [0.0] and first.series == {"h": [1.0], "v": [0.0]}
    assert buffer.snapshot() is None
    buffer.push(0.01, [0.99, float("inf"), 5.0])
    second = buffer.snapshot()
    assert second is not None and second.seq == 2 and second.series["v"] == [0.0, None]


def test_the_ring_keeps_the_last_points_and_halves_to_fit_the_cap() -> None:
    limits = replace(Limits(), live_points=50, live_max_bytes=400)
    buffer = LiveBuffer("r3", [("h", "m")], limits)
    for k in range(80):
        buffer.push(k * 0.01, [float(k)])
    buffer.record(0.79, [79.0], now=0.0)
    snapshot = buffer.snapshot()
    assert snapshot is not None
    assert len(dump_json(snapshot)) + 1 <= 400
    assert 0 < len(snapshot.time) < 50 and snapshot.series["h"][-1] == 79.0
    assert len(snapshot.series["h"]) == len(snapshot.time)
    assert snapshot.time == sorted(snapshot.time)


def test_the_worst_case_the_limits_allow_fits_without_dropping_history() -> None:
    limits = Limits()
    columns = [
        (f"component_{i:03d}.{'x' * 48}", "kg.m2/s3") for i in range(limits.live_max_columns)
    ]
    buffer = LiveBuffer("r123456", columns, limits)
    value = -1.23456789e-123
    for k in range(limits.live_points + 10):
        buffer.push(1234.56789 + k * 1e-3, [value] * len(columns))
    buffer.record(1234.56789, [value] * len(columns), now=0.0)
    snapshot = buffer.snapshot()
    assert snapshot is not None and len(snapshot.time) == limits.live_points
    size = len(json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":"))) + 1
    assert size < limits.live_max_bytes


def _filled(points: int = 4, live_variables: int = 3) -> LiveBuffer:
    """A buffer of columns a, b, c, d, e with ``points`` history points (a = k, b = 10k, ...)."""
    columns = [(name, None) for name in "abcde"]
    buffer = LiveBuffer("r3", columns, replace(Limits(), live_variables=live_variables))
    for k in range(points):
        buffer.push(k * 0.1, [float(k * 10**i) for i in range(5)])
    buffer.record(points * 0.1, [0.0] * 5, now=0.0)
    return buffer


def test_the_history_of_every_column_is_kept_and_only_the_selection_is_published() -> None:
    buffer = _filled()
    assert buffer.charted == ("a", "b", "c")  # the default: the first live_variables columns
    snapshot = buffer.snapshot()
    assert snapshot is not None and list(snapshot.series) == ["a", "b", "c"]
    assert [c.name for c in snapshot.values] == list("abcde")  # values stay complete
    assert buffer.chart(["e", "a"]) is None  # a column that was never charted has its history
    assert buffer.charted == ("e", "a")
    chosen = buffer.snapshot()
    assert chosen is not None and chosen.seq == 2 and chosen.time == snapshot.time
    assert list(chosen.series) == ["e", "a"]  # in the order the user chose
    assert chosen.series["e"] == [0.0, 10000.0, 20000.0, 30000.0]
    assert chosen.series["a"] == [0.0, 1.0, 2.0, 3.0]
    assert buffer.snapshot() is None


def test_a_chart_selection_may_be_empty_and_is_published_at_once() -> None:
    buffer = _filled()
    assert buffer.snapshot() is not None
    assert buffer.chart([]) is None and buffer.charted == ()
    empty = buffer.snapshot()  # a changed selection alone is a change to publish
    assert empty is not None and empty.series == {} and len(empty.time) == 4
    assert buffer.chart(["c"]) is None
    back = buffer.snapshot()
    assert back is not None and back.series == {"c": [0.0, 100.0, 200.0, 300.0]}


def test_a_chart_selection_is_checked_and_a_refusal_changes_nothing() -> None:
    buffer = _filled()
    assert buffer.snapshot() is not None
    before = buffer.charted
    assert "at most 3 signals, not 4" in (buffer.chart(["a", "b", "c", "d"]) or "")
    assert "'b' is named twice" in (buffer.chart(["b", "a", "b"]) or "")
    unknown = buffer.chart(["a", "nope"]) or ""
    assert "'nope' is not a recorded output with a live value" in unknown
    assert "Chart one of: a, b, c, d, e." in unknown
    assert buffer.charted == before
    assert buffer.snapshot() is None  # a refusal is no change either


def test_a_refused_chart_names_close_matches_and_lists_a_few_names() -> None:
    columns = [(f"signal_{i:02d}", None) for i in range(20)]
    buffer = LiveBuffer("r3", columns, Limits())
    reason = buffer.chart(["signal_5"]) or ""
    assert "Close matches: signal_15, signal_05, signal_19." in reason
    assert reason.endswith("signal_10, signal_11 and 8 more.")
    assert "signal_12" not in reason
