"""The OMSimulator engine: run one multi-FMU system described by a ``SystemSpec`` (ADR 0009).

The engine signature of ``engines/base.py``: ``simulate(spec, work_dir, gate) ->
EngineResult``, the same result as the FMPy engine, so analysis, plots and comparison need no
system-specific code.

One way to run a system: the model is built from the resolved spec through the OMSimulator 3
API, never by loading a user's SSP. SSD and SSV are import and export formats the server
handles (``ssp.py``). Facts this module relies on (spikes (g) and (h),
OMSimulator 3.0.0.post198):

- ``SSP(temp_dir=...)``, ``addResource(path, new_name="resources/<c>.fmu")``,
  ``addComponent(CRef("default", c), "resources/<c>.fmu")``, ``addConnection(CRef(...),
  CRef(...))``, ``newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": h})`` and
  ``setSolver(CRef("default", c), "ma")`` for every component, ``activeVariant.stopTime``;
  then ``instantiate()`` (one model per process: the model name is hard-coded).
- ``initialize()`` starts the clock at 0 whatever the start time (``activeVariant.startTime``
  and ``setStartTime`` are recorded but ignored), so a non-zero ``start_time`` is refused.
- ``inst.setResultFile("")`` after ``instantiate()`` turns the result file off ("No result file
  will be created"). Without it OMSimulator writes ``default_res.mat`` into the process's
  working directory; ``removeSignalsFromResults(".*")`` alone does not stop that. With the file
  off, FMI 3.0 Binary variables cost nothing (no 50x slowdown).
- ``inst.setValue(CRef(...), v)`` between ``instantiate()`` and ``initialize()`` returns
  ``Status.ok`` and takes effect; for a variable it refuses (neither an input nor
  ``initial="exact"``) it only issues a ``RuntimeWarning`` and returns ``None``. It does not
  refuse an output with ``initial="exact"``, so the server limits what may be set.
- ``inst.doStep()`` advances one communication step; ``inst.getValue(CRef(...))`` afterwards
  returns **post-exchange** values (a connected input equals its source output at the same
  time). Never ``stepUntil``: it can overshoot by one step. ``getTime()`` accumulates ``t += h``
  (within 1e-15 of ``k*h`` over 100 steps, 2e-14 over 300); the last step is clipped to the
  stop time; a ``doStep()`` at the stop time is a no-op.
- A component that discards a step (a pythonfmu ``do_step`` returning ``False``) only logs
  ``info: fmi2_doStep discarded``; ``doStep()`` doesn't raise and the clock stops advancing, so
  a step that makes no progress raises here. A pythonfmu slave that *raises* (in ``__init__``
  or ``do_step``) aborts the process inside OMSimulator (``std::logic_error``); nothing here can
  catch that, and the server's takeover records ``worker_lost`` with the line in ``log_tail``.
- ``getValue`` maps FMI 3.0 Float32/Float64 to Real and the integer types to Integer, and
  ignores the read status; a Binary read returns 0.0 and logs ``error: [getReal] UnSupported
  Numeric Type``. So ``String``, ``Binary`` and ``Clock`` outputs are left out, as FMPy does.
- Each ``instantiate()`` leaves a ``tempfile.mkdtemp()`` directory (the extracted FMUs, and the
  Python sources of pythonfmu FMUs, which load into this interpreter) under ``TMPDIR``.
- OMSimulator logs ``info:``/``warning:`` lines to stdout and ``error:`` lines to stderr (both
  ``log.txt`` in a worker). An exception left unwound with a live model can crash the
  interpreter at exit (SIGSEGV), so ``terminate()`` and ``delete()`` always run.

Live system runs (ADR 0010; spike (i) items 7 to 9) use the same loop:
the gate is ``gate(t, live)`` with :class:`_OmsLive` over the instance, and a live spec also
calls the gate once after ``initialize()`` and row 0, before the first ``doStep()``, so a live
system run pauses at ``start_time`` as an FMU run does (a ``False`` there returns row 0 as a
stopped result). A batch spec skips that call: its loop is the plain batch loop. While the gate
waits nothing steps (pausing is simply not calling ``doStep()``; OMSimulator has no internal
timeout). ``setValue`` after ``initialize()`` works on tunable parameters and unconnected
inputs (from the next ``doStep()``, persistent); a connected input is overwritten by the next
exchange (a one-step pulse) and an output, a fixed parameter or a constant is refused with a
``RuntimeWarning`` and a ``None`` status, so ``live.check_sets`` (in the gate) refuses those
first and :meth:`_OmsLive.set` raises ``SetRefused`` (a ``RuntimeError``) on any refusal that
gets through. ``setValue`` and ``getValue`` pick the FMI call from OMSimulator's own variable
type (Real; Integer, with Enumeration as Integer; Boolean), so the values ``live.check_sets``
converted to the variable's type family pass through unchanged. A discard and an FMU's terminate
request are not told apart here. FMI 2.0 reports both as a clock that stops, which the existing
no-progress check turns into ``simulation_error``. FMI 3.0 ignores the terminate request and
keeps stepping: the run goes on until the FMU itself fails (``simulation_error``) or reaches
``stop_time``.

Runs only in a worker process, after ``worker.py`` has verified the OMSimulator pin
(``oms_pin``) and copied every FMU to ``work_dir`` while hashing it. The only module that may
``import OMSimulator`` (ruff TID251), and it does so inside :func:`simulate`, so importing this
module loads nothing.

Collaborators: ``engines/base.py`` (seam), ``worker.py`` (the only caller),
``store/protocol.SystemSpec``, ``store/runstore.SYSTEM_FMUS_DIR``; FMPy's
``read_model_description`` for units and types (XML only).
"""

from __future__ import annotations

import math
import warnings
from array import array
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import numpy as np
from fmpy import read_model_description

from agentic_fmi.engines.base import (
    TIME_COLUMN,
    EngineResult,
    Live,
    LiveGate,
    variable_units,
)
from agentic_fmi.errors import SetRefused
from agentic_fmi.store.protocol import SetValue, StartValue, SystemSpec
from agentic_fmi.store.runstore import SYSTEM_FMUS_DIR

SYSTEM_NAME = "default"
"""The system name the API model uses in every ``CRef``."""

SOLVER_NAME = "ma"
"""The one fixed-step master shared by all components."""

SSP_TEMP_DIR = "ssp"
"""``work_dir/ssp``: the temp directory of the ``SSP`` object (its private FMU copies)."""

UNRECORDABLE_TYPES = frozenset({"String", "Binary", "Clock"})
"""FMPy type names of variables ``getValue`` can't sample as a number."""


def simulate(spec: SystemSpec, work_dir: Path, gate: LiveGate) -> EngineResult:
    """Simulate the system in ``spec`` with OMSimulator, calling ``gate`` after every step.

    ``work_dir`` is the run's ``work/``: the worker has copied each component's FMU to
    ``work_dir/fmus/<component>.fmu`` (``runstore.SYSTEM_FMUS_DIR``) while hashing it, and set
    ``TMPDIR`` and ``tempfile.tempdir`` to ``work_dir/tmp`` before calling this. The SSP's own
    temp directory is ``work_dir/ssp``.

    Sequence:

    1. Build the model as listed in the module docstring, ``h = spec.master.step_size``,
       ``stopTime`` from ``spec.experiment``; ``instantiate()``; ``setResultFile("")``.
    2. Set every ``spec.parameters`` value with ``setValue``; a ``RuntimeWarning`` or a status
       other than ``Status.ok`` raises naming the variable (the worker records
       ``simulation_error``).
    3. ``initialize()``; record row 0 at ``getTime()`` (it must be ``start_time``). A live
       spec then calls ``gate(start_time, live)``; ``False`` returns row 0 as a stopped result.
    4. While ``getTime() < stop_time - h/2``: ``doStep()``, record a row at ``getTime()`` (the
       outputs' ``getValue``, as float64), then ``gate(t, live)`` with :class:`_OmsLive`;
       ``False`` stops (``stopped`` unless the loop was done anyway). A step that doesn't
       advance the clock raises.
    5. Always ``terminate()`` if initialized, then ``delete()``; if a step raised, errors of
       this cleanup are dropped so the step's error is the one reported.

    Returns:
        ``time`` plus ``spec.outputs`` in order (duplicates once; ``String``, ``Binary`` and
        ``Clock`` outputs left out), units from each component's ``modelDescription.xml``
        (:func:`~agentic_fmi.engines.base.variable_units`).

    Raises:
        Exception: whatever OMSimulator or an FMU raises; the worker maps it to
            ``simulation_error``.
    """
    from OMSimulator import SSP, CRef  # lazily: importing loads libOMSimulator.so
    from OMSimulator.capi import Status

    fmus = work_dir / SYSTEM_FMUS_DIR
    outputs, units = _recordable_outputs(spec, fmus)
    model = _build_model(SSP, CRef, spec, fmus, work_dir / SSP_TEMP_DIR)
    inst = model.instantiate()
    initialized = False
    try:
        inst.setResultFile("")
        for ref, resolved in spec.parameters.items():
            cref = CRef(SYSTEM_NAME, *_split(ref))
            _set_parameter(inst, cref, ref, resolved.value, Status.ok)
        inst.initialize()
        initialized = True
        crefs = [CRef(SYSTEM_NAME, *_split(ref)) for ref in outputs]
        live = _OmsLive(inst, CRef, Status.ok)
        columns, stopped = _step(inst, crefs, spec, gate, live)
    except BaseException:
        with suppress(Exception):  # keep the original error: it says what went wrong
            _release(inst, initialized)
        raise
    _release(inst, initialized)
    return EngineResult(result=_result(outputs, columns), stopped=stopped, units=units)


def _split(ref: str) -> tuple[str, str]:
    """``"PI.u"`` -> ``("PI", "u")``: split at the first dot (``body.v[1]`` keeps its dots)."""
    component, variable = ref.split(".", 1)
    return component, variable


def _recordable_outputs(spec: SystemSpec, fmus: Path) -> tuple[list[str], dict[str, str | None]]:
    """``spec.outputs`` without duplicates and unrecordable types, and the unit of each."""
    types: dict[str, dict[str, str]] = {}
    units: dict[str, dict[str, str | None]] = {}
    for name in spec.components:
        md = read_model_description(str(fmus / f"{name}.fmu"), validate=False)
        types[name] = {variable.name: variable.type for variable in md.modelVariables}
        units[name] = variable_units(md)
    outputs: list[str] = []
    column_units: dict[str, str | None] = {}
    for ref in dict.fromkeys(spec.outputs):
        component, variable = _split(ref)
        if types.get(component, {}).get(variable) in UNRECORDABLE_TYPES:
            continue
        outputs.append(ref)
        column_units[ref] = units.get(component, {}).get(variable)
    return outputs, column_units


def _build_model(ssp: Any, cref: Any, spec: SystemSpec, fmus: Path, temp_dir: Path) -> Any:
    """The OMSimulator ``SSP`` of ``spec``: components, connections, one ``oms_ma`` master."""
    model = ssp(temp_dir=str(temp_dir))
    for name in spec.components:
        resource = f"resources/{name}.fmu"
        model.addResource(str(fmus / f"{name}.fmu"), new_name=resource)
        model.addComponent(cref(SYSTEM_NAME, name), resource)
    for connection in spec.connections:
        model.addConnection(
            cref(SYSTEM_NAME, *_split(connection.from_)), cref(SYSTEM_NAME, *_split(connection.to))
        )
    master = spec.master
    model.newSolver(
        {"name": SOLVER_NAME, "method": master.method, "fixedStepSize": master.step_size}
    )
    for name in spec.components:
        model.setSolver(cref(SYSTEM_NAME, name), SOLVER_NAME)
    model.activeVariant.startTime = spec.experiment.start_time
    model.activeVariant.stopTime = spec.experiment.stop_time
    return model


def _set_parameter(inst: Any, cref: Any, ref: str, value: StartValue | SetValue, ok: Any) -> None:
    """``setValue`` (before ``initialize()``, or live between steps), checked against ``ok``
    (``Status.ok``): OMSimulator signals a refusal only with a ``RuntimeWarning`` and a ``None``
    status; either raises ``RuntimeError`` naming ``ref``."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        status = inst.setValue(cref, value)
    refusals = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    if refusals or status != ok:
        reason = refusals[0].split(". ", 1)[-1] if refusals else f"status {status}"
        raise RuntimeError(f"OMSimulator did not set {ref} = {value!r}: {reason}")


def _step(
    inst: Any, crefs: Sequence[Any], spec: SystemSpec, gate: LiveGate, live: Live
) -> tuple[list[array[float]], bool]:
    """Row 0 after ``initialize()``, then one row per ``doStep()``; the columns (time first)
    and whether the gate stopped the run before the end."""
    columns: list[array[float]] = [array("d") for _ in range(len(crefs) + 1)]

    def record() -> float:
        t = float(inst.getTime())
        columns[0].append(t)
        for column, cref in zip(columns[1:], crefs, strict=True):
            column.append(float(inst.getValue(cref)))
        return t

    start, stop = spec.experiment.start_time, spec.experiment.stop_time
    t = record()
    if not math.isclose(t, start, rel_tol=1e-9, abs_tol=1e-12):
        raise RuntimeError(
            f"OMSimulator starts systems at t={t:g}, not at start_time={start:g}; "
            "start the run at 0"
        )
    end = stop - spec.master.step_size / 2  # no sliver step at the stop time
    if spec.live and not gate(t, live):  # a live run pauses at start_time
        return columns, t < end
    while t < end:
        inst.doStep()
        previous, t = t, record()
        if t <= previous:
            raise RuntimeError(
                f"the system made no progress at t={t:g}: a component discarded or failed its "
                "step (see the log)"
            )
        if not gate(t, live):
            return columns, t < end
    return columns, False


class _OmsLive:
    """:class:`~agentic_fmi.engines.base.Live` over an instantiated, initialized OMSimulator
    model. Names are ``<component>.<variable>``, each turned into a ``CRef`` once (the gate
    reads the same column names at every step). ``get`` is ``float(inst.getValue(CRef))`` per
    name (``None`` if non-finite; a Boolean reads 0.0 or 1.0); ``set`` is :func:`_set_parameter`
    per value, in order (a ``RuntimeWarning`` or a status other than ``Status.ok`` raises
    :class:`~agentic_fmi.errors.SetRefused` naming the variable; values set before it stay
    set and are listed in the exception's ``applied``)."""

    def __init__(self, inst: Any, cref: Any, ok: Any) -> None:
        self._inst = inst
        self._cref = cref
        self._ok = ok
        self._crefs: dict[str, Any] = {}

    def get(self, names: Sequence[str]) -> list[float | None]:
        """See :meth:`Live.get`."""
        values: list[float | None] = []
        for name in names:
            number = float(self._inst.getValue(self._ref(name)))
            values.append(number if math.isfinite(number) else None)
        return values

    def set(self, values: Mapping[str, SetValue]) -> None:
        """See :meth:`Live.set`; the refusal's ``applied`` lists the values set before it."""
        applied: dict[str, SetValue] = {}
        for name, value in values.items():
            try:
                _set_parameter(self._inst, self._ref(name), name, value, self._ok)
            except RuntimeError as error:
                raise SetRefused(str(error), applied) from error
            applied[name] = value

    def _ref(self, name: str) -> Any:
        """The ``CRef`` of ``name`` (``default``, component, variable), made on first use."""
        ref = self._crefs.get(name)
        if ref is None:
            ref = self._crefs[name] = self._cref(SYSTEM_NAME, *_split(name))
        return ref


def _release(inst: Any, initialized: bool) -> None:
    """``terminate()`` (if initialized), then ``delete()`` even if terminating failed."""
    try:
        if initialized:
            inst.terminate()
    finally:
        inst.delete()


def _result(outputs: Sequence[str], columns: Sequence[array[float]]) -> np.ndarray:
    """The structured result: ``time`` then ``outputs``, all float64."""
    names = [TIME_COLUMN, *outputs]
    result = np.empty(len(columns[0]), dtype=[(name, np.float64) for name in names])
    for name, column in zip(names, columns, strict=True):
        result[name] = np.frombuffer(column, dtype=np.float64)
    return result
