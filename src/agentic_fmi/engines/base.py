"""The engine seam: what every engine takes and returns (ADR 0009).

There is no ``Engine`` class or ``Instance`` protocol. Each engine is one module with one
function, ``simulate(spec, directory, gate) -> EngineResult``:

- ``engines.fmpy_engine.simulate(spec: RunSpec, fmu_dir, gate)`` runs one FMU with FMPy.
- ``engines.oms_engine.simulate(spec: SystemSpec, work_dir, gate)`` runs a system with
  OMSimulator.

``worker.py`` picks one by the spec's ``kind``. Both call ``gate(t, live)`` after every
communication (or output) step, stop cleanly when it returns ``False``, and return the same
:class:`EngineResult`, so the worker, ``result.csv``, the summary, ``run_results``,
``run_plot`` and ``run_compare`` treat both alike. The shared contract tests
(``tests/worker/test_engine_contract.py``, through real workers) hold both engines to this
module's promises.

Runs only inside a worker process (ADR 0002). Imports no engine library itself.

Phase 4 (ADR 0010) amends the seam without changing ``simulate``'s signature: the gate becomes
:data:`LiveGate`, ``gate(t, live)``, where ``live`` is the engine's :class:`Live` handle on the
running simulation (``get`` and ``set`` between steps). Both engines implement :class:`Live`
(``fmpy_engine._FmpyLive`` over ``recorder.fmu``, ``oms_engine._OmsLive`` over the OMSimulator
instance); a refused ``set`` raises :class:`~agentic_fmi.errors.SetRefused`, which lists the
values already applied. For a live spec (``spec.live``) both also call the gate once at
``start_time``, after initialization and before the first step, so a live run pauses at t0.
Engine-neutral outcomes, held by the contract tests: a step that never advances (an FMU
discarding without terminating) is ``simulation_error``; an FMU that requests termination ends
``finished`` early (the worker records "ended early at t" as ``status.reason``,
:func:`ended_early`) where the engine can tell the two apart (FMPy). OMSimulator never reports
it: with FMI 2.0 the clock stalls like a discard (``simulation_error``); with FMI 3.0 it ignores
the request and keeps stepping, so the run goes on until the FMU itself fails
(``simulation_error``) or reaches ``stop_time``. Batch runs get the same gate signature (the
worker's batch gate ignores ``live``).

Collaborators: ``engines/fmpy_engine.py`` and ``engines/oms_engine.py`` (the two engines),
``worker.py`` (the only caller), ``live.py`` (the live gate that uses :class:`Live`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from agentic_fmi.store.protocol import SetValue


class Live(Protocol):
    """The gate's handle on a running simulation at a communication point (ADR 0010).

    Two implementations: ``engines.fmpy_engine._FmpyLive`` (the FMU instance FMPy hands to
    ``step_finished`` as ``recorder.fmu``, or the instance the engine created for a live run)
    and ``engines.oms_engine._OmsLive`` (the OMSimulator instance). Valid only inside the gate
    call, in the simulation thread; never keep it. Names are the run's own: a variable name for
    one FMU, ``<component>.<variable>`` for a system. Callers check names and values first
    (``live.check_sets``); an engine refusal still raises.
    """

    def get(self, names: Sequence[str]) -> list[float | None]:
        """Current values of scalar numeric or Boolean variables as floats, in ``names`` order
        (``None`` for a non-finite value)."""
        ...

    def set(self, values: Mapping[str, SetValue]) -> None:
        """Set inputs and tunable parameters; they act from the next step. Raises
        :class:`~agentic_fmi.errors.SetRefused` (a ``RuntimeError`` naming the variable) if the
        FMU or OMSimulator refuses one; the values set before it stay set and are listed in
        ``SetRefused.applied``."""
        ...


LiveGate = Callable[[float, Live], bool]
"""The gate (ADR 0010): ``gate(t, live)`` after every communication step (and, for a live spec,
once at ``start_time``) with the simulation time reached; ``False`` stops the run cleanly (the
engine then terminates and frees its instances). The worker's batch gate ignores ``live``;
``live.LiveControl.gate`` reads, sets, waits and paces through it."""


def ended_early(t_end: float, stop_time: float, stopped: bool, step: float = 0.0) -> bool:
    """The early-end rule (ADR 0010): a result that ends before ``stop_time`` although nobody
    stopped the run means the FMU ended it (FMPy returns early on an FMU-requested terminate).
    The worker then writes ``finished`` with ``status.reason`` "ended early at t=...".

    A result that ends less than one communication ``step`` short is not early: FMPy stops at
    the last grid point when the next one would pass ``stop_time`` and the FMU can't shorten
    its step (an FMU that ends itself within that last step can't be told apart).
    """
    if stopped or reached(t_end, stop_time):
        return False
    return stop_time - t_end >= step * (1.0 - 1e-9)


TIME_COLUMN = "time"


@dataclass(frozen=True)
class EngineResult:
    """What a simulation produced."""

    result: np.ndarray
    """Structured array: ``time`` first, then the spec's ``outputs`` in the order given. An
    array variable becomes one column per element (``y[1]``, ``A[1,2]``, 1-based); a variable
    the engine can't record (``String``, ``Binary``, ``Clock``) is left out. A system run's
    columns are ``<component>.<variable>``, sampled after each exchange (post-exchange
    values)."""
    stopped: bool
    """True if the gate returned ``False`` before ``stop_time``."""
    units: Mapping[str, str | None] = field(default_factory=dict)
    """Unit of each output column (the variable's own unit, else its declared type's)."""


def reached(t: float, stop_time: float) -> bool:
    """Whether ``t`` is at or past ``stop_time``, up to rounding of the time grid."""
    return t >= stop_time or math.isclose(t, stop_time, rel_tol=1e-9, abs_tol=1e-12)


def variable_units(md: Any) -> dict[str, str | None]:
    """Variable name -> unit from an FMPy ``ModelDescription``: its own ``unit``, else its
    declared type's, else ``None``."""
    units: dict[str, str | None] = {}
    for variable in md.modelVariables:
        declared = variable.declaredType
        units[variable.name] = variable.unit or getattr(declared, "unit", None)
    return units
