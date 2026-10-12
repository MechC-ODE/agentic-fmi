"""Run use cases behind the Run tools: start, status, stop, list, results, compare, plot, and
the ``fmu_build`` job (``start_build``, ADR 0008), which uses the same store and supervisor.
``run_start(system=...)`` starts a system run (``kind: "system"``, ADR 0009) through
``system.plan_run``; results, plots and comparisons treat it like a run.

Each function validates the request against ``modelDescription.xml`` (a build: against
``build.check_request``), works through the run store and the supervisor, and returns a compact
pydantic model within the ``Limits`` budgets. They raise :class:`agentic_fmi.errors.UserError`
for anything the caller can fix; the MCP layer only translates. A later ``afmi run`` CLI can
reuse them unchanged. ``run_status``, ``run_stop`` and ``run_list`` serve build jobs too; the
result tools refuse them (a build has no time series). ``start_run(live=True)`` creates a live
run (ADR 0010); talking to it afterwards (``run_control``, ``run_status(values)``) is
``control.py``, which builds its views with :func:`view_of`.

Collaborators: ``fmu/catalog.py`` and ``fmu/inspect.py`` (resolution, runnability, experiment,
variable checks), ``signals.py`` (inputs check), ``build.py`` (build validation and spec),
``system.py`` and ``ssp.py`` (system runs: load, check, parameter layers, ``.ssv`` reading),
``oms_pin.py`` (the OMSimulator pin, checked before a system run is created),
``store/runstore.py`` and ``store/protocol.py`` (documents), ``supervisor.Supervisor`` (spawn,
reconcile, stop), ``analysis/`` (summaries, LTTB, comparison), ``plots.py`` (PNG), ``budget.py``
(output budgets).

Admission: ``start_run`` checks ``max_workers`` and claims the run ID under the supervisor's
``admission`` lock (one per server), so two threads of a server can't both take the last slot;
``start_build`` also checks there that no queued or running build has the same name, so two
threads can't both start it. Two servers sharing a store can, for a moment (the store lock is
not held across the supervisor's own locking); the overshoot is at most one run per extra
server and ends when a run finishes.

Output budgets: every result is cut to ``Limits.json_budget_bytes`` here, with ``budget.py``
(sizes, ``fit_to_budget``, page cuts); the strategies for ``RunView``, ``RunResults`` and
``Comparison`` stay in this module next to their models.

Never loads FMU binaries and never imports ``engines/`` (ADR 0002).
"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

import numpy as np
from pydantic import BaseModel, ValidationError

from agentic_fmi import budget, build, oms_pin, ssp
from agentic_fmi import system as systems
from agentic_fmi.analysis import compare as compare_analysis
from agentic_fmi.analysis.compare import Comparison, SimulationSpec, SpecDifference
from agentic_fmi.analysis.downsample import lttb_indices
from agentic_fmi.analysis.summary import TIME_COLUMN, round_sig
from agentic_fmi.config import Config, Limits, Timing
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.catalog import page_window, relative_path, resolve_fmu
from agentic_fmi.fmu.inspect import (
    FmiType,
    ModelDescription,
    check_run,
    default_fmi_type,
    read_description,
    require_variables,
    resolve_experiment,
    sha256_file,
    variable_types,
)
from agentic_fmi.plots import Line, Panel, render_png
from agentic_fmi.signals import check_inputs, to_input_array
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import atomic_open, locked
from agentic_fmi.store.protocol import (
    TERMINAL_STATES,
    BuildRecord,
    BuildSpec,
    ErrorCode,
    Event,
    EventType,
    Experiment,
    FmuRef,
    InputSignal,
    LiveColumn,
    OmsVersion,
    RunError,
    RunSpec,
    RunState,
    RunStatus,
    RunSummary,
    StartValue,
    SystemSpec,
    VariableSummary,
    Versions,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor

M = TypeVar("M", bound=BaseModel)

RESULT_STATES = frozenset({RunState.FINISHED, RunState.STOPPED})
"""States that have a result to read."""

DEFAULT_RESULT_VARIABLES = 4
"""Variables ``run_results`` returns when the caller names none (the first recorded ones)."""

MIN_POINTS = 4
"""The fewest points per variable LTTB keeps (both endpoints and the extrema)."""

RECORDABLE_TYPES = frozenset(
    {"Real", "Integer", "Boolean", "Enumeration", "Float32", "Float64"}
    | {f"{prefix}Int{bits}" for prefix in ("", "U") for bits in (8, 16, 32, 64)}
)
"""Variable types FMPy records; String, Binary and Clock variables are skipped by the engine."""

NOT_STATE_CAUSALITIES = frozenset(
    {"parameter", "calculatedParameter", "structuralParameter", "independent"}
)
"""Causalities left out of the default outputs of an FMU that declares none."""

MAX_SPEC_DIFFERENCES = 25
DIFFERENCES_NOTE = "Showing only the first"
MAX_DIFFERENCE_CHARS = 120
"""Longest value of a spec difference (an input table can be much longer)."""

MAX_LABEL_PARTS = 3
MAX_LABEL_VALUE_CHARS = 24
LABEL_SKIPPED_FIELDS = frozenset({"timeout_s", "force"})
"""Spec fields that never go into a legend label."""
LABEL_SKIPPED_PREFIXES = ("versions.", "system.", "paramset.", "omsimulator.")
"""Spec field groups that never go into a legend label (tool versions, and the hashes of a
system run's files and OMSimulator install)."""
LABEL_PREFIXES = ("start_values.", "parameters.")
"""Prefixes dropped from a field name in a label: ``PI.Kp=12`` for ``parameters.PI.Kp``."""

MAX_PLOT_FILE_CHARS = 120
PLOT_SUFFIX = ".png"
_UNSAFE_FILE_CHARS = re.compile(r"[^A-Za-z0-9._+-]")


class CompactSummary(BaseModel):
    """A finished or stopped run's summary, trimmed to ``Limits.inspect_max_items`` variables."""

    t_end: float
    n_points: int
    wall_time_s: float
    variables: list[VariableSummary]
    truncated: bool = False


class RunView(BaseModel):
    """What ``run_start``, ``run_status``, ``run_stop`` and ``fmu_build`` return.

    A system run (``kind: "system"``, ADR 0009) has ``fmu`` = the system file's path and
    ``fmi_type`` "CoSimulation"; its summary's variables are ``<component>.<variable>``.

    A live run (ADR 0010) also has ``speed``; its ``state`` may be ``paused``, and an ended run's
    ``status.reason`` leads the ``hint``. ``run_status(values=[...])`` adds ``values``.

    A build job (``kind: "build"``, ADR 0008) has no ``fmi_type``, ``t`` or ``t_stop``; ``fmu``
    is its output FMU, ``progress`` is 0 until it finishes, a failed build's ``error.message``
    holds the diagnostics, and a finished one carries ``build``. ``build`` is published as a
    plain object, not a typed schema, because ``RunView``'s schema appears in four tools and
    ``tools/list`` must stay under 32 KB.
    """

    run_id: str
    kind: Literal["run", "build", "system"] = "run"
    state: RunState
    fmu: str
    """Workspace-relative FMU path (absolute if the store is elsewhere); for a system run, the
    system file's."""
    fmi_type: str | None = None
    """Simulation runs only."""
    t: float | None = None
    t_stop: float | None = None
    """Simulation runs only."""
    progress: float
    error: RunError | None = None
    summary: CompactSummary | None = None
    build: dict[str, Any] | None = None
    """Finished builds only: ``sha256``, ``model_identifier``, ``interfaces``, ``counts``,
    ``backend``, ``duration_s``, at most ``Limits.build_max_warnings`` ``warnings`` and, after
    an overwrite, ``replaced_sha256`` (from ``runs/<id>/build.json``)."""
    hint: str | None = None
    """The obvious next call, e.g. "Poll with run_status('r3')" or "Use run_plot or run_results"."""
    speed: float | None = None
    """Live runs (ADR 0010): the pacing factor, 1 = real time."""
    values: list[LiveColumn] | None = None
    """``run_status(values=[...])`` only: the named outputs' current values with units, from
    ``live.json`` (``control.run_values``)."""


class RunListItem(BaseModel):
    run_id: str
    kind: Literal["run", "build", "system"] = "run"
    state: RunState
    fmu_name: str
    """The FMU's model name; for a build job, the build name; for a system run, its name."""
    created_at: datetime


class RunPage(BaseModel):
    """Runs, newest first."""

    total: int
    offset: int
    limit: int
    items: list[RunListItem]
    next_offset: int | None = None


class Series(BaseModel):
    """One variable, downsampled with LTTB and rounded to ``Limits.significant_digits``."""

    name: str
    unit: str | None = None
    t: list[float]
    y: list[float | None]


class RunResults(BaseModel):
    """The ``run_results`` result. Never the raw series: at most ``max_points`` per variable."""

    run_id: str
    t_start: float
    t_end: float
    n_points_total: int
    series: list[Series]
    note: str | None = None
    """Set when points were reduced to fit the JSON budget, with what to do instead."""


@dataclass(frozen=True)
class Plot:
    """A rendered ``run_plot`` image and where it was saved."""

    png: bytes
    path: Path
    caption: str


# --- run_start ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """A validated run request that becomes ``spec.json`` once it has a run ID."""

    fmu: FmuRef
    fmi_type: FmiType
    solver: Literal["CVode", "Euler"]
    experiment: Experiment
    start_values: dict[str, StartValue]
    inputs: dict[str, InputSignal]
    outputs: list[str]
    timeout_s: float
    estimated_steps: float | None
    force: bool
    live: bool = False
    """A live run (ADR 0010): the spec records ``live`` and ``idle_limit_s``."""
    idle_limit_s: float | None = None
    """``Timing.live_idle_limit_s`` for a live run, else ``None``."""

    def spec(self, run_id: str) -> RunSpec:
        """The spec for ``run_id`` (stamped now, with the current tool versions)."""
        return RunSpec(
            run_id=run_id,
            created_at=utc_now(),
            fmu=self.fmu,
            fmi_type=self.fmi_type,
            solver=self.solver,
            experiment=self.experiment,
            start_values=self.start_values,
            inputs=self.inputs,
            outputs=self.outputs,
            timeout_s=self.timeout_s,
            estimated_steps=self.estimated_steps,
            force=self.force,
            versions=Versions.current(),
            live=self.live,
            idle_limit_s=self.idle_limit_s,
        )


def start_run(
    config: Config,
    supervisor: Supervisor,
    fmu: str | None = None,
    *,
    system: str | None = None,
    paramset: str | None = None,
    start_time: float | None = None,
    stop_time: float | None = None,
    output_interval: float | None = None,
    relative_tolerance: float | None = None,
    start_values: Mapping[str, StartValue] | None = None,
    inputs: Mapping[str, InputSignal] | None = None,
    outputs: Sequence[str] | None = None,
    fmi_type: Literal["CoSimulation", "ModelExchange"] | None = None,
    solver: Literal["CVode", "Euler"] | None = None,
    timeout_s: float | None = None,
    wait_s: float | None = None,
    force: bool = False,
    live: bool = False,
) -> RunView:
    """Validate, create and spawn a run, then wait up to ``wait_s`` for it to finish.

    Exactly one of ``fmu`` and ``system`` is required; ``system`` or ``paramset`` hands the
    request to :func:`start_system_run` (ADR 0009). ``solver`` defaults to ``"CVode"``.

    ``live=True`` (ADR 0010) makes a live run: Co-Simulation only (an ME-only FMU, or
    ``fmi_type="ModelExchange"``, is refused with the hint to drop ``live`` or use a
    Co-Simulation FMU; ``solver`` doesn't apply), the spec records ``live`` and
    ``idle_limit_s = Timing.live_idle_limit_s``, and the wait ends as soon as the run is
    ``paused`` at its start time (or terminal), with a hint to call ``run_control``. It counts
    toward ``max_workers`` like any run until it ends.

    Order: resolve the FMU; parse it; pick ``fmi_type`` (default as FMPy); resolve the
    experiment; ``check_run`` (step guard, platform, SE); check start value, input and output
    names (close matches); refuse if ``Supervisor.active_run_ids()`` already has
    ``config.max_workers`` runs; hash the FMU; claim the run ID; write ``spec.json`` (with
    ``Versions.current()``), ``queued`` status, the index entry and a ``created`` event; spawn;
    poll ``Supervisor.reconcile`` every ``Timing.poll_interval_s`` until terminal or ``wait_s``
    (default ``Timing.default_wait_s``, capped at ``Timing.max_wait_s``). Default outputs: the
    FMU's outputs, or if it has none, its continuous non-parameter variables (at most
    ``Limits.page_max``).

    Raises:
        UserError: any validation failure, with the fix.
    """
    if fmu is None or system is not None or paramset is not None:
        return start_system_run(
            config,
            supervisor,
            system,
            fmu=fmu,
            paramset=paramset,
            start_time=start_time,
            stop_time=stop_time,
            output_interval=output_interval,
            relative_tolerance=relative_tolerance,
            start_values=start_values,
            inputs=inputs,
            outputs=outputs,
            fmi_type=fmi_type,
            solver=solver,
            timeout_s=timeout_s,
            wait_s=wait_s,
            force=force,
            live=live,
        )
    path = resolve_fmu(config, fmu)
    md = read_description(path)
    kind: FmiType = fmi_type or default_fmi_type(md)
    if live:
        _require_cosimulation(path, md, kind)
    timeout = _timeout(config.timing, timeout_s)
    wait = wait_seconds(config.timing, wait_s)
    experiment = resolve_experiment(
        md,
        kind,
        start_time=start_time,
        stop_time=stop_time,
        output_interval=output_interval,
        relative_tolerance=relative_tolerance,
    )
    steps = check_run(path, md, kind, experiment, config.limits, force)
    plan = _Plan(
        fmu=_fmu_ref(path, md),
        fmi_type=kind,
        solver=solver or "CVode",
        experiment=experiment,
        start_values=_checked_start_values(md, start_values),
        inputs=_checked_inputs(md, inputs, experiment),
        outputs=_checked_outputs(md, outputs, config.limits),
        timeout_s=timeout,
        estimated_steps=steps,
        force=force,
        live=live,
        idle_limit_s=config.timing.live_idle_limit_s if live else None,
    )
    _validated(plan)
    run_id, run_path = _create_run(
        config, supervisor, lambda rid, path: _write_new_run(config, path, plan.spec(rid))
    )
    _spawn(config, supervisor, run_id, run_path)
    return _view(config, _wait_for(config, supervisor, run_id, wait, until_paused=live))


def _require_cosimulation(path: Path, md: ModelDescription, kind: FmiType) -> None:
    """Live control runs the FMU's own stepping, so it needs Co-Simulation (ADR 0010).

    Raises:
        UserError: ``kind`` is Model Exchange; the text says to run it as a batch run (or, for
            an FMU that offers both, to ask for Co-Simulation).
    """
    if kind == "CoSimulation":
        return
    if md.coSimulation is None:
        raise UserError(
            f"{path.name} is a Model Exchange FMU, and live control needs Co-Simulation (a live "
            "run steps the FMU's own solver). Run it as a batch run: drop live=true."
        )
    raise UserError(
        "Live control needs Co-Simulation, so fmi_type='ModelExchange' can't be live. Drop "
        f"fmi_type (or pass 'CoSimulation': {path.name} offers it), or run it as a batch run "
        "by dropping live=true."
    )


def start_system_run(
    config: Config,
    supervisor: Supervisor,
    system: str | None,
    *,
    fmu: str | None = None,
    paramset: str | None = None,
    start_time: float | None = None,
    stop_time: float | None = None,
    output_interval: float | None = None,
    relative_tolerance: float | None = None,
    start_values: Mapping[str, StartValue] | None = None,
    inputs: Mapping[str, InputSignal] | None = None,
    outputs: Sequence[str] | None = None,
    fmi_type: Literal["CoSimulation", "ModelExchange"] | None = None,
    solver: Literal["CVode", "Euler"] | None = None,
    timeout_s: float | None = None,
    wait_s: float | None = None,
    force: bool = False,
    live: bool = False,
) -> RunView:
    """Validate, create and spawn a system run (``SystemSpec``, ADR 0009), then wait.

    ``live=True`` (ADR 0010): as in :func:`start_run`, through ``system.plan_run`` recording
    ``live`` and ``idle_limit_s``; every component is Co-Simulation already. The wait ends when
    the run is paused at its start time.

    Refusals first, each naming the fix: neither or both of ``fmu`` and ``system``;
    ``paramset`` with ``fmu`` (parameter sets apply to systems; pass ``start_values``);
    ``inputs``, ``fmi_type``, ``solver`` or ``relative_tolerance`` with ``system`` (systems
    have no input signals, run Co-Simulation only, and each FMU keeps its own tolerance).
    Then: ``oms_pin.pin_problem(oms_pin.installed(), oms_pin.pinned())`` (refuse early,
    printing both digests); ``system.load_system``; ``ssp.read_paramset`` if given;
    ``system.plan_run`` with ``output_interval`` as the communication step; ``max_workers``
    and the ID claim as in :func:`start_run` (``_create_run``); ``_write_new_run`` with
    ``plan.spec(run_id)``; spawn; wait (``_wait_for``); return :func:`_view`.

    Raises:
        UserError: any refusal or validation failure, with the fix.
    """
    system_file = _system_argument(system, fmu, paramset)
    _refuse_single_fmu_arguments(inputs, fmi_type, solver, relative_tolerance)
    timeout = _timeout(config.timing, timeout_s)
    wait = wait_seconds(config.timing, wait_s)
    installed = _verified_omsimulator()
    loaded = systems.load_system(config, system_file)
    plan = systems.plan_run(
        config,
        loaded,
        paramset=None if paramset is None else ssp.read_paramset(config, paramset),
        start_values=start_values,
        outputs=outputs,
        start_time=start_time,
        stop_time=stop_time,
        step_size=output_interval,
        timeout_s=timeout,
        force=force,
        omsimulator=installed,
        live=live,
    )
    _validated(plan)
    run_id, run_path = _create_run(
        config, supervisor, lambda rid, path: _write_new_run(config, path, plan.spec(rid))
    )
    _spawn(config, supervisor, run_id, run_path)
    return _view(config, _wait_for(config, supervisor, run_id, wait, until_paused=live))


def _system_argument(system: str | None, fmu: str | None, paramset: str | None) -> str:
    """The system file of a system run: ``run_start`` takes exactly one of ``fmu`` and
    ``system``, and ``paramset`` needs ``system``."""
    if system is None:
        if paramset is not None:
            raise UserError(
                "paramset applies to system runs only: pass system=<system file> with it. For "
                "one FMU, pass start_values={...} instead."
            )
        raise UserError(
            "Pass exactly one of fmu (a single FMU, as listed by fmu_list) or system (a system "
            "file; check it with system_check first)."
        )
    if fmu is not None:
        raise UserError(
            "Pass either fmu (one FMU) or system (a system file), not both. To simulate an FMU "
            "together with others, make it a component of the system file."
        )
    return system


def _refuse_single_fmu_arguments(
    inputs: Mapping[str, InputSignal] | None,
    fmi_type: str | None,
    solver: str | None,
    relative_tolerance: float | None,
) -> None:
    """Arguments that only make sense for one FMU are errors for a system, not ignored."""
    if inputs:
        raise UserError(
            "Systems have no input signals: every input is driven by a connection, or set to "
            "a constant with start_values (for example {'PI.setpoint': 1}). Remove inputs, or "
            "connect a component that produces the signal."
        )
    if fmi_type is not None:
        raise UserError("Systems run Co-Simulation FMUs only; remove fmi_type.")
    if solver is not None:
        raise UserError(
            "Systems run on OMSimulator's fixed-step master, not a solver; remove solver. The "
            "step is output_interval (or experiment.step_size in the system file)."
        )
    if relative_tolerance is not None:
        raise UserError("Each FMU of a system keeps its own tolerance; remove relative_tolerance.")


def _verified_omsimulator() -> OmsVersion:
    """The installed OMSimulator identity, if it matches ``config.OMSIMULATOR_PIN``.

    Raises:
        UserError: not installed or a different build; the text prints both digests.
    """
    found = oms_pin.installed()
    problem = oms_pin.pin_problem(found, oms_pin.pinned())
    if problem is not None or found is None:
        raise UserError(problem or "OMSimulator is not installed. Run `uv sync --locked`.")
    return found


def _timeout(timing: Timing, timeout_s: float | None) -> float:
    """The run's wall-time limit: the default, or the caller's within ``Timing.max_timeout_s``."""
    if timeout_s is None:
        return timing.default_timeout_s
    if not math.isfinite(timeout_s) or not 0 < timeout_s <= timing.max_timeout_s:
        raise UserError(
            f"timeout_s must be above 0 and at most {timing.max_timeout_s:g} seconds "
            f"(got {timeout_s:g}). Pass a value in that range."
        )
    return timeout_s


def wait_seconds(timing: Timing, wait_s: float | None) -> float:
    """How long a tool waits (``start_run``; ``control.py`` passes its own default): the default
    ``Timing.default_wait_s``, or the caller's capped at ``max_wait_s``."""
    if wait_s is None:
        return timing.default_wait_s
    if not math.isfinite(wait_s) or wait_s < 0:
        raise UserError(f"wait_s must be 0 or more seconds (got {wait_s:g}).")
    return min(wait_s, timing.max_wait_s)


def _fmu_ref(path: Path, md: ModelDescription) -> FmuRef:
    """The pinned FMU for the spec (hash, size, version, model name)."""
    try:
        return FmuRef(
            path=str(path),
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            fmi_version=md.fmiVersion,
            model_name=md.modelName,
        )
    except ValidationError as exc:
        raise UserError(
            f"{path.name} declares FMI version {md.fmiVersion!r}; Agentic FMI runs FMI 2.0 and "
            "3.0 FMUs. Export the model as one of those."
        ) from exc


def _checked_start_values(
    md: ModelDescription, start_values: Mapping[str, StartValue] | None
) -> dict[str, StartValue]:
    """The start values, if every name exists and every number is finite."""
    values = dict(start_values or {})
    require_variables(md, values, purpose="start_values")
    for name, value in values.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise UserError(
                f"start_values[{name!r}] is {value}; start values must be finite numbers. "
                "Pass a finite value."
            )
    return values


def _checked_inputs(
    md: ModelDescription, inputs: Mapping[str, InputSignal] | None, experiment: Experiment
) -> dict[str, InputSignal]:
    """The input signals, if each names an input of the FMU and can drive its type."""
    signals = dict(inputs or {})
    if not signals:
        return signals
    variables = list(md.modelVariables)
    input_names = [v.name for v in variables if v.causality == "input"]
    check_inputs(signals, input_names, non_input_variables=[v.name for v in variables])
    to_input_array(signals, variable_types(md), experiment.start_time, experiment.stop_time)
    return signals


def _checked_outputs(
    md: ModelDescription, outputs: Sequence[str] | None, limits: Limits
) -> list[str]:
    """The variables to record: the caller's (checked against the FMU) or the defaults."""
    if not outputs:
        defaults = _default_outputs(md, limits)
        if not defaults:
            raise UserError(
                f"{md.modelName} declares no outputs and no continuous variables to record. "
                "Pass outputs=[...] naming variables from fmu_variables."
            )
        return defaults
    names = [name for name in dict.fromkeys(outputs) if name != TIME_COLUMN]
    require_variables(md, names, purpose="outputs")
    types = variable_types(md)
    for name in names:
        if types[name] not in RECORDABLE_TYPES:
            raise UserError(
                f"Output {name!r} is a {types[name]} variable; only numeric, Boolean and "
                "enumeration variables can be recorded. Choose other outputs "
                "(fmu_variables shows each type)."
            )
    if not names:
        raise UserError("outputs names only 'time', which is always recorded. Name a variable.")
    return names


def _default_outputs(md: ModelDescription, limits: Limits) -> list[str]:
    """The FMU's recordable outputs, else its continuous non-parameter variables."""
    variables = [v for v in md.modelVariables if v.type in RECORDABLE_TYPES]
    declared = [v.name for v in variables if v.causality == "output"]
    if declared:
        return declared
    states = [
        v.name
        for v in variables
        if v.variability == "continuous" and v.causality not in NOT_STATE_CAUSALITIES
    ]
    return states[: limits.page_max]


def _validated(plan: _Plan | systems.SystemPlan) -> None:
    """Build the spec once with a placeholder ID, so a bad request fails before an ID is claimed."""
    try:
        plan.spec("r1")
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        raise UserError(
            f"The run request is invalid ({where}: {first['msg']}). Check start_values, inputs "
            "and the experiment."
        ) from exc


def _create_run(
    config: Config,
    supervisor: Supervisor,
    write: Callable[[str, Path], None],
    admit: Callable[[], None] | None = None,
) -> tuple[str, Path]:
    """Check capacity, claim the next run ID and ``write(run_id, run_path)`` the first
    documents of a run or build job, atomically with respect to other threads of this server.
    ``admit`` is a further check that runs under the same lock, before the ID is claimed; it
    raises :class:`UserError` to refuse."""
    with supervisor.admission:
        active = supervisor.active_run_ids()
        if len(active) >= config.max_workers:
            raise UserError(
                f"{len(active)} runs or builds are already queued or running "
                f"({', '.join(active)}); the limit is {config.max_workers}. Wait for one to "
                "finish (run_status) or stop one (run_stop), then start again."
            )
        if admit is not None:
            admit()
        run_id, run_path = runstore.claim_run_id(config.store_dir)
        try:
            write(run_id, run_path)
        except OSError as exc:
            raise UserError(
                f"Can't write the run store at {config.store_dir}: {exc.strerror or exc}. "
                "Check free disk space and permissions, then start again."
            ) from exc
    return run_id, run_path


def _write_new_run(config: Config, run_path: Path, spec: runstore.JobSpec) -> None:
    """``spec.json``, the ``queued`` status, the index entry and the ``created`` event. A build
    job's status has ``t_start = t_stop = 0``."""
    if isinstance(spec, BuildSpec):
        window = (0.0, 0.0)
        created = f"build {spec.name} from {spec.source.path} ({spec.backend})"
    elif isinstance(spec, SystemSpec):
        window = (spec.experiment.start_time, spec.experiment.stop_time)
        created = (
            f"system {spec.name} ({len(spec.components)} components, "
            f"h={spec.master.step_size:g}) t={window[0]:g}..{window[1]:g}"
        )
    else:
        experiment = spec.experiment
        window = (experiment.start_time, experiment.stop_time)
        created = f"{spec.fmu.model_name} {spec.fmi_type} t={window[0]:g}..{window[1]:g}"
    status = RunStatus(
        run_id=spec.run_id,
        state=RunState.QUEUED,
        t_start=window[0],
        t_stop=window[1],
        updated_at=spec.created_at,
    )
    runstore.write_spec(run_path, spec)
    runstore.write_status(run_path, status)
    runstore.upsert_index(config.store_dir, runstore.index_entry(spec, status))
    _append_event(config, spec.run_id, EventType.CREATED, created)


def _append_event(config: Config, run_id: str, kind: EventType, message: str) -> None:
    """Append a server event; a failed append never fails the call (the status is written)."""
    event = Event(at=utc_now(), run_id=run_id, type=kind, source="server", message=message)
    with suppress(OSError, ValueError):
        runstore.append_event(config.store_dir, event)


def _spawn(config: Config, supervisor: Supervisor, run_id: str, run_path: Path) -> None:
    """Start the worker; if the process can't be started, record ``failed{internal_error}``."""
    try:
        supervisor.spawn(run_path)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        message = (
            f"The worker process could not be started ({reason}). Check that the server can "
            f"run {sys.executable} and write the run directory, then start a new run."
        )
        _fail_unspawned(config, run_id, run_path, message)


def _fail_unspawned(config: Config, run_id: str, run_path: Path, message: str) -> None:
    """Write ``failed{internal_error}`` for a run that never got a worker, under the store lock
    and only if it is still not terminal."""
    now = utc_now()
    error = RunError(code=ErrorCode.INTERNAL_ERROR, message=message)
    with locked(config.store_dir / runstore.LOCK_FILE):
        current = runstore.read_status(run_path)
        if current.state in TERMINAL_STATES:
            return
        failed = current.model_copy(
            update={"state": RunState.FAILED, "error": error, "finished_at": now, "updated_at": now}
        )
        runstore.write_status(run_path, failed)
    _append_event(config, run_id, EventType.FAILED, f"{ErrorCode.INTERNAL_ERROR.value}: {message}")


def _wait_for(
    config: Config,
    supervisor: Supervisor,
    run_id: str,
    wait_s: float,
    *,
    until_paused: bool = False,
) -> RunStatus:
    """Reconcile every poll interval until the run is terminal or ``wait_s`` has passed; a live
    run (``until_paused``) is also done once it is ``paused`` (at its start time)."""
    deadline = time.monotonic() + wait_s
    while True:
        status = supervisor.reconcile(run_id)
        done = status.state in TERMINAL_STATES or (until_paused and status.state is RunState.PAUSED)
        if done or time.monotonic() >= deadline:
            return status
        time.sleep(config.timing.poll_interval_s)


# --- fmu_build ------------------------------------------------------------------------------


def start_build(
    config: Config,
    supervisor: Supervisor,
    source: str,
    *,
    name: str | None = None,
    overwrite: bool = False,
    wait_s: float | None = None,
) -> RunView:
    """Validate a build, create its job and wait up to ``wait_s`` for it (ADR 0008).

    Order: ``build.check_request`` (no subprocess); then, under the supervisor's ``admission``
    lock, the same ``max_workers`` limit as runs and ``build.check_name`` again with
    ``building`` = the names of this store's build jobs that are still queued or running (so
    two threads of a server can't both start one name), claim the ID and write
    ``source/<file name>`` (the bytes ``check_request`` hashed), ``spec.json``
    (``build.build_spec``), the ``queued`` status (``t_start = t_stop = 0``), the index entry
    and a ``created`` event; spawn the worker; poll until terminal or ``wait_s`` (default
    ``Timing.build_default_wait_s``, capped at ``Timing.max_wait_s``). Returns the job's view:
    finished (with ``build``), still queued or running (hint: poll ``run_status``, cancel with
    ``run_stop``), or stopped. The view of a finished build fits
    ``Limits.build_result_max_bytes``.

    Raises:
        UserError: a refused request, with the fix; or the build failed within ``wait_s``:
            ``build_failed`` raises its diagnostics text as is, any other code its message plus
            "run_status('<id>') has the details".
    """
    timing = config.timing
    wait = wait_seconds(timing, timing.build_default_wait_s if wait_s is None else wait_s)
    request = build.check_request(config, source, name=name, overwrite=overwrite)

    def not_building() -> None:
        building = _building_names(config, supervisor)
        build.check_name(config, request.name, overwrite=request.overwrite, building=building)

    def write(run_id: str, run_path: Path) -> None:
        runstore.write_build_source(run_path, request.source.name, request.data)
        _write_new_run(config, run_path, build.build_spec(config, request, run_id))

    run_id, run_path = _create_run(config, supervisor, write, admit=not_building)
    _spawn(config, supervisor, run_id, run_path)
    status = _wait_for(config, supervisor, run_id, wait)
    if status.state is RunState.FAILED:
        raise _build_failure(status)
    return _view(config, status)


def _building_names(config: Config, supervisor: Supervisor) -> list[str]:
    """The names of this store's build jobs that are still queued or running."""
    fresh = {status.run_id: status.state for status in supervisor.reconcile_all()}
    return [
        entry.fmu_name
        for entry in runstore.read_index(config.store_dir).runs
        if entry.kind == "build" and fresh.get(entry.run_id, entry.state) not in TERMINAL_STATES
    ]


def _build_failure(status: RunStatus) -> UserError:
    """The error ``fmu_build`` raises for a build that failed while it waited."""
    error = status.error
    if error is None:
        return UserError(f"Build {status.run_id} failed. run_status('{status.run_id}') has more.")
    if error.code is ErrorCode.BUILD_FAILED:
        return UserError(error.message)
    return UserError(
        f"Build {status.run_id} failed ({error.code.value}): {error.message} "
        f"run_status('{status.run_id}') has the details."
    )


# --- run_status, run_stop, run_list ------------------------------------------------------------


def get_run(config: Config, supervisor: Supervisor, run_id: str) -> RunView:
    """Reconcile and describe one run (summary when finished or stopped, error when failed)."""
    return _view(config, supervisor.reconcile(run_id))


def view_of(
    config: Config,
    status: RunStatus,
    hint: str | None = None,
    values: list[LiveColumn] | None = None,
) -> RunView:
    """The :class:`RunView` of ``status`` with an optional hint and ``run_status(values)``
    columns, for ``control.py``.

    Live runs' views carry ``speed`` from ``status.speed``. Without ``hint``, a ``paused`` run
    gets "paused at t=…" with the ``run_control`` actions that apply, a running live run says
    how to control it, and an ended run's ``status.reason`` (the idle limit, "ended early at t")
    leads its hint."""
    return _view(config, status, hint, values)


def stop_run(config: Config, supervisor: Supervisor, run_id: str) -> RunView:
    """Stop a run through :meth:`Supervisor.stop` and describe the result.

    The supervisor blocks up to ``stop_grace_s + kill_confirm_s``. If the worker could not be
    confirmed gone, the state is still non-terminal and the hint says to call again.
    """
    status = supervisor.stop(run_id)
    if status.state in TERMINAL_STATES:
        return _view(config, status)
    hint = (
        f"The worker hasn't been confirmed stopped yet; call run_stop('{run_id}') again, then "
        f"check run_status('{run_id}')."
    )
    return _view(config, status, hint)


def list_runs(
    config: Config,
    supervisor: Supervisor,
    *,
    state: RunState | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> RunPage:
    """Page through runs, newest first, reconciling the non-terminal ones. A page over the JSON
    budget is cut and continues at ``next_offset``."""
    fresh = {status.run_id: status.state for status in supervisor.reconcile_all()}
    entries = [
        (entry, fresh.get(entry.run_id, entry.state))
        for entry in reversed(runstore.read_index(config.store_dir).runs)
    ]
    if state is not None:
        entries = [(entry, current) for entry, current in entries if current == state]
    start, size = page_window(config, len(entries), offset, limit, "runs")
    shown = entries[start : start + size]
    end = start + len(shown)
    page = RunPage(
        total=len(entries),
        offset=start,
        limit=size,
        items=[
            RunListItem(
                run_id=entry.run_id,
                kind=entry.kind,
                state=current,
                fmu_name=entry.fmu_name,
                created_at=entry.created_at,
            )
            for entry, current in shown
        ],
        next_offset=end if end < len(entries) else None,
    )
    return budget.fit_page(page, config.limits, limit)


def _view(
    config: Config,
    status: RunStatus,
    hint: str | None = None,
    values: list[LiveColumn] | None = None,
) -> RunView:
    """The :class:`RunView` of ``status``: spec facts, summary or error, and the next step;
    for a build job, :func:`_build_view`. ``values`` are a live run's ``run_status(values)``
    columns."""
    run_path = runstore.run_dir(config.store_dir, status.run_id)
    spec = runstore.read_job_spec(run_path)
    if isinstance(spec, BuildSpec):
        return _build_view(config, status, spec, hint)
    if isinstance(spec, SystemSpec):
        return _system_view(config, status, spec, hint, values)
    summary = runstore.read_summary(run_path) if status.state in RESULT_STATES else None
    view = RunView(
        run_id=status.run_id,
        state=status.state,
        fmu=relative_path(config, Path(spec.fmu.path)),
        fmi_type=spec.fmi_type,
        hint=hint or _hint(status, summary, spec.live),
        values=values,
        **_progress_fields(config, status, summary),
    )
    return budget.fit_to_budget(view, config.limits.json_budget_bytes, _smaller_view)


def _system_view(
    config: Config,
    status: RunStatus,
    spec: SystemSpec,
    hint: str | None = None,
    values: list[LiveColumn] | None = None,
) -> RunView:
    """A system run's view: as :func:`_view` for a run, with ``kind`` "system", ``fmu`` the
    system file's workspace-relative path and ``fmi_type`` "CoSimulation"; a finished run's
    hint suggests ``run_plot``/``run_compare`` and, for tuning, ``start_values`` keyed
    ``<component>.<variable>``."""
    run_path = runstore.run_dir(config.store_dir, status.run_id)
    summary = runstore.read_summary(run_path) if status.state in RESULT_STATES else None
    view = RunView(
        run_id=status.run_id,
        kind="system",
        state=status.state,
        fmu=relative_path(config, Path(spec.system.path)),
        fmi_type="CoSimulation",
        hint=hint or _system_hint(status, summary, spec.live),
        values=values,
        **_progress_fields(config, status, summary),
    )
    return budget.fit_to_budget(view, config.limits.json_budget_bytes, _smaller_view)


def _progress_fields(
    config: Config, status: RunStatus, summary: RunSummary | None
) -> dict[str, Any]:
    """The ``RunView`` fields every simulation run has: time, progress, error and summary."""
    digits = config.limits.significant_digits
    return {
        "t": None if status.t is None else round_sig(status.t, digits),
        "t_stop": _sig(status.t_stop, digits),
        "progress": _sig(status.progress, 3),
        "speed": status.speed,
        "error": status.error,
        "summary": None if summary is None else _compact_summary(summary, config.limits),
    }


def _system_hint(status: RunStatus, summary: RunSummary | None, live: bool = False) -> str:
    """:func:`_hint`, and for a finished system run how to tune it."""
    hint = _hint(status, summary, live)
    if status.state is RunState.FINISHED:
        tune = "run_start(system=..., start_values={'<component>.<parameter>': v})"
        return f"{hint} Tune with {tune}."
    if status.error is not None and status.error.code is ErrorCode.WORKER_LOST:
        return (
            f"{hint} A component that raises inside OMSimulator aborts the worker; run each "
            "component alone with run_start(fmu=...) to find it."
        )
    return hint


def _compact_summary(summary: RunSummary, limits: Limits) -> CompactSummary:
    cap = limits.inspect_max_items
    return CompactSummary(
        t_end=_sig(summary.t_end, limits.significant_digits),
        n_points=summary.n_points,
        wall_time_s=summary.wall_time_s,
        variables=summary.variables[:cap],
        truncated=len(summary.variables) > cap,
    )


def _hint(status: RunStatus, summary: RunSummary | None, live: bool = False) -> str:
    """The obvious next call for a run in this state; an ended run's ``status.reason`` (the idle
    limit, "ended early at t") comes first."""
    hint = _state_hint(status, summary, live)
    if status.reason is not None and status.state in TERMINAL_STATES:
        return f"{status.reason[:1].upper()}{status.reason[1:].rstrip('.')}. {hint}"
    return hint


def _state_hint(status: RunStatus, summary: RunSummary | None, live: bool) -> str:
    rid = status.run_id
    if status.state is RunState.PAUSED:
        at = "" if status.t is None else f" at t={status.t:g}"
        return (
            f"Paused{at}. Continue with run_control('{rid}', action='resume'), or use 'step', "
            f"'set' or 'stop'; read outputs with run_status('{rid}', values=[...])."
        )
    if status.state in (RunState.QUEUED, RunState.RUNNING):
        poll = f"Poll with run_status('{rid}'); stop it with run_stop('{rid}')."
        if not live:
            return poll
        return f"{poll} Steer it with run_control('{rid}', action='pause'), 'set' or 'stop'."
    if status.state is RunState.FINISHED:
        return f"Use run_plot or run_results on '{rid}', or run_compare against another run."
    if status.state is RunState.STOPPED:
        if summary is None:
            return "Stopped without a result. Start a new run with run_start."
        return f"Stopped; the partial result is kept. Use run_plot or run_results on '{rid}'."
    return "Fix the cause named in error (log_tail holds the FMU's own messages), then run_start."


def _build_view(
    config: Config, status: RunStatus, spec: BuildSpec, hint: str | None = None
) -> RunView:
    """A build job's view: its output FMU, the ``build`` facts once finished (within
    ``Limits.build_result_max_bytes``), or its error, whose message is the diagnostics."""
    limits = config.limits
    record = None
    if status.state is RunState.FINISHED:
        record = runstore.read_build_record(runstore.run_dir(config.store_dir, status.run_id))
    view = RunView(
        run_id=status.run_id,
        kind="build",
        state=status.state,
        fmu=spec.fmu_path,
        progress=_sig(status.progress, 3),
        error=status.error,
        build=None if record is None else _build_facts(record, limits),
        hint=hint or _build_hint(status, spec),
    )
    cap = limits.build_result_max_bytes if record is not None else limits.json_budget_bytes
    return budget.fit_to_budget(view, cap, _smaller_build_view)


def _build_facts(record: BuildRecord, limits: Limits) -> dict[str, Any]:
    """The ``build`` object of a finished build's view."""
    facts: dict[str, Any] = {
        "sha256": record.fmu.sha256,
        "model_identifier": record.fmu.model_identifier,
        "interfaces": record.fmu.interfaces,
        "counts": record.summary.counts,
        "backend": record.backend,
        "duration_s": record.duration_s,
        "warnings": record.warnings[: limits.build_max_warnings],
    }
    if record.replaced_sha256 is not None:
        facts["replaced_sha256"] = record.replaced_sha256
    return facts


def _build_hint(status: RunStatus, spec: BuildSpec) -> str:
    """The obvious next call for a build job in this state."""
    rid = status.run_id
    if status.state in (RunState.QUEUED, RunState.RUNNING):
        return f"Building; poll with run_status('{rid}'), cancel with run_stop('{rid}')."
    if status.state is RunState.FINISHED:
        return f"Inspect it with fmu_inspect or simulate it with run_start(fmu='{spec.fmu_path}')."
    if status.state is RunState.STOPPED:
        return "Cancelled; nothing was written to generated/. Call fmu_build again to rebuild."
    if status.error is not None and status.error.code is ErrorCode.BUILD_FAILED:
        return f"Fix {spec.source.path} at the lines in error.message, then call fmu_build again."
    return "Fix the cause named in error, then call fmu_build again."


def _smaller_build_view(view: RunView) -> RunView | None:
    """Drop the build warnings, then the variable counts; ``None`` when nothing is left."""
    facts = view.build or {}
    for key in ("warnings", "counts"):
        if key in facts:
            return view.model_copy(update={"build": {k: v for k, v in facts.items() if k != key}})
    return None


def _smaller_view(view: RunView) -> RunView | None:
    """Halve the summary's variables, else the log tail; ``None`` when nothing is left to cut."""
    summary = view.summary
    if summary is not None and len(summary.variables) > 1:
        cut = summary.model_copy(
            update={
                "variables": summary.variables[: len(summary.variables) // 2],
                "truncated": True,
            }
        )
        return view.model_copy(update={"summary": cut})
    error = view.error
    if error is not None and error.log_tail:
        tail = error.log_tail[len(error.log_tail) // 2 :]
        return view.model_copy(update={"error": error.model_copy(update={"log_tail": tail})})
    return None


# --- run_results ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ready:
    """A run that has a result: its spec (a single-FMU run's or a system run's), recorded
    columns, units and directory."""

    spec: SimulationSpec
    columns: list[str]
    units: dict[str, str | None]
    path: Path

    @property
    def run_id(self) -> str:
        return self.spec.run_id

    def read(self, names: Sequence[str]) -> np.ndarray:
        """The result array with ``time`` plus the requested columns this run has."""
        return runstore.read_result(self.path, [n for n in names if n in self.columns])


def _load_ready(config: Config, run_id: str) -> _Ready:
    """Open a finished or stopped run.

    Raises:
        UserError: unknown run, a build job, not finished or stopped yet, failed, or stopped
            without a result.
    """
    run_path = runstore.run_dir(config.store_dir, run_id)
    spec = runstore.read_job_spec(run_path)
    if isinstance(spec, BuildSpec):
        raise UserError(
            f"{run_id} is an fmu_build job, not a simulation, so it has no time series. "
            f"Simulate its FMU with run_start(fmu='{spec.fmu_path}') and use that run."
        )
    status = runstore.read_status(run_path)
    if status.state not in RESULT_STATES:
        raise _not_ready(status)
    summary = runstore.read_summary(run_path)
    if summary is None:
        raise UserError(
            f"Run {run_id} was {status.state.value} before it wrote a result. Start a new run "
            "with run_start."
        )
    return _Ready(
        spec=spec,
        columns=summary.columns,
        units={v.name: v.unit for v in summary.variables},
        path=run_path,
    )


def _not_ready(status: RunStatus) -> UserError:
    rid = status.run_id
    if status.state is RunState.FAILED:
        code = status.error.code.value if status.error else "unknown error"
        return UserError(
            f"Run {rid} failed ({code}), so it has no result. Read run_status('{rid}') for the "
            "error, fix it, then start a new run."
        )
    return UserError(
        f"Run {rid} is {status.state.value}; only finished or stopped runs have results. Poll "
        f"run_status('{rid}') until it is finished, or call run_stop('{rid}')."
    )


def get_results(
    config: Config,
    run_id: str,
    *,
    variables: Sequence[str] | None = None,
    max_points: int | None = None,
    start_time: float | None = None,
    stop_time: float | None = None,
) -> RunResults:
    """Downsampled series of up to ``Limits.results_max_variables`` variables.

    ``max_points`` defaults to ``Limits.results_default_points`` and is capped at
    ``Limits.results_max_points``. The JSON budget is ``Limits.json_budget_bytes``, or
    ``json_hard_cap_bytes`` if the caller raised ``max_points`` above the default; if the
    serialized result is larger, halve the points of every series until it fits and say so in
    ``note``. Default variables: the first ``DEFAULT_RESULT_VARIABLES`` recorded ones.

    Raises:
        UserError: unknown run or variable (close matches), run not finished or stopped yet,
            too many variables.
    """
    limits = config.limits
    run = _load_ready(config, run_id)
    names, notes = _result_variables(run, variables, limits)
    points = _requested_points(max_points, limits)
    table = _window(runstore.read_result(run.path, names), start_time, stop_time)
    limit = budget.json_budget(limits, enlarged=points > limits.results_default_points)
    return _fit_results(run, table, names, (points, limit), notes, limits.significant_digits)


def _result_variables(
    run: _Ready, variables: Sequence[str] | None, limits: Limits
) -> tuple[list[str], list[str]]:
    """The variables to return and the notes about the choice."""
    if variables:
        names = list(dict.fromkeys(variables))
        if len(names) > limits.results_max_variables:
            raise UserError(
                f"run_results returns at most {limits.results_max_variables} variables per call "
                f"(got {len(names)}). Split them over several calls, or use run_compare."
            )
        return names, []
    names = run.columns[:DEFAULT_RESULT_VARIABLES]
    if len(run.columns) <= len(names):
        return names, []
    more = f"{len(run.columns)} variables were recorded; showing the first {len(names)}."
    return names, [f"{more} Pass variables=[...] for others: {', '.join(run.columns[:12])}..."]


def _requested_points(max_points: int | None, limits: Limits) -> int:
    """``max_points`` or the default, capped at ``Limits.results_max_points``."""
    points = limits.results_default_points if max_points is None else max_points
    if points < MIN_POINTS:
        raise UserError(f"max_points must be at least {MIN_POINTS} (got {points}).")
    return min(points, limits.results_max_points)


def _window(table: np.ndarray, start_time: float | None, stop_time: float | None) -> np.ndarray:
    """The rows of ``table`` between the optional start and stop times (inclusive)."""
    times = table[TIME_COLUMN]
    if times.size == 0:
        raise UserError("The run's result has no samples.")
    if start_time is not None and stop_time is not None and stop_time <= start_time:
        raise UserError(
            f"stop_time ({stop_time:g}) must be greater than start_time ({start_time:g})."
        )
    keep = np.ones(times.size, dtype=np.bool_)
    if start_time is not None:
        keep &= times >= start_time
    if stop_time is not None:
        keep &= times <= stop_time
    if not keep.any():
        raise UserError(
            f"No samples between the given times. The run covers t={times[0]:g} to "
            f"t={times[-1]:g}; widen start_time and stop_time."
        )
    return table[keep]


def _fit_results(
    run: _Ready,
    table: np.ndarray,
    names: Sequence[str],
    size: tuple[int, int],
    notes: Sequence[str],
    digits: int,
) -> RunResults:
    """Series with ``requested`` points each, halved until the JSON fits the budget; ``size`` is
    ``(requested, budget_bytes)``."""
    requested, limit = size
    points = requested
    while True:
        results = _results_model(run, table, names, points, notes, digits)
        if points <= MIN_POINTS or budget.json_size(results) <= limit:
            break
        points = max(MIN_POINTS, points // 2)
    if points == requested:
        return results
    reduced = (
        f"Reduced from {requested} to {points} points per variable to fit the output budget. "
        "Ask for fewer variables or a time window (start_time, stop_time) for more detail, or "
        "use run_plot to see the shape."
    )
    return results.model_copy(update={"note": " ".join([*notes, reduced])})


def _results_model(
    run: _Ready,
    table: np.ndarray,
    names: Sequence[str],
    points: int,
    notes: Sequence[str],
    digits: int,
) -> RunResults:
    times = table[TIME_COLUMN]
    return RunResults(
        run_id=run.run_id,
        t_start=_sig(times[0], digits),
        t_end=_sig(times[-1], digits),
        n_points_total=int(times.size),
        series=[
            _series(table, name, run.units.get(name), points, digits)
            for name in names
            if name != TIME_COLUMN
        ],
        note=" ".join(notes) or None,
    )


def _series(table: np.ndarray, name: str, unit: str | None, points: int, digits: int) -> Series:
    times, values = table[TIME_COLUMN], table[name]
    keep = lttb_indices(times, values, points)
    return Series(
        name=name,
        unit=unit,
        t=[_sig(t, digits) for t in times[keep]],
        y=[round_sig(y, digits) for y in values[keep]],
    )


def _sig(value: float, digits: int) -> float:
    """``value`` rounded to ``digits`` significant digits (times are always finite)."""
    rounded = round_sig(value, digits)
    return 0.0 if rounded is None else rounded


# --- run_compare, run_plot --------------------------------------------------------------------


def _load_all(config: Config, run_ids: Sequence[str], minimum: int) -> list[_Ready]:
    """Open 1 to ``Limits.plot_max_runs`` distinct finished or stopped runs."""
    limit = config.limits.plot_max_runs
    unique = list(dict.fromkeys(run_ids))
    if len(unique) != len(run_ids):
        raise UserError("A run is listed twice. Pass each run once.")
    if not minimum <= len(unique) <= limit:
        raise UserError(f"Pass {minimum} to {limit} runs (got {len(unique)}); run_list has IDs.")
    return [_load_ready(config, run_id) for run_id in unique]


def _choose_variables(
    runs: Sequence[_Ready], variables: Sequence[str] | None, cap: int
) -> list[str]:
    """The variables to compare or plot: the caller's (known to some run) or those in all runs."""
    if not variables:
        shared = [c for c in runs[0].columns if all(c in run.columns for run in runs[1:])]
        if not shared:
            raise UserError(
                "These runs have no recorded variable in common. Pass variables=[...] "
                f"(run 1 recorded: {', '.join(runs[0].columns[:10])})."
            )
        return shared[:cap]
    names = list(dict.fromkeys(variables))
    if len(names) > cap:
        raise UserError(f"At most {cap} variables per call (got {len(names)}). Split the request.")
    known = list(dict.fromkeys(c for run in runs for c in run.columns))
    unknown = [name for name in names if name not in known]
    if unknown:
        close = difflib.get_close_matches(unknown[0], known, n=3)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        recorded = ", ".join(known[:10])
        raise UserError(
            f"Unknown variable {unknown[0]!r} in these runs.{hint} Recorded: {recorded}."
        )
    return names


def compare_runs(
    config: Config, run_ids: Sequence[str], variables: Sequence[str] | None = None
) -> Comparison:
    """Compare 2 to ``Limits.plot_max_runs`` finished or stopped runs; the first is the baseline.

    Default variables: those recorded in every run, at most ``Limits.plot_max_variables``.
    """
    limits = config.limits
    runs = _load_all(config, run_ids, minimum=2)
    names = _choose_variables(runs, variables, limits.plot_max_variables)
    results = {run.run_id: run.read(names) for run in runs}
    units = {name: unit for run in reversed(runs) for name, unit in run.units.items() if unit}
    comparison = compare_analysis.compare_runs(
        [run.spec for run in runs], results, names, units, limits.significant_digits
    )
    comparison = _clip_differences(comparison)
    everything = comparison.spec_differences
    comparison = _show_differences(comparison, everything, MAX_SPEC_DIFFERENCES)
    return budget.fit_to_budget(
        comparison, limits.json_budget_bytes, lambda c: _fewer_differences(c, everything)
    )


def _clip_differences(comparison: Comparison) -> Comparison:
    """Cut long spec-difference values (an input table is one long JSON text)."""
    differences = [
        difference.model_copy(
            update={"values": {run: _clip_value(v) for run, v in difference.values.items()}}
        )
        for difference in comparison.spec_differences
    ]
    return comparison.model_copy(update={"spec_differences": differences})


def _clip_value(value: str | float | int | bool | None) -> str | float | int | bool | None:
    if isinstance(value, str) and len(value) > MAX_DIFFERENCE_CHARS:
        return value[: MAX_DIFFERENCE_CHARS - 3] + "..."
    return value


def _show_differences(
    comparison: Comparison, everything: Sequence[SpecDifference], count: int
) -> Comparison:
    """``comparison`` with its first ``count`` spec differences, and a note if some are hidden."""
    if count >= len(everything):
        return comparison
    notes = [note for note in comparison.notes if not note.startswith(DIFFERENCES_NOTE)]
    notes.append(f"{DIFFERENCES_NOTE} {count} of {len(everything)} spec differences.")
    return comparison.model_copy(
        update={"spec_differences": list(everything[:count]), "notes": notes}
    )


def _fewer_differences(
    comparison: Comparison, everything: Sequence[SpecDifference]
) -> Comparison | None:
    """Half as many spec differences as ``comparison`` shows; ``None`` when only one is left."""
    shown = len(comparison.spec_differences)
    return None if shown <= 1 else _show_differences(comparison, everything, shown // 2)


def plot_runs(
    config: Config,
    run_ids: Sequence[str],
    variables: Sequence[str] | None = None,
    title: str | None = None,
) -> Plot:
    """Render up to ``Limits.plot_max_runs`` runs x ``Limits.plot_max_variables`` variables to one
    PNG of at most ``Limits.plot_max_bytes``, saved atomically as
    ``runs/<first run>/plots/<variables>__<runs>.png`` (sanitized, at most 120 characters).
    Legend labels name the run and the spec values that differ between the runs."""
    limits = config.limits
    runs = _load_all(config, run_ids, minimum=1)
    names = _choose_variables(runs, variables, limits.plot_max_variables)
    labels = _legend_labels([run.spec for run in runs])
    results = {run.run_id: run.read(names) for run in runs}
    panels = [_panel(name, runs, results, labels) for name in names]
    try:
        png = render_png(
            panels,
            title,
            limits.plot_max_bytes,
            max_panels=limits.plot_max_variables,
            max_lines=limits.plot_max_runs,
        )
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    path = _save_plot(runs[0].path, names, [run.run_id for run in runs], png)
    caption = (
        f"{', '.join(names)} for {', '.join(run.run_id for run in runs)}; "
        f"saved to {relative_path(config, path)}"
    )
    return Plot(png=png, path=path, caption=caption)


def _panel(
    name: str, runs: Sequence[_Ready], results: Mapping[str, np.ndarray], labels: Mapping[str, str]
) -> Panel:
    """One subplot: ``name`` in every run that recorded it."""
    lines = [
        Line(
            label=labels[run.run_id],
            t=results[run.run_id][TIME_COLUMN],
            y=results[run.run_id][name],
        )
        for run in runs
        if name in run.columns
    ]
    unit = next((run.units[name] for run in runs if run.units.get(name)), None)
    return Panel(variable=name, unit=unit, lines=lines)


def _legend_labels(specs: Sequence[SimulationSpec]) -> dict[str, str]:
    """``r2 (e=0.8)``, or ``r2 (PI.Kp=12)`` for system runs: each run's ID and the spec values
    that differ between the runs."""
    differences = [
        d
        for d in compare_analysis.diff_specs(specs)
        if d.field not in LABEL_SKIPPED_FIELDS and not d.field.startswith(LABEL_SKIPPED_PREFIXES)
    ][:MAX_LABEL_PARTS]
    labels: dict[str, str] = {}
    for spec in specs:
        parts = [_label_part(d, spec.run_id) for d in differences]
        labels[spec.run_id] = f"{spec.run_id} ({', '.join(parts)})" if parts else spec.run_id
    return labels


def _label_part(difference: SpecDifference, run_id: str) -> str:
    field = difference.field
    name = next(
        (field.removeprefix(prefix) for prefix in LABEL_PREFIXES if field.startswith(prefix)), field
    )
    value = difference.values.get(run_id)
    if value is None:
        text = "unset"
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    if len(text) > MAX_LABEL_VALUE_CHARS:
        text = text[: MAX_LABEL_VALUE_CHARS - 3] + "..."
    return f"{name}={text}"


def _plot_file_name(names: Sequence[str], run_ids: Sequence[str]) -> str:
    """``<variables>__<runs>.png`` with unsafe characters replaced, at most 120 characters; a
    longer name keeps its start and ends in a hash of the whole, so different plots differ."""
    stem = f"{'+'.join(names)}__{'+'.join(run_ids)}"
    safe = _UNSAFE_FILE_CHARS.sub("_", stem)
    room = MAX_PLOT_FILE_CHARS - len(PLOT_SUFFIX)
    if len(safe) > room:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[: room - 9]}_{digest}"
    return safe + PLOT_SUFFIX


def _save_plot(run_path: Path, names: Sequence[str], run_ids: Sequence[str], png: bytes) -> Path:
    """Write the PNG atomically under the first run's ``plots/`` directory."""
    directory = run_path / runstore.PLOTS_DIR
    path = directory / _plot_file_name(names, run_ids)
    try:
        directory.mkdir(exist_ok=True)
        with atomic_open(path) as handle:
            handle.write(png)
    except OSError as exc:
        raise UserError(
            f"Can't save the plot under {directory}: {exc.strerror or exc}. Check free disk "
            "space and permissions."
        ) from exc
    return path
