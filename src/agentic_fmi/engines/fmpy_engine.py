"""The FMPy engine: run one simulation of an extracted FMU described by a ``RunSpec``.

One plain function with the engine signature of ``engines/base.py`` (``LiveGate`` in,
``EngineResult`` out), shared with the OMSimulator engine (ADR 0009). Runs only inside a worker
process (ADR 0002); ruff TID251 allows FMPy's loading APIs only here and in ``worker.py``.

ADR 0010 keeps ``simulate_fmu`` for every run, batch and live, Model Exchange and
Co-Simulation (spike (i): the gate through ``recorder.fmu`` is bit-identical to an own
``doStep`` loop):

- The gate is ``gate(t, live)`` with :class:`_FmpyLive` over ``recorder.fmu`` (built once per
  run, lazily: a batch gate never reads it).
- Stall check, batch and live: ``step_finished`` counts consecutive calls at the same ``t``;
  at ``Limits.stall_repeats`` it returns ``False`` and flags the stall, and after
  ``simulate_fmu`` returns the engine raises ``RuntimeError("the FMU made no progress at
  t=...: it discarded the step without terminating")`` (``simulation_error``). This ends the
  endless retry of an FMI 2.0 ``fmi2Discard`` without ``fmi2Terminated`` (spike (i) item 6).
- Live specs only (``spec.live``, always Co-Simulation): :func:`_gated_instance` creates the
  instance with ``instantiate_fmu`` and wraps its ``exitInitializationMode`` so the gate runs
  once at ``start_time`` (a live run pauses there); it is passed as
  ``simulate_fmu(fmu_instance=...)`` and freed here afterwards (FMPy frees only instances it
  made). A ``False`` at ``start_time`` raises a private exception out of the wrapper; the
  engine catches it, records the one row at ``start_time`` with FMPy's ``Recorder``,
  terminates and frees the instance, and returns that row as a stopped result.
- An FMU that requests termination makes ``simulate_fmu`` return before ``stop_time`` without an
  error: ``stopped`` stays ``False`` and the worker applies ``base.ended_early``.

Collaborators: ``worker.py`` (the only caller), ``engines/base.py`` (the seam), ``signals.py``
(inputs DSL to FMPy's input array), ``fmu/inspect.py`` (variable types),
``store/protocol.py`` (``RunSpec``), ``config.Limits`` (``stall_repeats``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Final

import numpy as np
from fmpy import read_model_description, simulate_fmu
from fmpy.simulation import Recorder, instantiate_fmu

from agentic_fmi.config import Limits
from agentic_fmi.engines.base import (
    TIME_COLUMN,
    EngineResult,
    Live,
    LiveGate,
    reached,
    variable_units,
)
from agentic_fmi.errors import SetRefused
from agentic_fmi.fmu.inspect import variable_types
from agentic_fmi.signals import to_input_array
from agentic_fmi.store.protocol import RunSpec, SetValue

FMI2_KINDS: Final = {"Real": "Real", "Integer": "Integer", "Enumeration": "Integer"}
"""FMI 2.0 type -> the ``get``/``set`` suffix of ``FMU2Slave`` (Boolean is its own)."""
FMI3_KINDS: Final = {
    "Enumeration": "Int64",
    **{name: name for name in ("Float32", "Float64")},
    **{f"{sign}Int{bits}": f"{sign}Int{bits}" for sign in ("", "U") for bits in (8, 16, 32, 64)},
}
"""FMI 3.0 type -> the ``get``/``set`` suffix of ``FMU3Slave`` (Boolean is its own)."""


def simulate(spec: RunSpec, fmu_dir: Path, gate: LiveGate) -> EngineResult:
    """Simulate the FMU extracted in ``fmu_dir`` as ``spec`` says, calling ``gate`` each step.

    Calls ``fmpy.simulate_fmu(str(fmu_dir), ...)`` with: ``fmi_type``, ``solver``,
    ``start_time``/``stop_time``/``output_interval``/``relative_tolerance``/``step_size`` from
    ``spec.experiment``, ``start_values``, ``input`` from
    :func:`agentic_fmi.signals.to_input_array` (or ``None``), ``output=spec.outputs``,
    ``step_finished`` (the stall check, then ``gate(t, live)``), ``remote_platform=None`` (never
    wine or remoting), ``fmu_instance`` from :func:`_gated_instance` for a live spec, and **no**
    ``timeout`` (the worker's watchdog enforces it). The directory already holds
    ``modelDescription.xml``, so FMPy extracts nothing to a temporary directory. FMU log
    messages go to stdout, which the worker has redirected to ``log.txt``.

    Raises:
        RuntimeError: the FMU made no progress for ``Limits.stall_repeats`` steps.
        Exception: whatever FMPy or the FMU raises; the worker maps it to ``simulation_error``.
    """
    md = read_model_description(str(fmu_dir), validate=False)  # the server validated it
    experiment = spec.experiment
    inputs = to_input_array(
        spec.inputs, variable_types(md), experiment.start_time, experiment.stop_time
    )
    outputs = list(dict.fromkeys(spec.outputs))
    stall_repeats = Limits().stall_repeats
    refused_at: list[float] = []
    stalled_at: list[float] = []
    step_finished = _step_finished(md, gate, stall_repeats, refused_at, stalled_at)
    instance = _gated_instance(fmu_dir, md, experiment.start_time, gate) if spec.live else None
    with _owned(instance):
        try:
            raw = simulate_fmu(
                str(fmu_dir),
                model_description=md,
                fmi_type=spec.fmi_type,
                solver=spec.solver,
                start_time=experiment.start_time,
                stop_time=experiment.stop_time,
                output_interval=experiment.output_interval,
                relative_tolerance=experiment.relative_tolerance,
                step_size=experiment.step_size,
                start_values=dict(spec.start_values),
                input=inputs,
                output=outputs,
                step_finished=step_finished,
                remote_platform=None,
                fmu_instance=instance,
            )
        except _StoppedAtStart:
            raw = _start_row(instance, md, outputs, experiment.start_time)
            refused_at.append(experiment.start_time)
    if stalled_at:
        raise RuntimeError(
            f"the FMU made no progress at t={stalled_at[0]:g}: it discarded the step "
            f"{stall_repeats} times without terminating"
        )
    result, units = _columns(raw, outputs, variable_units(md))
    stopped = bool(refused_at) and not reached(refused_at[-1], experiment.stop_time)
    return EngineResult(result=result, stopped=stopped, units=units)


def _step_finished(
    md: Any, gate: LiveGate, stall_repeats: int, refused_at: list[float], stalled_at: list[float]
) -> Callable[[float, Any], bool]:
    """FMPy's ``step_finished``: the stall check, then ``gate(t, live)``. Records where the gate
    refused in ``refused_at`` and where a stall was found in ``stalled_at``. A closure over
    locals, because it runs once per step: a batch run pays a float comparison and a call."""
    last_t = math.nan
    repeats = 0
    live: _FmpyLive | None = None

    def step_finished(t: float, recorder: Any) -> bool:
        nonlocal last_t, repeats, live
        if t == last_t:
            repeats += 1
            if repeats >= stall_repeats:
                stalled_at.append(t)
                return False
        else:
            last_t = t
            repeats = 1
        if live is None:
            live = _FmpyLive(recorder.fmu, md)
        if gate(t, live):
            return True
        refused_at.append(t)
        return False

    return step_finished


class _StoppedAtStart(Exception):
    """The gate returned ``False`` at ``start_time`` (raised out of ``exitInitializationMode``)."""


@contextmanager
def _owned(instance: Any) -> Iterator[None]:
    """Free an instance we created when the block ends; on an error, without masking it."""
    try:
        yield
    except BaseException:
        if instance is not None:
            with suppress(Exception):
                instance.freeInstance()
        raise
    if instance is not None:
        instance.freeInstance()


def _start_row(instance: Any, md: Any, outputs: Sequence[str], start_time: float) -> np.ndarray:
    """The one row at ``start_time`` of a run stopped there, recorded the way ``simulate_fmu``
    records (FMPy's ``Recorder``), then ``terminate()``."""
    recorder = Recorder(fmu=instance, modelDescription=md, variableNames=list(outputs))
    recorder.sample(start_time, force=True)
    instance.terminate()
    return np.asarray(recorder.result())


class _FmpyLive:
    """:class:`~agentic_fmi.engines.base.Live` over an FMPy ``FMU2Slave``/``FMU3Slave``.

    Built once per run from the model description: name -> (value reference, type). ``get``
    and ``set`` group the names by type and make one FMI call per type: FMI 2.0
    ``getReal``/``getInteger``/``getBoolean`` (Enumeration as Integer), FMI 3.0 ``getFloat64``,
    ``getFloat32``, ``getInt8`` ... ``getUInt64``, ``getBoolean`` (Enumeration as Int64); the
    setters likewise. Co-Simulation sets act from the next ``doStep``. Scalars only (callers
    check with ``live.check_sets`` and record only scalar outputs in ``live.json``). The index
    and the per-name-list call plans are built on first use, so a batch run pays nothing.
    """

    def __init__(self, fmu: Any, md: Any) -> None:
        self._fmu = fmu
        self._md = md
        self._refs: dict[str, tuple[int, str]] | None = None
        self._plans: dict[tuple[str, ...], list[tuple[str, list[int], list[int]]]] = {}

    def get(self, names: Sequence[str]) -> list[float | None]:
        """See :meth:`Live.get`."""
        values: list[float | None] = [None] * len(names)
        for kind, indices, references in self._plan(names):
            read = getattr(self._fmu, f"get{kind}")(references)
            for index, value in zip(indices, read, strict=True):
                number = float(value)
                values[index] = number if math.isfinite(number) else None
        return values

    def set(self, values: Mapping[str, SetValue]) -> None:
        """See :meth:`Live.set`; an FMI error status raises ``SetRefused`` naming the variables
        of the failing type group. Its ``applied`` lists the groups set before it (a group is
        one FMI call, so the failing group's own variables are not listed)."""
        names = list(values)
        applied: dict[str, SetValue] = {}
        for kind, indices, references in self._plan(names):
            convert: Callable[[SetValue], Any] = (
                bool if kind == "Boolean" else float if kind.startswith(("Real", "Float")) else int
            )
            batch = [convert(values[names[index]]) for index in indices]
            try:
                getattr(self._fmu, f"set{kind}")(references, batch)
            except Exception as error:
                refused = ", ".join(names[index] for index in indices)
                raise SetRefused(f"the FMU refused to set {refused}: {error}", applied) from error
            applied.update((names[index], values[names[index]]) for index in indices)

    def _plan(self, names: Sequence[str]) -> list[tuple[str, list[int], list[int]]]:
        """``(kind, positions in names, value references)`` per FMI call for ``names``."""
        key = tuple(names)
        plan = self._plans.get(key)
        if plan is None:
            refs = self._index()
            groups: dict[str, tuple[list[int], list[int]]] = {}
            for position, name in enumerate(key):
                if name not in refs:
                    raise RuntimeError(f"{name} is not a scalar numeric or Boolean variable")
                reference, kind = refs[name]
                indices, references = groups.setdefault(kind, ([], []))
                indices.append(position)
                references.append(reference)
            plan = [(kind, indices, references) for kind, (indices, references) in groups.items()]
            self._plans[key] = plan
        return plan

    def _index(self) -> dict[str, tuple[int, str]]:
        if self._refs is None:
            kinds = FMI3_KINDS if self._md.fmiVersion.startswith("3") else FMI2_KINDS
            self._refs = {
                variable.name: (variable.valueReference, kind)
                for variable in self._md.modelVariables
                if not variable.dimensions
                and (kind := "Boolean" if variable.type == "Boolean" else kinds.get(variable.type))
            }
        return self._refs


def _gated_instance(fmu_dir: Path, md: Any, start_time: float, gate: LiveGate) -> Any:
    """A Co-Simulation instance (``fmpy.instantiate_fmu(str(fmu_dir), md, "CoSimulation")``)
    whose ``exitInitializationMode`` calls the original, then ``gate(start_time,
    _FmpyLive(instance, md))``; a ``False`` raises the private stop-at-start exception
    (spike (i) item 2). The caller passes it as ``simulate_fmu(fmu_instance=...)`` and must call
    ``freeInstance()`` itself afterwards, also on errors."""
    instance = instantiate_fmu(str(fmu_dir), md, "CoSimulation")
    live: Live = _FmpyLive(instance, md)
    original = instance.exitInitializationMode

    def exit_then_gate(*args: Any, **kwargs: Any) -> Any:
        status = original(*args, **kwargs)
        if not gate(start_time, live):
            raise _StoppedAtStart(start_time)
        return status

    instance.exitInitializationMode = exit_then_gate
    return instance


def _columns(
    raw: np.ndarray, outputs: Sequence[str], units: Mapping[str, str | None]
) -> tuple[np.ndarray, dict[str, str | None]]:
    """FMPy's result reordered to ``time`` plus ``outputs``, arrays flattened, with units.

    FMPy groups its columns by type, not in the requested order, and keeps array variables as
    sub-arrays; ``result.csv`` needs one scalar column per name.
    """
    recorded = set(raw.dtype.names or ())
    columns: list[tuple[str, np.ndarray]] = [(TIME_COLUMN, raw[TIME_COLUMN])]
    column_units: dict[str, str | None] = {}
    for name in outputs:
        if name not in recorded:
            continue
        for label, values in _flatten(name, raw[name]):
            columns.append((label, values))
            column_units[label] = units.get(name)
    result = np.empty(len(raw), dtype=[(label, values.dtype) for label, values in columns])
    for label, values in columns:
        result[label] = values
    return result, column_units


def _flatten(name: str, values: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """One ``(label, column)`` per element of an array variable; a scalar stays as it is."""
    if values.ndim == 1:
        return [(name, values)]
    return [
        (f"{name}[{','.join(str(i + 1) for i in index)}]", values[(slice(None), *index)])
        for index in np.ndindex(values.shape[1:])
    ]
