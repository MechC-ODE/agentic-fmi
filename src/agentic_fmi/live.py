"""Live control (ADR 0010): the gate of a live run with its one wait loop, the ``live.json`` ring
buffer, and the one ``settable`` rule that the server and the worker share.

A live run is an ordinary run whose spec has ``live: true`` (Co-Simulation FMUs and systems
only). Its worker passes :meth:`LiveControl.gate` to the engine instead of the batch gate. The
engine calls it once at ``start_time`` (after initialization) and after every communication
step, in the simulation thread, with its :class:`~agentic_fmi.engines.base.Live` handle. All
waiting happens inside that call, in one loop:

- **Pause.** The run starts paused at ``start_time``. ``pause`` pauses at the next gate call;
  ``resume`` (optionally with a new ``speed``) continues; a paused run waits in the loop.
- **Pacing.** While running, the gate returns only once ``wall >= anchor_wall + (t -
  anchor_t) / speed`` (``speed`` 1 = real time, default ``Timing.live_default_speed``, at most
  ``Timing.live_max_speed``). The anchor is reset on every resume and speed change, so a pause
  never makes the run race to catch up. Batch runs are never paced (they don't use this
  module's gate).
- **Single steps.** ``step`` (paused runs only) lets exactly ``n`` communication steps through
  unpaced, then pauses again; its outcome is written when the n-th step is reached (or when
  the run ends first, saying how many steps ran). A ``pause``, ``resume`` or ``stop`` during
  the steps ends them early, and the step outcome says so; a second ``step`` meanwhile is
  rejected.
- **Inbox.** Commands are read with ``store.inbox.InboxReader``: every
  ``Timing.inbox_poll_s`` while stepping, every wait slice while waiting. Each is applied in
  name order and gets exactly one outcome (``store.inbox.write_outcome``, with the simulation
  time) and one ``command`` event. A ``set`` is checked with :func:`check_sets` (the same check
  the server made; a pane's set may be wrong) and applied through ``live.set``; a bad one is
  rejected and the run goes on. A ``chart`` (the pane's chart picker) replaces the signals
  ``live.json`` publishes history for (:meth:`LiveBuffer.chart`: recorded outputs only, each
  once, at most ``Limits.live_variables``); a bad one is rejected and the run goes on, and a
  good one never pauses, resumes, steps, stops or changes the speed. ``stop`` makes the gate
  return ``False``; commands after it are rejected.
- **Waiting.** The loop sleeps at most ``Timing.live_wait_slice_s`` at a time and calls
  ``on_wait(True)`` when it starts waiting and ``on_wait(False)`` when it stops, so the worker
  pauses its ``ActiveClock``: the watchdog counts only time spent stepping, never time spent
  paused or pacing (a 1x live run longer than ``timeout_s`` is not killed). A stop request
  (SIGTERM from ``run_stop``) is seen within one slice.
- **Idle limit.** A run paused for longer than ``spec.idle_limit_s`` without a command is
  stopped (every command it handles restarts the count; time spent running never counts): the
  gate returns ``False`` and :attr:`LiveControl.reason` says why, so the worker writes
  ``stopped`` with the partial result and that reason.

History: the buffer keeps the last ``Limits.live_points`` points of every column (one
``live.get`` per gate call feeds it), so a signal the user charts later has its history at once;
``live.json`` carries the history of the current chart selection only.

Thread model: the gate (simulation thread) owns the inbox and the outcomes; the worker's
heartbeat thread reads :meth:`LiveControl.view` (state, speed, last command) into
``status.json`` and :meth:`LiveBuffer.snapshot` into ``live.json``. Only the worker writes
``status.json``; only the heartbeat thread writes ``live.json``.

:func:`settable`, :func:`driven_inputs` and :func:`check_sets` are pure and are also imported by
the server (``control.py``) to validate ``run_control(action="set")`` before writing a command.
This module imports no engine library and nothing from ``engines/`` at run time (``Live`` only
for type checking; ``SetRefused`` is in ``errors.py``), so the server may import it.

Collaborators: ``store/inbox.py`` (commands and outcomes), ``store/runstore.py``
(``append_event``), ``store/atomic.dump_json`` (the size of ``live.json``),
``store/protocol.py`` (``Command``, ``CommandOutcome``, ``LiveSnapshot``, ``RunSpec``,
``SystemSpec``), ``system.TYPE_FAMILY`` (the scalar types), ``analysis/summary.round_sig``,
``config.Timing`` and ``config.Limits``, ``errors.SetRefused``, ``engines/base.Live`` (typing only);
``worker.py`` (builds :class:`LiveControl` and :class:`LiveBuffer` for a live spec) and
``control.py`` (server-side set checks).
"""

from __future__ import annotations

import difflib
import math
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from agentic_fmi.analysis.summary import round_sig
from agentic_fmi.config import Limits, Timing
from agentic_fmi.errors import SetRefused
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import dump_json
from agentic_fmi.store.inbox import InboxReader, Pending, Source, write_outcome
from agentic_fmi.store.protocol import (
    Command,
    CommandAction,
    CommandOutcome,
    Event,
    EventType,
    LiveColumn,
    LiveSnapshot,
    RunSpec,
    RunState,
    SetValue,
    SystemSpec,
    utc_now,
)
from agentic_fmi.system import EXPECTED_VALUE, TYPE_FAMILY

if TYPE_CHECKING:
    from agentic_fmi.engines.base import Live

LiveSpec = RunSpec | SystemSpec
"""The spec of a run that can be live: one FMU (Co-Simulation) or a system."""

INPUTS_TABLE = "the run's inputs table"
"""What :func:`driven_inputs` names as the driver of an input that ``RunSpec.inputs`` drives
(FMPy re-applies the table before every step, so a live set would be overwritten)."""

SINGLE_FMU = ""
"""The key of the one model description of a single-FMU run in ``descriptions``."""

SAMPLE_INTERVAL_S: Final = 0.2
"""Least wall time between two full samples of ``live.json``'s values (the worker's heartbeat
tick, ``worker.PROGRESS_INTERVAL_S``: ``live.json`` is written at most 5 times a second)."""

MAX_CLOSE_MATCHES: Final = 3

MAX_LISTED_NAMES: Final = 12
"""Names a rejected ``chart`` lists as the ones to choose from."""

CLOCK_TOLERANCE_S: Final = 1e-6
"""Slack in the poll-period checks, so rounding in a sum of slices never skips a poll."""

OutcomeStatus = Literal["applied", "rejected"]
Verdict = tuple[OutcomeStatus, str | None]

INTEGER_RANGES: Final = {
    "Integer": (-(2**31), 2**31 - 1),
    "Enumeration": (-(2**31), 2**31 - 1),
    **{f"Int{bits}": (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) for bits in (8, 16, 32, 64)},
    **{f"UInt{bits}": (0, 2**bits - 1) for bits in (8, 16, 32, 64)},
}
"""What each integer type can hold (FMI 2.0 Integer and Enumeration are 32-bit; FMI 3.0's
Enumeration is set as Int64 but declared values stay small), so a set never wraps around."""

_REFUSED_KINDS: Final = {
    "output": "an output, which the FMU computes; set the input or tunable parameter behind it",
    "local": "a local variable, which the FMU computes",
    "independent": "the independent variable (time)",
    "calculatedParameter": "a calculated parameter, which the FMU computes from others",
    "structuralParameter": "a structural parameter, fixed once the FMU is initialized",
}


# --- settable: shared by the server and the worker ----------------------------------------------


def settable(name: str, variable: Any, value: SetValue, driven_by: str | None) -> SetValue:
    """``value`` converted to the type of ``variable`` (an FMPy ``ModelVariable``) if a live
    ``set`` may change it, else ``ValueError`` with the reason and the fix.

    Allowed (spike (i) items 3, 5 and 7): scalar inputs that nothing drives, and scalar
    parameters with variability ``tunable``. Refused, each with its own message: outputs,
    locals, calculated and structural parameters, the independent variable, constants,
    ``fixed`` parameters (VanDerPol's ``mu``: an FMU may accept the call and act on it anyway),
    arrays (``dimensions``), String, Binary and Clock variables, an input that ``driven_by``
    names (the inputs table, or a connection's source in a system: OMSimulator would overwrite
    it after one step), a value of the wrong kind (Boolean needs ``true``/``false``, Integer
    and Enumeration a whole number, Real a finite number), and a number outside the declared
    ``min``/``max`` (the variable's own, else its declared type's; FMPy stores them as text).
    """
    if variable.dimensions:
        raise ValueError(
            f"{name} is an array variable; a live set changes scalar variables only. Set a "
            "scalar input or tunable parameter."
        )
    family = TYPE_FAMILY.get(variable.type)
    if family is None:
        raise ValueError(
            f"{name} is a {variable.type} variable, which a live set can't change. Set a Real, "
            "Integer, Boolean or Enumeration input or tunable parameter."
        )
    _check_kind(name, variable, driven_by)
    converted = _converted(name, variable, family, value)
    _check_range(name, variable, converted)
    return converted


def _check_kind(name: str, variable: Any, driven_by: str | None) -> None:
    """Inputs nothing drives and tunable parameters pass; everything else raises."""
    causality, variability = variable.causality, variable.variability
    if causality == "input":
        if driven_by is None:
            return
        driver = driven_by if driven_by == INPUTS_TABLE else f"the connection from {driven_by}"
        raise ValueError(
            f"{name} is an input driven by {driver}, which overwrites a live set before the "
            "next step. Set an input nothing drives, or start a new run without that driver."
        )
    if causality == "parameter" and variability == "tunable":
        return
    if variability == "constant":
        kind = "a constant"
    elif causality == "parameter":
        kind = (
            f"a {variability or 'fixed'} parameter, which may only change before the run "
            "starts; pass it in start_values of a new run"
        )
    else:
        kind = _REFUSED_KINDS.get(causality, f"a variable of causality {causality!r}")
    raise ValueError(
        f"{name} is {kind}. A live set changes inputs nothing drives and tunable parameters; "
        "fmu_variables lists them."
    )


def _converted(name: str, variable: Any, family: str, value: SetValue) -> SetValue:
    """``value`` as the family's Python type, or ``ValueError`` saying what is needed."""
    if family == "boolean":
        if isinstance(value, bool):
            return value
    elif isinstance(value, bool):
        pass  # a Boolean is never a number here
    elif family in ("integer", "enumeration"):
        if isinstance(value, int):
            return value
        if math.isfinite(value) and value.is_integer():
            return int(value)
    else:
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if math.isfinite(number):
            return number
    expected = EXPECTED_VALUE[family]
    raise ValueError(
        f"{name} is a {variable.type} variable and needs {expected}, but the value is "
        f"{value!r}. Pass {expected}."
    )


def _bound(variable: Any, attribute: str) -> float | None:
    """The variable's ``min`` or ``max`` (its own, else its declared type's) as a float."""
    text = getattr(variable, attribute, None)
    if text is None and variable.declaredType is not None:
        text = getattr(variable.declaredType, attribute, None)
    try:
        return None if text is None else float(text)
    except (TypeError, ValueError):
        return None


def _check_range(name: str, variable: Any, value: SetValue) -> None:
    if isinstance(value, bool):
        return
    fits = INTEGER_RANGES.get(variable.type)
    if isinstance(value, int) and fits is not None and not fits[0] <= value <= fits[1]:
        raise ValueError(
            f"{name} = {value!r} doesn't fit the {variable.type} range [{fits[0]}, {fits[1]}]. "
            "Pass a value within it."
        )
    low, high = _bound(variable, "min"), _bound(variable, "max")
    if (low is None or value >= low) and (high is None or value <= high):
        return
    shown_low = "-inf" if low is None else f"{low:g}"
    shown_high = "inf" if high is None else f"{high:g}"
    raise ValueError(
        f"{name} = {value!r} is outside its declared range [{shown_low}, {shown_high}]. Pass a "
        "value within it."
    )


def driven_inputs(spec: LiveSpec) -> dict[str, str]:
    """Inputs whose value the run itself supplies, mapped to what supplies it: for a single
    FMU every key of ``spec.inputs`` (:data:`INPUTS_TABLE`), for a system every connection's
    ``to`` (its ``from``)."""
    if isinstance(spec, SystemSpec):
        return {connection.to: connection.from_ for connection in spec.connections}
    return dict.fromkeys(spec.inputs, INPUTS_TABLE)


def check_sets(
    spec: LiveSpec,
    descriptions: Mapping[str, Any],
    values: Mapping[str, SetValue],
    limits: Limits,
) -> dict[str, SetValue]:
    """Check and convert the values of one ``set`` command for this run.

    ``descriptions`` maps :data:`SINGLE_FMU` to the FMU's model description for a single-FMU
    run, or each component name to its model description for a system. Names are variable names
    for one FMU and ``<component>.<variable>`` (split at the first dot) for a system. Checks: at
    most ``limits.live_set_max_values`` names; each name exists (else close matches, as
    ``fmu_variables`` would list them); :func:`settable` with :func:`driven_inputs`.

    Raises:
        ValueError: the first problem, one or two sentences with the fix (the server raises it
            as ``UserError``; the worker puts it into a rejected outcome).
    """
    if not values:
        raise ValueError("A set needs at least one variable and its value.")
    if len(values) > limits.live_set_max_values:
        raise ValueError(
            f"A set takes at most {limits.live_set_max_values} variables, not {len(values)}. "
            "Split it into several sets."
        )
    driven = driven_inputs(spec)
    return {
        name: settable(name, _variable(spec, descriptions, name), value, driven.get(name))
        for name, value in values.items()
    }


APPLIED_SHOWN: Final = 10
"""How many already applied values a rejected set names before saying "and N more" (the message
is cut at ``Limits.message_max_chars``)."""


def _already_applied(applied: Mapping[str, SetValue]) -> str:
    """``"; already applied: PI.Kp=8, P.K=2"`` for the values a refused set left in effect (at
    most :data:`APPLIED_SHOWN`, then "and N more"), or ``""`` when none was set."""
    if not applied:
        return ""
    shown = [
        f"{name}={_value_text(value)}" for name, value in list(applied.items())[:APPLIED_SHOWN]
    ]
    more = len(applied) - len(shown)
    return f"; already applied: {', '.join(shown)}" + (f" and {more} more" if more > 0 else "")


def _value_text(value: SetValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"{value:.10g}" if isinstance(value, float) else str(value)


def _variable(spec: LiveSpec, descriptions: Mapping[str, Any], name: str) -> Any:
    """The ``ModelVariable`` a run's name refers to, or ``ValueError`` with close matches."""
    prefix, variable_name = "", name
    if isinstance(spec, SystemSpec):
        component, _, variable_name = name.partition(".")
        if not variable_name or component not in descriptions:
            raise ValueError(
                f"{name!r} names no variable of a component: a system's names are "
                f"<component>.<variable> with a component among {', '.join(descriptions)}."
            )
        md, prefix = descriptions[component], f"{component}."
    else:
        md = descriptions[SINGLE_FMU]
    by_name = {variable.name: variable for variable in md.modelVariables}
    variable = by_name.get(variable_name)
    if variable is not None:
        return variable
    close = difflib.get_close_matches(variable_name, list(by_name), n=MAX_CLOSE_MATCHES)
    text = f"Unknown variable {name!r}."
    if close:
        text += f" Close matches: {', '.join(prefix + match for match in close)}."
    raise ValueError(f"{text} Call fmu_variables to list names.")


def live_columns(
    spec: LiveSpec, descriptions: Mapping[str, Any], limits: Limits
) -> list[tuple[str, str | None]]:
    """The columns of ``live.json``: the scalar recorded outputs in ``spec.outputs`` order
    (arrays and String, Binary and Clock variables left out), each with its unit (the variable's,
    else its declared type's), at most ``limits.live_max_columns``."""
    columns: list[tuple[str, str | None]] = []
    for name in dict.fromkeys(spec.outputs):
        if len(columns) >= limits.live_max_columns:
            break
        try:
            variable = _variable(spec, descriptions, name)
        except ValueError:
            continue
        if variable.dimensions or variable.type not in TYPE_FAMILY:
            continue
        declared = variable.declaredType
        columns.append((name, variable.unit or getattr(declared, "unit", None)))
    return columns


# --- live.json ----------------------------------------------------------------------------------


class LiveBuffer:
    """The ``live.json`` ring buffer, shared by the gate (writer) and the heartbeat thread
    (reader) under one lock.

    :meth:`push` appends the values of every column at every gate call to a ring of the last
    ``limits.live_points`` points per column. :attr:`charted` is the current chart selection,
    by default the first ``limits.live_variables`` columns; :meth:`chart` replaces it, and only
    the charted columns' history goes into :meth:`snapshot`. :meth:`record` takes a sample of
    every column; the gate calls it when :meth:`due` says so (at most every
    ``sample_interval_s`` of wall time, :data:`SAMPLE_INTERVAL_S`) and always before it starts
    waiting paused, so a paused run's values are exactly those at its ``t``.
    """

    def __init__(
        self,
        run_id: str,
        columns: Sequence[tuple[str, str | None]],
        limits: Limits,
        sample_interval_s: float = SAMPLE_INTERVAL_S,
    ) -> None:
        self.run_id = run_id
        self.columns = tuple(columns)
        self.names: tuple[str, ...] = tuple(name for name, _unit in self.columns)
        """The names :meth:`push` and :meth:`record` take values of, in order (every column)."""
        self.limits = limits
        self._interval_s = sample_interval_s
        self._lock = threading.Lock()
        self._time: deque[float] = deque(maxlen=limits.live_points)
        self._history: dict[str, deque[float | None]] = {
            name: deque(maxlen=limits.live_points) for name in self.names
        }
        self._charted = self.names[: limits.live_variables]
        self._t: float | None = None
        self._values: tuple[float | None, ...] = ()
        self._sampled_at = -math.inf
        self._changed = False
        self._seq = 0

    @property
    def charted(self) -> tuple[str, ...]:
        """The current chart selection, in order: what :meth:`snapshot` publishes history for."""
        return self._charted

    def push(self, t: float, values: Sequence[float | None]) -> None:
        """Append one history point (values in :attr:`names` order, one per column)."""
        with self._lock:
            self._time.append(t)
            for column, value in zip(self._history.values(), values, strict=True):
                column.append(value)
            self._changed = True

    def chart(self, names: Sequence[str]) -> str | None:
        """Replace the chart selection with ``names`` (in order) and return ``None``, or leave
        it as it is and return why not: more than ``limits.live_variables`` names, a name twice,
        a name that is not a column (with close matches). An empty selection is allowed."""
        with self._lock:
            reason = self._refusal(names)
            if reason is None:
                self._charted = tuple(names)
                self._changed = True
            return reason

    def _refusal(self, names: Sequence[str]) -> str | None:
        top = self.limits.live_variables
        if len(names) > top:
            return f"a chart selection holds at most {top} signals, not {len(names)}; name fewer"
        repeated = [name for name, count in Counter(names).items() if count > 1]
        if repeated:
            return f"{repeated[0]!r} is named twice; name each signal once"
        unknown = [name for name in names if name not in self._history]
        if not unknown:
            return None
        close = difflib.get_close_matches(unknown[0], self.names, n=MAX_CLOSE_MATCHES)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        listed = ", ".join(self.names[:MAX_LISTED_NAMES])
        left = len(self.names) - MAX_LISTED_NAMES
        more = f" and {left} more" if left > 0 else ""
        return (
            f"{unknown[0]!r} is not a recorded output with a live value.{hint} Chart one of: "
            f"{listed}{more}."
        )

    def due(self, now: float) -> bool:
        """Whether a full sample is due at monotonic time ``now``."""
        return now - self._sampled_at >= self._interval_s

    def record(self, t: float, values: Sequence[float | None], now: float) -> None:
        """Store a sample of every column (values in :attr:`names` order) taken at ``t``."""
        with self._lock:
            self._t, self._values = t, tuple(values)
            self._sampled_at = now
            self._changed = True

    def snapshot(self) -> LiveSnapshot | None:
        """The next ``live.json``, or ``None`` if nothing was pushed, recorded or charted since
        the last call (the heartbeat thread writes only on change). ``seq`` increases by one per
        snapshot. ``series`` holds the history of the charted names only. Times and values are
        rounded to ``limits.significant_digits`` (``analysis.summary.round_sig``); non-finite
        values become null. If the JSON would exceed ``limits.live_max_bytes``, the oldest
        history points are dropped (halving) until it fits."""
        with self._lock:
            if not self._changed or self._t is None:
                return None
            self._changed = False
            self._seq += 1
            seq, t, values = self._seq, self._t, self._values
            times = list(self._time)
            series = {name: list(self._history[name]) for name in self._charted}
        digits = self.limits.significant_digits
        snapshot = LiveSnapshot(
            run_id=self.run_id,
            seq=seq,
            t=t,
            values=[
                self.column(name, unit, _rounded(value, digits))
                for (name, unit), value in zip(self.columns, values, strict=True)
            ],
            time=[round_sig(moment, digits) or 0.0 for moment in times],
            series={
                name: [_rounded(value, digits) for value in column]
                for name, column in series.items()
            },
        )
        return self._fitted(snapshot)

    def _fitted(self, snapshot: LiveSnapshot) -> LiveSnapshot:
        """``snapshot`` with the oldest history halved away until its JSON fits the cap."""
        while len(snapshot.time) and _size(snapshot) > self.limits.live_max_bytes:
            keep = len(snapshot.time) // 2
            start = len(snapshot.time) - keep
            snapshot = snapshot.model_copy(
                update={
                    "time": snapshot.time[start:],
                    "series": {name: column[start:] for name, column in snapshot.series.items()},
                }
            )
        return snapshot

    @staticmethod
    def column(name: str, unit: str | None, value: float | None) -> LiveColumn:
        """One ``LiveColumn`` (a non-finite ``value`` becomes ``None``)."""
        finite = value is not None and math.isfinite(value)
        return LiveColumn(name=name, unit=unit, value=value if finite else None)


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round_sig(value, digits)


def _size(snapshot: LiveSnapshot) -> int:
    """Bytes of ``live.json`` as ``runstore.write_live`` writes it (JSON plus a newline)."""
    return len(dump_json(snapshot).encode("utf-8")) + 1


# --- the gate -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveView:
    """What the heartbeat thread copies into ``status.json`` for a live run."""

    state: RunState
    """``RunState.RUNNING`` or ``RunState.PAUSED``."""
    speed: float
    last_cmd: str | None


@dataclass
class _Steps:
    """A ``step`` command in progress: ``done`` of ``n`` steps taken."""

    id: str
    source: Source
    n: int
    done: int = 0


class LiveControl:
    """The gate of one live run and its state: paused or running, speed and pacing anchor,
    single steps still to take, the inbox reader and the idle clock. Built by the worker for a
    live spec; see the module docstring for the rules.
    """

    def __init__(
        self,
        run_path: Path,
        spec: LiveSpec,
        descriptions: Mapping[str, Any],
        buffer: LiveBuffer,
        *,
        stop_requested: Callable[[], bool],
        on_wait: Callable[[bool], None],
        timing: Timing,
        limits: Limits,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.run_path = run_path
        self.spec = spec
        self.descriptions = descriptions
        self.buffer = buffer
        self.stop_requested = stop_requested
        self.on_wait = on_wait
        self.timing = timing
        self.limits = limits
        self._now = now
        self._sleep = sleep
        self._inbox = InboxReader(run_path, timing.inbox_grace_s, now)
        self._idle_limit_s = (
            timing.live_idle_limit_s if spec.idle_limit_s is None else spec.idle_limit_s
        )
        self._paused = True
        self._speed = timing.live_default_speed
        self._anchor = (now(), spec.experiment.start_time)
        self._steps: _Steps | None = None
        self._stopped_by: str | None = None
        self._reason: str | None = None
        self._last_cmd: str | None = None
        self._last_poll = -math.inf
        self._idle_since = now()
        self._view = LiveView(RunState.RUNNING, self._speed, None)

    def gate(self, t: float, live: Live) -> bool:
        """The engine's gate for a live run (:data:`~agentic_fmi.engines.base.LiveGate`).

        Order at each call: read every column once (``live.get``) into the history, and into
        the current values when a sample is due; count down a step sequence; apply inbox
        commands if a poll is due; then loop while paused (or while the pacing deadline is
        ahead), sampling every column once before the first sleep, sleeping at most one slice,
        polling the inbox each slice, checking the stop request and the idle limit. Returns
        ``False`` for a ``stop`` command, a stop request or the idle limit, else ``True`` once
        the run may take its next step.
        """
        self._sample(t, live)
        if self._steps is not None:
            self._count_step(t)
        if self._now() - self._last_poll >= self.timing.inbox_poll_s - CLOCK_TOLERANCE_S:
            self._poll(t, live)
        return self._wait(t, live)

    def view(self) -> LiveView:
        """The current state, speed and last command (safe from the heartbeat thread)."""
        return self._view

    @property
    def reason(self) -> str | None:
        """Why the gate stopped the run if a command or SIGTERM didn't (the idle limit), for
        ``status.reason``; else ``None``."""
        return self._reason

    def finish(self, t: float) -> None:
        """The final sweep when the run ends at ``t`` (any way but a crash): write the outcome
        of an unfinished step command (applied, k of n steps) and reject every command file
        still in the inbox ("the run ended at t=... before this command was applied"), so each
        command found gets exactly one outcome."""
        if self._steps is not None:
            self._end_steps(t, "before the run ended")
        for pending in self._inbox.remaining():
            message = f"the run ended at t={t:g} before this command was applied"
            if pending.problem is not None:
                message += f"; the file was {pending.problem}"
            action = pending.command.action if pending.command is not None else None
            self._write(pending.id, pending.source, action, ("rejected", message), t)

    # --- waiting --------------------------------------------------------------------------------

    def _wait(self, t: float, live: Live) -> bool:
        """The one wait loop: pause, pacing, steps, polling, stop and the idle limit."""
        slice_s = self.timing.live_wait_slice_s
        waiting = False
        try:
            while True:
                if self._stopped_by is not None or self.stop_requested():
                    return False
                now = self._now()
                delay = self._delay(t, now)
                if delay <= 0:
                    return True
                if self._idle_expired(t, now):
                    return False
                if not waiting:
                    waiting = True
                    self._start_waiting(t, live, now)
                self._sleep(min(delay, slice_s))
                if self._now() - self._last_poll >= slice_s - CLOCK_TOLERANCE_S:
                    self._poll(t, live)
        finally:
            if waiting:
                self.on_wait(False)

    def _delay(self, t: float, now: float) -> float:
        """Seconds to wait before the next step: a slice while paused, the pacing deadline's
        distance while running, 0 during single steps."""
        if self._steps is not None:
            return 0.0
        if self._paused:
            return self.timing.live_wait_slice_s
        anchor_wall, anchor_t = self._anchor
        return anchor_wall + (t - anchor_t) / self._speed - now

    def _idle_expired(self, t: float, now: float) -> bool:
        if not self._paused or now - self._idle_since <= self._idle_limit_s:
            return False
        self._reason = (
            f"Paused for more than {self._idle_limit_s:g} s without a command (the idle "
            f"limit), so the worker stopped the run at t={t:g} with its partial result."
        )
        return True

    def _start_waiting(self, t: float, live: Live, now: float) -> None:
        if self._paused:
            self._record(t, live, now)
        self._publish()
        self.on_wait(True)

    def _sample(self, t: float, live: Live) -> None:
        """Read every column once; push it into the history, and record it as the current values
        when a sample is due."""
        names = self.buffer.names
        values = live.get(names) if names else []
        if names:
            self.buffer.push(t, values)
        now = self._now()
        if self.buffer.due(now):
            self.buffer.record(t, values, now)

    def _record(self, t: float, live: Live, now: float) -> None:
        names = self.buffer.names
        self.buffer.record(t, live.get(names) if names else [], now)

    def _publish(self) -> None:
        state = RunState.PAUSED if self._paused and self._steps is None else RunState.RUNNING
        self._view = LiveView(state, self._speed, self._last_cmd)

    # --- commands -------------------------------------------------------------------------------

    def _poll(self, t: float, live: Live) -> None:
        self._last_poll = self._now()
        for pending in self._inbox.poll():
            self._handle(pending, t, live)

    def _handle(self, pending: Pending, t: float, live: Live) -> None:
        """Apply one command file and write its outcome (a step's is written when it ends)."""
        self._idle_since = self._now()
        command = pending.command
        if command is None:
            self._write(pending.id, pending.source, None, ("rejected", pending.problem), t)
            return
        if self._stopped_by is not None:
            message = f"the run is stopping: command {self._stopped_by} stopped it first"
            self._write(pending.id, pending.source, command.action, ("rejected", message), t)
            return
        verdict = self._apply(pending, command, t, live)
        if verdict is not None:
            self._write(pending.id, pending.source, command.action, verdict, t)
        self._publish()  # a step that has just begun shows as running

    def _apply(self, pending: Pending, command: Command, t: float, live: Live) -> Verdict | None:
        """Carry out one command; its verdict, or ``None`` for a step that has just begun."""
        if command.action == "pause":
            return self._pause(t)
        if command.action == "resume":
            return self._resume(command.speed, t)
        if command.action == "step":
            return self._step(pending, command.n or 1)
        if command.action == "set":
            return self._set(command.values or {}, live)
        if command.action == "chart":
            return self._chart(command.names or [])
        if self._steps is not None:
            self._end_steps(t, "before a stop ended them")
        self._stopped_by = pending.id
        return "applied", None

    def _pause(self, t: float) -> Verdict:
        if self._steps is not None:
            self._end_steps(t, "before a pause ended them")
            return "applied", None
        if self._paused:
            return "applied", "already paused"
        self._paused = True
        return "applied", None

    def _resume(self, speed: float | None, t: float) -> Verdict:
        top = self.timing.live_max_speed
        if speed is not None and speed > top:
            return "rejected", f"speed {speed:g} is over the limit of {top:g}; pass at most {top:g}"
        if self._steps is not None:
            self._end_steps(t, "before a resume ended them")
        note = None if self._paused else "already running"
        if note is not None and speed is not None:
            note += f"; speed set to {speed:g}"
        self._paused = False
        if speed is not None:
            self._speed = speed
        self._anchor = (self._now(), t)  # re-anchored: a pause never makes the run catch up
        return "applied", note

    def _step(self, pending: Pending, n: int) -> Verdict | None:
        if self._steps is not None:
            steps = self._steps
            return "rejected", (
                f"a step command is still running ({steps.done} of {steps.n} steps); wait for "
                "its outcome"
            )
        if not self._paused:
            return "rejected", "step needs a paused run; pause it first"
        self._steps = _Steps(pending.id, pending.source, n)
        return None

    def _set(self, values: Mapping[str, SetValue], live: Live) -> Verdict:
        """Check, then apply a ``set``. An engine refusal partway leaves the earlier values set
        (a multi-value set is not atomic), so the rejection names them."""
        try:
            live.set(check_sets(self.spec, self.descriptions, values, self.limits))
        except SetRefused as error:
            return "rejected", f"{error}{_already_applied(error.applied)}"
        except (ValueError, RuntimeError) as error:
            return "rejected", str(error)
        return "applied", None

    def _chart(self, names: Sequence[str]) -> Verdict:
        """Select the charted signals; nothing else about the run changes."""
        reason = self.buffer.chart(names)
        return ("applied", None) if reason is None else ("rejected", reason)

    def _count_step(self, t: float) -> None:
        """One more step of the running step command; at its last, pause and write its outcome."""
        steps = self._steps
        assert steps is not None
        steps.done += 1
        if steps.done < steps.n:
            return
        self._steps = None
        self._idle_since = self._now()
        self._write(steps.id, steps.source, "step", ("applied", None), t)

    def _end_steps(self, t: float, why: str) -> None:
        """End the running step command early and write its outcome."""
        steps = self._steps
        assert steps is not None
        self._steps = None
        message = f"{steps.done} of {steps.n} steps {why}"
        self._write(steps.id, steps.source, "step", ("applied", message), t)

    def _write(
        self,
        command_id: str,
        source: Source,
        action: CommandAction | None,
        verdict: tuple[OutcomeStatus, str | None],
        t: float,
    ) -> None:
        """Write the outcome, append the ``command`` event, then show it as ``last_cmd``. A
        store error is reported on stderr (``log.txt``) and never ends the run."""
        status, message = verdict
        limit = self.limits.message_max_chars
        if message is not None and len(message) > limit:
            message = message[: limit - 3] + "..."
        at = utc_now()
        outcome = CommandOutcome(
            run_id=self.spec.run_id,
            id=command_id,
            source=source,
            action=action,
            status=status,
            t=t,
            at=at,
            message=message,
        )
        text = f"{command_id} {action or 'unreadable'} {status} at t={t:g}"
        event = Event(
            at=at,
            run_id=self.spec.run_id,
            type=EventType.COMMAND,
            source="worker",
            message=f"{text}: {message}" if message else text,
        )
        try:
            write_outcome(self.run_path, outcome)
            runstore.append_event(self.run_path.parent.parent, event)
        except (OSError, ValueError) as error:
            print(f"afmi worker: can't record command {command_id}: {error}", file=sys.stderr)
        self._last_cmd = command_id
        self._publish()
