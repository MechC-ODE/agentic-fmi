"""Run-store protocol v1: the pydantic models for every JSON document in ``.agentic-fmi/``.

These models are the contract between the MCP server, the workers and the pane
([ADR 0003](../../../docs/adr/0003-file-based-run-store-protocol.md),
[docs/protocol.md](../../../docs/protocol.md)). The committed JSON Schemas in ``docs/schemas/``
are generated from them by :func:`export_schemas`, and a drift test keeps the two in sync.

Rules encoded here:
- Every document carries ``"protocol": 1``. A reader that meets another value fails validation
  and reports "unsupported protocol version" instead of guessing.
- Floats must be finite: NaN and Infinity are rejected on validation (``allow_inf_nan=False``).
  Writers map non-finite results to ``null`` before building a model.
- Readers ignore unknown fields, so additive changes keep the version.
- ``runs/<id>/spec.json`` is a :class:`RunSpec` (``kind: "run"``), for a build job a
  :class:`BuildSpec` (``kind: "build"``, ADR 0008), or for a run of a multi-FMU system a
  :class:`SystemSpec` (``kind: "system"``, ADR 0009); readers dispatch on ``kind``. An
  ``events.jsonl`` line is an :class:`Event`, or a :class:`BuildEvent` when ``type`` is
  ``fmu_built``. ``build.json`` is a :class:`BuildRecord`.
- Live runs (ADR 0010, all additive): ``RunSpec.live`` / ``SystemSpec.live`` with
  ``idle_limit_s``, the ``paused`` state, ``RunStatus.heartbeat_mono``, ``speed`` and
  ``reason``, the ``command`` event type, the pane's ``chart`` action (``Command.names``: the
  chart selection) and three documents: a :class:`Command` in
  ``runs/<id>/commands/<id>.json`` (written by the server or the pane), its
  :class:`CommandOutcome` in ``runs/<id>/outcomes/<id>.json`` (worker only) and the
  :class:`LiveSnapshot` ``runs/<id>/live.json`` (the worker's heartbeat thread only).
- Response styles (additive): ``<store>/preferences.json`` is a :class:`Preferences`, written
  only by the MCP server's ``response_style`` tool.

Collaborators: ``store/atomic.py`` and ``store/runstore.py`` read and write these models;
``store/inbox.py`` the command and outcome files; ``signals.py`` converts :data:`InputSignal`
values into FMPy input arrays; ``build.py`` writes the build documents; ``live.py`` (worker)
builds the live documents; ``scripts/pane_fixtures.py`` turns them into the pane's fixtures.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

PROTOCOL_VERSION: Final = 1
"""The run-store protocol version these models read and write."""

RUN_ID_PATTERN: Final = r"^r[1-9]\d*$"
"""Run IDs: ``r`` plus a positive decimal number without leading zeros (``r1``, ``r12``)."""

SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"

MAX_EVENT_LINE_BYTES: Final = 4096
"""An ``events.jsonl`` line, including its newline, is at most this many bytes."""

BUILD_NAME_PATTERN: Final = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
"""``fmu_build`` output names: ``generated/<name>/<name>.fmu`` (ADR 0008)."""

COMPONENT_NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
"""System component names (ADR 0009): an ASCII identifier, so ``<component>.<variable>`` splits
at the first dot and ``<component>.fmu`` is a safe file name."""

VARIABLE_REF_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}\..+$"
"""``<component>.<variable>``: split at the first dot; the variable name may contain dots,
brackets and parentheses (``body.v[1]``, ``der(x)``)."""

COMMAND_ID_PATTERN: Final = r"^\d{13}-(server|pane)-[0-9a-f]{8}$"
"""Inbox command IDs (ADR 0010): 13 digits of Unix milliseconds, the writer, 8 lowercase hex
digits of randomness. ``commands/<id>.json`` is the file; the worker ignores any other name and
applies files in name order."""

MAX_COMMAND_BYTES: Final = 4096
"""A command file is at most this many bytes; a larger one is rejected unread."""

RunId = Annotated[str, StringConstraints(pattern=RUN_ID_PATTERN)]
CommandId = Annotated[str, StringConstraints(pattern=COMMAND_ID_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
BuildName = Annotated[str, StringConstraints(pattern=BUILD_NAME_PATTERN)]
ComponentName = Annotated[str, StringConstraints(pattern=COMPONENT_NAME_PATTERN)]
VariableRef = Annotated[str, StringConstraints(pattern=VARIABLE_REF_PATTERN)]
Backend = Literal["pythonfmu", "omc"]
"""The build tool: pythonfmu for ``.py`` sources, OpenModelica ``omc`` for ``.mo`` sources."""
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
StartValue = bool | int | float | str
"""A start value as the caller gave it; the engine converts it to the variable's type."""
SetValue = bool | int | FiniteFloat
"""A live ``set`` value (ADR 0010): scalar Real, Integer, Enumeration or Boolean only (no
String, Binary or arrays); ``live.settable`` converts it to the variable's type."""
ResponseStyle = Literal["talkative", "ninja"]
"""How Claude answers: ``ninja`` (one status line, the default) or ``talkative`` (full
summaries)."""
CommandAction = Literal["pause", "resume", "step", "set", "stop", "chart"]
"""What an inbox command asks the worker of a live run to do. ``chart`` (the pane's chart picker)
is not a ``run_control`` action: the server refuses to send it."""


class _Model(BaseModel):
    """Base for every protocol model: finite floats, unknown fields ignored, immutable."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="ignore",
        frozen=True,
        json_schema_serialization_defaults_required=True,
    )


class RunState(StrEnum):
    """Lifecycle state of a run. ``status.json`` is authoritative; ``index.json`` caches it.
    ``paused``: a live run waiting at a communication point (ADR 0010)."""

    # Kept short: the docstring is published in tool schemas (run_list). A paused run is active
    # (it holds a max_workers slot and keeps its heartbeat); only its worker writes running <->
    # paused, at t0, after a pause command and after a step.

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


TERMINAL_STATES: Final = frozenset({RunState.FINISHED, RunState.FAILED, RunState.STOPPED})


class ErrorCode(StrEnum):
    """Why a run failed. New codes are additive; non-Python readers (the pane) must treat an
    unknown code as a generic failure."""

    TIMEOUT = "timeout"
    """The run used more than ``timeout_s`` of non-paused wall time; the worker killed itself."""
    WORKER_LOST = "worker_lost"
    """The worker died, never started, or its heartbeat went stale; the server killed and reaped
    it before writing this."""
    FMU_CHANGED = "fmu_changed"
    """The FMU's sha256 at worker start differs from ``spec.fmu.sha256``."""
    NOT_RUNNABLE = "not_runnable"
    """The engine cannot run this FMU here (no binary for this platform, unsupported interface)."""
    SIMULATION_ERROR = "simulation_error"
    """FMPy or the FMU reported an error during instantiation, initialization or stepping."""
    INTERNAL_ERROR = "internal_error"
    """A bug in Agentic FMI (for example a result file could not be written)."""
    BUILD_FAILED = "build_failed"
    """A build job produced no FMU: the tool reported errors, its output failed the checks, or
    the output directory was taken meanwhile. ``message`` holds the diagnostics (a count line,
    ``path:line[:col]: error: text`` lines and the build log's path), at most
    ``Limits.build_diagnostics_max_chars``; ``log_tail`` is null."""
    OMS_CHANGED = "oms_changed"
    """A system run's worker found the installed OMSimulator package tree's sha256 (or version)
    different from ``spec.omsimulator`` and refused to import it (ADR 0009). The message prints
    both digests."""


class RunError(_Model):
    """The error attached to a ``failed`` status."""

    code: ErrorCode
    message: str = Field(description="One or two sentences: what failed and what to do next.")
    log_tail: str | None = Field(
        default=None, description="The last part of log.txt (at most Limits.log_tail_bytes)."
    )


class FmuRef(_Model):
    """The FMU a run used, pinned by content hash."""

    path: str = Field(description="Absolute, symlink-resolved path at run creation.")
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    fmi_version: Literal["2.0", "3.0"]
    model_name: str


class Experiment(_Model):
    """The resolved experiment: caller overrides on top of the FMU's defaultExperiment, with
    FMPy's defaults made explicit so ``spec.json`` alone reproduces the run."""

    start_time: FiniteFloat
    stop_time: FiniteFloat
    output_interval: FiniteFloat = Field(gt=0)
    relative_tolerance: FiniteFloat | None = Field(default=None, gt=0)
    step_size: FiniteFloat | None = Field(
        default=None, gt=0, description="Model Exchange solver step; null means FMPy's default."
    )

    @model_validator(mode="after")
    def _stop_after_start(self) -> Experiment:
        if self.stop_time <= self.start_time:
            raise ValueError("stop_time must be greater than start_time")
        return self


class Constant(_Model):
    """``value`` for the whole run."""

    kind: Literal["constant"] = "constant"
    value: FiniteFloat


class Step(_Model):
    """``before`` until ``time``, then ``after``."""

    kind: Literal["step"] = "step"
    time: FiniteFloat
    before: FiniteFloat = 0.0
    after: FiniteFloat


class Ramp(_Model):
    """``before`` until ``start``, linear to ``after`` at ``end``, then ``after``."""

    kind: Literal["ramp"] = "ramp"
    start: FiniteFloat
    end: FiniteFloat
    before: FiniteFloat = 0.0
    after: FiniteFloat

    @model_validator(mode="after")
    def _end_after_start(self) -> Ramp:
        if self.end <= self.start:
            raise ValueError("ramp end must be greater than start")
        return self


class Table(_Model):
    """Time-value points with non-decreasing times, ``linear`` or zero-order ``hold``."""

    kind: Literal["table"] = "table"
    points: list[tuple[FiniteFloat, FiniteFloat]] = Field(min_length=1, max_length=1000)
    interpolation: Literal["linear", "hold"] = "linear"

    @model_validator(mode="after")
    def _times_non_decreasing(self) -> Table:
        times = [t for t, _ in self.points]
        if any(b < a for a, b in pairwise(times)):
            raise ValueError("table times must be non-decreasing")
        return self


InputSignal = Annotated[Constant | Step | Ramp | Table, Field(discriminator="kind")]
"""The inputs DSL of ``run_start``: one signal per input variable, stored in the spec as given."""


class Versions(_Model):
    """Tool versions recorded with every run (principle 3)."""

    agentic_fmi: str
    fmpy: str
    numpy: str
    python: str
    platform: str = Field(description="Machine and OS, for example x86_64-linux.")

    @classmethod
    def current(cls) -> Versions:
        """Versions of this process, read from package metadata without importing FMPy."""

        def _version(name: str) -> str:
            try:
                return version(name)
            except PackageNotFoundError:
                return "unknown"

        return cls(
            agentic_fmi=_version("agentic-fmi"),
            fmpy=_version("fmpy"),
            numpy=_version("numpy"),
            python=platform.python_version(),
            platform=f"{platform.machine()}-{sys.platform}",
        )


class RunSpec(_Model):
    """``runs/<id>/spec.json``: everything needed to reproduce the run. Written once by the
    server before the worker starts; immutable afterwards."""

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    kind: Literal["run"] = "run"
    created_at: AwareDatetime
    fmu: FmuRef
    fmi_type: Literal["CoSimulation", "ModelExchange"]
    solver: Literal["CVode", "Euler"] = Field(
        default="CVode", description="Model Exchange solver; ignored for Co-Simulation."
    )
    experiment: Experiment
    start_values: dict[str, StartValue] = Field(default_factory=dict)
    inputs: dict[str, InputSignal] = Field(
        default_factory=dict, description="Input signals keyed by variable name, as given."
    )
    outputs: list[str] = Field(description="Variables recorded in result.csv, besides time.")
    timeout_s: FiniteFloat = Field(
        gt=0, description="Limit on wall time spent stepping; time the gate waits is excluded."
    )
    estimated_steps: FiniteFloat | None = Field(
        default=None, description="Internal step-count estimate made at creation, if known."
    )
    force: bool = Field(default=False, description="True if the step-count guard was overridden.")
    versions: Versions
    live: bool = Field(
        default=False,
        description="A live run (ADR 0010): starts paused at start_time, is paced and takes "
        "inbox commands. Co-Simulation only.",
    )
    idle_limit_s: FiniteFloat | None = Field(
        default=None,
        gt=0,
        description="Live runs: seconds a run may stay paused before the worker stops it.",
    )

    @model_validator(mode="after")
    def _live_rules(self) -> RunSpec:
        _check_live(self.live, self.idle_limit_s)
        if self.live and self.fmi_type != "CoSimulation":
            raise ValueError("live runs need fmi_type CoSimulation")
        return self


def _check_live(live: bool, idle_limit_s: float | None) -> None:
    """A live spec records its idle limit; a batch spec has none."""
    if live and idle_limit_s is None:
        raise ValueError("a live run needs idle_limit_s")
    if not live and idle_limit_s is not None:
        raise ValueError("idle_limit_s applies to live runs only")


class FileRef(_Model):
    """A workspace file a system run read (the system file, the parameter set), pinned by hash."""

    path: str = Field(description="Absolute, symlink-resolved path at run creation.")
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class ComponentSpec(_Model):
    """One component of a system run: the FMU it instantiates, pinned by content hash. The
    worker simulates a private copy whose bytes it hashed (ADR 0009)."""

    fmu: FmuRef


class SystemConnection(_Model):
    """One connection, written ``{"from": "PI.u", "to": "MSD.F"}``: the value of output
    ``from`` drives input ``to`` at every communication point."""

    model_config = ConfigDict(
        serialize_by_alias=True, validate_by_alias=True, validate_by_name=True
    )

    from_: VariableRef = Field(alias="from", description="<component>.<output variable>.")
    to: VariableRef = Field(description="<component>.<input variable>.")


ParameterLayer = Literal["system", "paramset", "start_values"]
"""Where a resolved value came from, lowest precedence first (the FMU's own start values are
the implicit layer below): the system file, the run's ``.ssv`` parameter set, ``start_values``."""


class ResolvedValue(_Model):
    """A value the worker sets before initialization, and the layer that supplied it."""

    value: StartValue
    source: ParameterLayer


class MasterAlgorithm(_Model):
    """How OMSimulator couples the components (spike (g)): its fixed-step ``oms_ma`` master
    steps every component from the same communication point with the inputs of the previous
    exchange (Jacobi), then copies all outputs to the connected inputs. Results are sampled
    after each exchange (post-exchange values)."""

    method: Literal["oms_ma"] = "oms_ma"
    coupling: Literal["jacobi"] = "jacobi"
    step_size: FiniteFloat = Field(gt=0, description="Communication step h in seconds.")


class OmsVersion(_Model):
    """The OMSimulator package a system run uses: distribution version and package-tree sha256
    (``oms_pin.package_digest``), checked by the server and again by the worker before import."""

    version: str
    sha256: Sha256


class SystemSpec(_Model):
    """``runs/<id>/spec.json`` of a system run (``kind: "system"``, ADR 0009): the system fully
    resolved, so the worker builds the OMSimulator model from it alone and the pane can draw
    it. Written once by the server before the worker starts; immutable afterwards.

    ``experiment.output_interval`` equals ``master.step_size``: one result row per
    communication point. ``experiment.relative_tolerance`` and ``step_size`` stay null."""

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    kind: Literal["system"] = "system"
    created_at: AwareDatetime
    name: str = Field(description="The system's display name: the system file's stem.")
    system: FileRef = Field(description="The system file as read at run creation.")
    paramset: FileRef | None = Field(default=None, description="The .ssv parameter set, if any.")
    components: dict[ComponentName, ComponentSpec] = Field(min_length=1)
    connections: list[SystemConnection] = Field(default_factory=list)
    parameters: dict[VariableRef, ResolvedValue] = Field(
        default_factory=dict,
        description="Values set before initialization, keyed <component>.<variable>, with the "
        "layer that won (system < paramset < start_values).",
    )
    master: MasterAlgorithm
    experiment: Experiment
    outputs: list[VariableRef] = Field(description="Columns of result.csv after time.")
    timeout_s: FiniteFloat = Field(
        gt=0, description="Limit on wall time spent stepping; time the gate waits is excluded."
    )
    estimated_steps: FiniteFloat | None = Field(
        default=None, description="Communication steps: (stop_time - start_time) / step_size."
    )
    force: bool = Field(default=False, description="True if the step-count guard was overridden.")
    versions: Versions
    omsimulator: OmsVersion
    live: bool = Field(
        default=False,
        description="A live run (ADR 0010): starts paused at start_time, is paced and takes "
        "inbox commands.",
    )
    idle_limit_s: FiniteFloat | None = Field(
        default=None,
        gt=0,
        description="Live runs: seconds a run may stay paused before the worker stops it.",
    )

    @model_validator(mode="after")
    def _one_row_per_step(self) -> SystemSpec:
        if self.experiment.output_interval != self.master.step_size:
            raise ValueError("experiment.output_interval must equal master.step_size")
        _check_live(self.live, self.idle_limit_s)
        return self


class SourceRef(_Model):
    """The source file a build used, pinned by content hash."""

    path: str = Field(description="Workspace-relative POSIX path of the source as given.")
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class BuildSpec(_Model):
    """``runs/<id>/spec.json`` of a build job (``kind: "build"``, ADR 0008). Written once by the
    server before the worker starts; immutable afterwards. The worker builds from the private
    copy ``runs/<id>/source/<file name>``, whose sha256 is ``source.sha256``."""

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    kind: Literal["build"] = "build"
    created_at: AwareDatetime
    workspace: str = Field(description="Absolute workspace root; the relative paths are under it.")
    source: SourceRef
    name: BuildName
    fmu_path: str = Field(description="Workspace-relative output: generated/<name>/<name>.fmu.")
    backend: Backend
    options: dict[str, str] = Field(
        description="The fixed tool options, e.g. fmi_version, fmu_type and, for omc, fmi_flags."
    )
    overwrite: bool = Field(
        default=False, description="Replace an earlier build of this name (one with build.json)."
    )
    timeout_s: FiniteFloat = Field(gt=0, description="Limit on the build's wall time.")
    versions: Versions


class BuildVersions(_Model):
    """Tool versions recorded with every build (principle 3)."""

    agentic_fmi: str
    python: str
    platform: str = Field(description="Machine and OS, for example x86_64-linux.")
    pythonfmu: str | None = Field(default=None, description="pythonfmu builds only.")
    omc: str | None = Field(default=None, description="omc builds only: getVersion().")
    msl: str | None = Field(
        default=None, description="omc builds only: the Modelica Standard Library loaded."
    )


class BuiltFmu(_Model):
    """The FMU a build wrote, pinned by content hash."""

    path: str = Field(description="Workspace-relative POSIX path: generated/<name>/<name>.fmu.")
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    fmi_version: Literal["2.0"]
    model_name: str
    model_identifier: str
    interfaces: list[Literal["CoSimulation", "ModelExchange"]] = Field(min_length=1)


class BuildExperiment(_Model):
    """The built FMU's DefaultExperiment; each field is null if the FMU doesn't declare it."""

    start_time: FiniteFloat | None = None
    stop_time: FiniteFloat | None = None
    step_size: FiniteFloat | None = None
    tolerance: FiniteFloat | None = None


class BuildSummary(_Model):
    """Compact inspect facts of the built FMU, read from its modelDescription.xml only."""

    counts: dict[str, int] = Field(description="Variables per causality, plus 'total'.")
    inputs: list[str] = Field(description="Input names (at most Limits.inspect_max_items).")
    outputs: list[str] = Field(description="Output names (at most Limits.inspect_max_items).")
    parameters: list[str] = Field(
        description="Parameter names, tunable first (at most Limits.inspect_max_items)."
    )
    default_experiment: BuildExperiment | None = None


class BuildRecord(_Model):
    """``build.json``: the provenance of one built FMU (ADR 0008). The worker writes it next to
    the FMU (``generated/<name>/build.json``, replaced by a later ``overwrite`` build) and keeps
    the same record in ``runs/<id>/build.json``. ``fmu.sha256`` is the sha256 of the FMU file
    written with it."""

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId = Field(description="The build job; its log is runs/<run_id>/build.log.")
    name: BuildName
    built_at: AwareDatetime
    backend: Backend
    source: SourceRef
    options: dict[str, str]
    versions: BuildVersions
    fmu: BuiltFmu
    duration_s: FiniteFloat = Field(ge=0, description="Wall time of the build tool.")
    warnings: list[str] = Field(
        default_factory=list,
        description="Tool warnings as 'path:line[:col]: warning: text' lines (capped).",
    )
    replaced_sha256: Sha256 | None = Field(
        default=None, description="sha256 of the FMU this build replaced (overwrite only)."
    )
    summary: BuildSummary


class RunStatus(_Model):
    """``runs/<id>/status.json``: the authoritative state. Single writer at a time: the server
    writes ``queued``; the worker owns the file while it lives; the server writes a terminal
    state only after it has confirmed the worker is gone (ADR 0002, ADR 0007)."""

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    state: RunState
    t: FiniteFloat | None = Field(default=None, description="Last simulation time reached.")
    t_start: FiniteFloat
    t_stop: FiniteFloat
    progress: FiniteFloat = Field(default=0.0, ge=0.0, le=1.0)
    updated_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    heartbeat_at: AwareDatetime | None = Field(
        default=None, description="Written by the worker's heartbeat thread, independent of t."
    )
    pid: int | None = Field(default=None, description="Worker pid; also its process group id.")
    pid_start_time: int | None = Field(
        default=None,
        description="Field 22 of /proc/<pid>/stat (clock ticks since boot); guards pid reuse.",
    )
    error: RunError | None = None
    last_cmd: str | None = Field(
        default=None,
        description="Live runs: the ID of the last inbox command that got an outcome (a display "
        "field; outcomes/<id>.json is the record).",
    )
    heartbeat_mono: FiniteFloat | None = Field(
        default=None,
        description="The worker's time.monotonic() (CLOCK_MONOTONIC, shared by the processes of "
        "one machine and blind to wall-clock jumps) when it wrote heartbeat_at. Staleness uses "
        "it; files without it fall back to heartbeat_at.",
    )
    speed: FiniteFloat | None = Field(
        default=None,
        gt=0,
        description="Live runs: the pacing factor (1 = real time); null for batch runs.",
    )
    reason: str | None = Field(
        default=None,
        description="Why a run ended where its state alone doesn't say: the live idle limit, or "
        "an FMU that ended the run before t_stop. Null otherwise.",
    )


class VariableSummary(_Model):
    """Compact statistics of one recorded variable. Non-finite or non-numeric values are null."""

    name: str
    unit: str | None = None
    min: FiniteFloat | None = None
    max: FiniteFloat | None = None
    final: FiniteFloat | None = None
    min_at: FiniteFloat | None = Field(default=None, description="Time of the first minimum.")
    max_at: FiniteFloat | None = Field(default=None, description="Time of the first maximum.")


class RunSummary(_Model):
    """``runs/<id>/summary.json``: compact results, written by the worker with ``result.csv``
    when a run finishes or stops."""

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    state: RunState
    t_end: FiniteFloat
    n_points: int = Field(ge=0)
    wall_time_s: FiniteFloat = Field(ge=0)
    columns: list[str] = Field(description="result.csv columns after time.")
    variables: list[VariableSummary]


class IndexEntry(_Model):
    """One run, build job or system run in ``index.json``. ``state`` is a cached copy of
    ``status.json``."""

    run_id: RunId
    kind: Literal["run", "build", "system"] = "run"
    state: RunState
    fmu_name: str = Field(
        description="The FMU's model name; for a build job, the build name; for a system run, "
        "the system's name."
    )
    fmu_path: str = Field(
        description="Absolute FMU path; for a build job, the output FMU; for a system run, the "
        "system file."
    )
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Index(_Model):
    """``index.json``: every run in ascending run number. Written by MCP servers only, under the
    store lock."""

    protocol: Literal[1] = PROTOCOL_VERSION
    runs: list[IndexEntry] = Field(default_factory=list)


class EventType(StrEnum):
    """Lifecycle events of runs and build jobs. New types are additive; ``fmu_built`` lines are
    :class:`BuildEvent`, not :class:`Event`. ``command``: the worker of a live run
    appends one per command outcome; its message names the command ID, the action, applied or
    rejected, and the simulation time."""

    CREATED = "created"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"
    COMMAND = "command"


class Event(_Model):
    """One line of ``events.jsonl``, at most :data:`MAX_EVENT_LINE_BYTES` bytes."""

    protocol: Literal[1] = PROTOCOL_VERSION
    at: AwareDatetime
    run_id: RunId
    type: EventType
    source: Literal["server", "worker"]
    message: str | None = None


class BuildEvent(_Model):
    """The ``events.jsonl`` line a successful build job appends once, after it wrote the FMU and
    ``build.json`` (ADR 0008). Readers tell it from :class:`Event` by ``type``; run lifecycle
    events are unchanged."""

    protocol: Literal[1] = PROTOCOL_VERSION
    at: AwareDatetime
    run_id: RunId = Field(description="The build job.")
    type: Literal["fmu_built"] = "fmu_built"
    source: Literal["worker"] = "worker"
    fmu_path: str = Field(description="Workspace-relative path of the new FMU.")
    fmu_sha256: Sha256
    message: str | None = None


class Command(_Model):
    """``runs/<id>/commands/<id>.json``: one request to the worker of a live run (ADR 0010).

    Written once and never changed: by the server through an exclusive create (a temporary file
    linked into place), or by the pane with ``$.fs.write`` (not atomic, so the worker retries a
    file that doesn't parse until ``Timing.inbox_grace_s``, then rejects it). At most
    :data:`MAX_COMMAND_BYTES`. The worker applies each file at most once, in name order, and
    writes exactly one :class:`CommandOutcome` for it. The arguments must match the action:
    ``n`` only for ``step`` (required there), ``values`` only for ``set`` (required there),
    ``speed`` only for ``resume`` (optional), ``names`` only for ``chart`` (required there; an
    empty list charts nothing; at most 16 names, each once).
    """

    protocol: Literal[1] = PROTOCOL_VERSION
    id: CommandId = Field(description="The file name without .json.")
    source: Literal["server", "pane"] = Field(description="Equals the writer part of id.")
    issued_at: AwareDatetime
    action: CommandAction
    n: int | None = Field(
        default=None, ge=1, description="step: whole communication steps to advance."
    )
    values: dict[str, SetValue] | None = Field(
        default=None,
        min_length=1,
        description="set: variable name (<component>.<variable> in a system) to value.",
    )
    speed: FiniteFloat | None = Field(
        default=None, gt=0, description="resume: the new pacing factor (1 = real time)."
    )
    names: list[str] | None = Field(
        default=None,
        max_length=16,
        description="chart: the recorded outputs to chart, in order (empty: none).",
    )

    @model_validator(mode="after")
    def _arguments_match(self) -> Command:
        if self.id.split("-")[1] != self.source:
            raise ValueError("source must equal the writer part of id")
        if (self.n is not None) != (self.action == "step"):
            raise ValueError("n is required for step and allowed only there")
        if (self.values is not None) != (self.action == "set"):
            raise ValueError("values is required for set and allowed only there")
        if self.speed is not None and self.action != "resume":
            raise ValueError("speed is allowed only with resume")
        if (self.names is not None) != (self.action == "chart"):
            raise ValueError("names is required for chart and allowed only there")
        if self.names is not None and len(set(self.names)) != len(self.names):
            raise ValueError("names must not repeat a name")
        return self


class CommandOutcome(_Model):
    """``runs/<id>/outcomes/<command id>.json``: what the worker did with one command file.

    Exactly one per command file the worker found (a file it could not read too, once its grace
    ran out), written atomically by the worker only, before the matching ``command`` event. A
    reader finds it in O(1) from the command ID. The command files plus their outcomes (each
    with its simulation time ``t``) are the run's replay log (principle 3).
    """

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    id: CommandId
    source: Literal["server", "pane"]
    action: CommandAction | None = Field(
        default=None, description="Null when the file could not be read as a Command."
    )
    status: Literal["applied", "rejected"]
    t: FiniteFloat = Field(
        description="Simulation time when it took effect or was rejected; for step, the time "
        "the last step reached."
    )
    at: AwareDatetime
    message: str | None = Field(
        default=None,
        description="Why it was rejected, or a note (already paused, 3 of 5 steps before the "
        "run ended). At most Limits.message_max_chars.",
    )


class LiveColumn(_Model):
    """One recorded scalar output at ``LiveSnapshot.t``: its unit and value (null when
    non-finite)."""

    name: str
    unit: str | None = None
    value: FiniteFloat | None = None


class LiveSnapshot(_Model):
    """``runs/<id>/live.json``: the live chart history and current values of a live run
    (ADR 0010).

    Written only by the worker's heartbeat thread, atomically, at most 5 times a second and only
    when a new sample arrived; kept after the run ends, so a pane that reopens still draws it.
    ``values`` holds every scalar recorded output (``spec.outputs`` order, at most
    ``Limits.live_max_columns``) sampled at ``t``. ``time`` and ``series`` are the ring buffer:
    the last ``Limits.live_points`` communication points of the current chart selection.
    ``series`` holds exactly the selected names, in order: by default the first
    ``Limits.live_variables`` names of ``values``, replaced by a ``chart`` command (the worker
    keeps that history for every column, so a newly charted name has its full history at once).
    The whole file stays under ``Limits.live_max_bytes`` (the writer drops the oldest history
    points until it fits). Batch runs have no ``live.json``.
    """

    protocol: Literal[1] = PROTOCOL_VERSION
    run_id: RunId
    seq: int = Field(ge=1, description="Increases with every write.")
    t: FiniteFloat = Field(description="Simulation time of values.")
    values: list[LiveColumn]
    time: list[FiniteFloat] = Field(description="History times, ascending.")
    series: dict[str, list[FiniteFloat | None]] = Field(
        description="History of each charted variable (the chart selection), one value per "
        "entry of time."
    )

    @model_validator(mode="after")
    def _series_match_time(self) -> LiveSnapshot:
        if any(len(column) != len(self.time) for column in self.series.values()):
            raise ValueError("every series needs one value per entry of time")
        return self


class Preferences(_Model):
    """``preferences.json`` in the store root: the user's choices for this workspace. Written
    only by the MCP server (the ``response_style`` tool, atomically) and read on each call that
    needs it; a missing or unreadable file means the defaults."""

    protocol: Literal[1] = PROTOCOL_VERSION
    response_style: ResponseStyle = Field(
        default="ninja",
        description="ninja: run, build and live-control results carry a status line and Claude "
        "replies with it. talkative: tool results carry summaries and Claude explains them.",
    )


DOCUMENTS: Final[dict[str, type[BaseModel]]] = {
    "spec": RunSpec,
    "build-spec": BuildSpec,
    "system-spec": SystemSpec,
    "status": RunStatus,
    "summary": RunSummary,
    "index": Index,
    "event": Event,
    "build-event": BuildEvent,
    "build": BuildRecord,
    "command": Command,
    "command-outcome": CommandOutcome,
    "live": LiveSnapshot,
    "preferences": Preferences,
}
"""Schema file stem -> model. ``docs/schemas/<stem>.schema.json`` is generated from each."""


def utc_now() -> datetime:
    """The current time as an aware UTC datetime, the only timestamp form the store uses."""
    return datetime.now(UTC)


def schema_for(model: type[BaseModel]) -> dict[str, object]:
    """The JSON Schema of ``model`` as written to disk: serialization mode, so every field a
    writer always emits is required."""
    schema = model.model_json_schema(mode="serialization")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def export_schemas(directory: Path) -> list[Path]:
    """Write ``<stem>.schema.json`` for every model in :data:`DOCUMENTS` and return the paths.

    The output is deterministic (2-space indent, LF, trailing newline), so the committed copies
    in ``docs/schemas/`` can be compared byte for byte. Regenerate them with
    ``uv run python -c "from pathlib import Path; from agentic_fmi.store.protocol import
    export_schemas; export_schemas(Path('docs/schemas'))"``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for stem, model in DOCUMENTS.items():
        path = directory / f"{stem}.schema.json"
        text = json.dumps(schema_for(model), indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
