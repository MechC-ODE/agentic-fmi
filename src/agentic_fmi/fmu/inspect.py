"""Parse ``modelDescription.xml`` into compact, model-friendly facts: metadata, interfaces,
variables (filtered and paged), runnability on this platform, the resolved experiment and the
internal step-count estimate.

Never loads FMU binaries: it uses FMPy's ``read_model_description`` and ``supported_platforms``
(XML and zip directory only), and ``fmpy.util.auto_interval``. The ruff rule TID251 enforces this.

Collaborators: ``fmu/catalog.py`` (path resolution), ``runs.py`` (``run_start`` uses
:func:`resolve_experiment`, :func:`check_run` and :func:`require_variables`), ``signals.py``
(variable types for inputs), ``mcp/server.py`` (``fmu_inspect``, ``fmu_variables``).
"""

from __future__ import annotations

import difflib
import hashlib
import math
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal, get_args

import fmpy
from fmpy import read_model_description, supported_platforms
from fmpy.model_description import ValidationError as FmpyValidationError
from fmpy.util import auto_interval
from pydantic import BaseModel

from agentic_fmi.config import Config, Limits
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.catalog import matches, page_window, relative_path, resolve_fmu
from agentic_fmi.store.protocol import Experiment

FmiType = Literal["CoSimulation", "ModelExchange"]
Interface = Literal["CoSimulation", "ModelExchange", "ScheduledExecution"]
Causality = Literal[
    "parameter",
    "calculatedParameter",
    "input",
    "output",
    "local",
    "independent",
    "structuralParameter",
]
Variability = Literal["constant", "fixed", "tunable", "discrete", "continuous"]

ModelDescription = Any
"""FMPy's ``fmpy.model_description.ModelDescription`` (FMPy ships no type hints)."""

MODEL_DESCRIPTION = "modelDescription.xml"
"""The archive member that describes the FMU."""

SUPPORTED_FMI = "2.0 and 3.0"

VARIABLE_DESCRIPTION_CHARS = 120
"""Longest ``VariableInfo.description``."""

ERROR_TEXT_CHARS = 200
"""Longest foreign (FMPy or XML parser) error text quoted in a :class:`UserError`."""

MAX_OUTPUT_INTERVALS = 1000
"""FMPy doubles an output interval until the run has at most this many of them."""

MAX_CLOSE_MATCHES = 3
"""Suggestions offered for an unknown variable name."""

SCHEDULED_ONLY = "ScheduledExecution only"
"""The blocker for an FMU that declares ScheduledExecution but neither CS nor ME."""

SCHEDULED_EXPLAINED = (
    "ScheduledExecution only. FMPy runs CoSimulation and ModelExchange FMUs; use an FMU that "
    "declares one."
)
"""The same blocker with the next step, for error messages."""


class VariableInfo(BaseModel):
    """One model variable, as compact as possible. Attribute values are the XML strings."""

    name: str
    causality: str
    variability: str
    type: str
    unit: str | None = None
    start: str | None = None
    min: str | None = None
    max: str | None = None
    description: str | None = None
    """Truncated to 120 characters."""


class VariablePage(BaseModel):
    """A filtered page of variables in modelDescription order."""

    fmu: str
    total: int
    """Number of variables matching the filters, across all pages."""
    offset: int
    limit: int
    items: list[VariableInfo]
    next_offset: int | None = None


class DefaultExperiment(BaseModel):
    """The FMU's own ``DefaultExperiment`` as floats (null when absent)."""

    start_time: float | None = None
    stop_time: float | None = None
    step_size: float | None = None
    tolerance: float | None = None


class InterfaceCheck(BaseModel):
    """Whether one interface can run its default experiment here."""

    fmi_type: FmiType
    runnable: bool
    estimated_steps: float | None = None
    """Internal steps for the default experiment (Co-Simulation with fixedInternalStepSize)."""
    reason: str | None = None
    """Why not, with the next step (e.g. "use fmi_type='ModelExchange'")."""


class Runnability(BaseModel):
    """Can FMPy run this FMU on this machine, and through which interfaces."""

    runnable: bool
    """True if at least one interface in :attr:`interfaces` is runnable."""
    current_platform: str
    """FMPy's platform name for this machine, e.g. ``linux64``."""
    platforms: list[str]
    """FMPy's ``supported_platforms``: binary platforms plus ``c-code`` for source FMUs."""
    interfaces: list[InterfaceCheck]
    reasons: list[str] = []
    """FMU-level blockers: no binary for this platform, source-only, ScheduledExecution only."""


class FmuInfo(BaseModel):
    """The ``fmu_inspect`` result. Lists hold at most ``Limits.inspect_max_items`` entries."""

    path: str
    sha256: str
    size_bytes: int
    fmi_version: str
    model_name: str
    description: str | None = None
    generation_tool: str | None = None
    interfaces: list[Interface]
    default_experiment: DefaultExperiment | None = None
    runnability: Runnability
    counts: dict[str, int]
    """Variables per causality, plus ``total``."""
    inputs: list[VariableInfo]
    outputs: list[VariableInfo]
    parameters: list[VariableInfo]
    """Tunable parameters first, then fixed ones."""
    truncated: bool = False
    """True if a list was cut; call ``fmu_variables`` for the rest."""


def truncate(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, ending in ``...`` when it was cut."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def declared_fmi_version(path: Path) -> str:
    """The ``fmiVersion`` attribute of the FMU's ``modelDescription.xml`` root element.

    Reads only the first start tag, so it works for FMI 1.0 files that FMPy's schema rejects.

    Raises:
        UserError: not a zip, no modelDescription.xml, malformed XML, or no ``fmiVersion``.
    """
    try:
        with zipfile.ZipFile(path) as archive, archive.open(MODEL_DESCRIPTION) as xml:
            for _, root in ET.iterparse(xml, events=("start",)):
                version = root.get("fmiVersion")
                break
            else:
                version = None
    except zipfile.BadZipFile as exc:
        raise UserError(
            f"{path.name} is not a valid FMU: it is not a zip archive. Re-export or re-download it."
        ) from exc
    except KeyError as exc:
        raise UserError(
            f"{path.name} is not a valid FMU: the archive has no {MODEL_DESCRIPTION}."
        ) from exc
    except ET.ParseError as exc:
        raise UserError(
            f"{path.name}: {MODEL_DESCRIPTION} is not well-formed XML ({truncate(str(exc), 100)}). "
            "Re-export the FMU."
        ) from exc
    except OSError as exc:
        raise UserError(f"Can't read {path.name}: {exc.strerror}.") from exc
    if not version:
        raise UserError(f"{path.name}: {MODEL_DESCRIPTION} has no fmiVersion attribute.")
    return version


def read_description(path: Path) -> ModelDescription:
    """Parse ``modelDescription.xml`` from the FMU at ``path`` (no binaries).

    Raises:
        UserError: not a zip, no modelDescription.xml, invalid XML, or FMI 1.0 ("FMI 1.0 is not
            supported; export the model as FMI 2.0 or 3.0").
    """
    version = declared_fmi_version(path)
    if version == "1.0":
        raise UserError(
            f"{path.name}: FMI 1.0 is not supported; export the model as FMI 2.0 or 3.0."
        )
    if version != "2.0" and not version.startswith("3."):
        raise UserError(
            f"{path.name} declares FMI version {version!r}; supported versions are {SUPPORTED_FMI}."
        )
    try:
        return read_model_description(str(path), validate=True)
    except FmpyValidationError as exc:
        first = truncate(str(exc.problems[0]), ERROR_TEXT_CHARS) if exc.problems else "unknown"
        raise UserError(
            f"{path.name}: {MODEL_DESCRIPTION} violates the FMI schema "
            f"({len(exc.problems)} problems; first: {first}). Call fmu_validate for the full list."
        ) from exc
    except Exception as exc:  # FMPy raises bare Exception for malformed content
        raise UserError(
            f"{path.name}: can't read {MODEL_DESCRIPTION}: {truncate(str(exc), ERROR_TEXT_CHARS)}"
        ) from exc


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of the file, read in 1 MiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def interfaces_of(md: ModelDescription) -> list[Interface]:
    """The interfaces the FMU declares, in CS, ME, SE order."""
    declared: list[tuple[Any, Interface]] = [
        (md.coSimulation, "CoSimulation"),
        (md.modelExchange, "ModelExchange"),
        (md.scheduledExecution, "ScheduledExecution"),
    ]
    return [name for element, name in declared if element is not None]


def _fmi_types(md: ModelDescription) -> list[FmiType]:
    """The interfaces FMPy can run (CS, ME order) among those the FMU declares."""
    types: list[FmiType] = []
    if md.coSimulation is not None:
        types.append("CoSimulation")
    if md.modelExchange is not None:
        types.append("ModelExchange")
    return types


def default_fmi_type(md: ModelDescription) -> FmiType:
    """FMPy's default choice: Co-Simulation if declared, else Model Exchange.

    Raises:
        UserError: the FMU declares only ScheduledExecution (unsupported).
    """
    if md.coSimulation is not None:
        return "CoSimulation"
    if md.modelExchange is not None:
        return "ModelExchange"
    raise UserError(f"{md.modelName}: {SCHEDULED_EXPLAINED}")


def _to_float(text: str | None) -> float | None:
    """A float from an XML attribute string; ``None`` if absent or not a number."""
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _default_value(md: ModelDescription, attribute: str) -> float | None:
    """One ``DefaultExperiment`` attribute as a float, or ``None``."""
    experiment = md.defaultExperiment
    return None if experiment is None else _to_float(getattr(experiment, attribute))


def _fixed_internal_step(md: ModelDescription) -> float | None:
    """``fixedInternalStepSize`` of the Co-Simulation interface; ``None`` if absent or invalid."""
    interface = md.coSimulation
    value = None if interface is None else _to_float(interface.fixedInternalStepSize)
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _require_positive(name: str, value: float) -> None:
    """Raise :class:`UserError` unless ``value`` is a finite number above zero."""
    if not math.isfinite(value) or value <= 0:
        raise UserError(f"{name} must be a positive number (got {value:g}). Pass a larger {name}.")


def _coarsen(interval: float, horizon: float) -> float:
    """Double ``interval`` until the horizon holds at most :data:`MAX_OUTPUT_INTERVALS` of them."""
    while horizon / interval > MAX_OUTPUT_INTERVALS:
        interval *= 2
    return interval


def _default_interval(
    md: ModelDescription, fmi_type: FmiType, horizon: float, solver_step: float | None
) -> float:
    """FMPy 0.3.32's output interval when the caller gives none (see :func:`resolve_experiment`)."""
    if fmi_type == "ModelExchange":
        solver = 10.0 ** (round(math.log10(horizon)) - 3) if solver_step is None else solver_step
        return _coarsen(solver, horizon)
    declared = _fixed_internal_step(md)
    if declared is None:
        declared = _default_value(md, "stepSize")
    if declared is None or declared <= 0:
        return float(auto_interval(horizon))
    return _coarsen(declared, horizon)


def resolve_experiment(
    md: ModelDescription,
    fmi_type: FmiType,
    *,
    start_time: float | None = None,
    stop_time: float | None = None,
    output_interval: float | None = None,
    relative_tolerance: float | None = None,
    step_size: float | None = None,
) -> Experiment:
    """Caller overrides on top of the DefaultExperiment, with FMPy 0.3.32's defaults made explicit.

    Mirrors ``simulate_fmu``: start 0 and stop start+1 when absent; for Co-Simulation the
    output interval is ``fixedInternalStepSize``, else ``stepSize``, doubled until there are at
    most 1000 intervals, else ``auto_interval``; for Model Exchange it is FMPy's ``step_size``
    (``10**(round(log10(T)) - 3)``) doubled the same way. Example: BouncingBall 3.0 CS gives
    0.004 (751 rows), BouncingBall 2.0 CS gives 0.01 (301 rows).

    ``step_size`` is the Model Exchange solver step and is dropped (``None``) for Co-Simulation.
    A caller's ``output_interval`` is used as given. ``relative_tolerance`` stays ``None`` when
    neither the caller nor the FMU sets it.

    Raises:
        UserError: stop_time <= start_time, or a non-positive interval or tolerance.
    """
    start = _default_value(md, "startTime") if start_time is None else start_time
    start = 0.0 if start is None else start
    stop = _default_value(md, "stopTime") if stop_time is None else stop_time
    stop = start + 1.0 if stop is None else stop
    if not (math.isfinite(start) and math.isfinite(stop)) or stop <= start:
        raise UserError(
            f"stop_time ({stop:g}) must be greater than start_time ({start:g}). "
            "Pass a stop_time after the start."
        )
    tolerance = (
        _default_value(md, "tolerance") if relative_tolerance is None else relative_tolerance
    )
    if tolerance is not None:
        _require_positive("relative_tolerance", tolerance)
    solver_step = step_size if fmi_type == "ModelExchange" else None
    if solver_step is not None:
        _require_positive("step_size", solver_step)
    if output_interval is not None:
        _require_positive("output_interval", output_interval)
    interval = (
        _default_interval(md, fmi_type, stop - start, solver_step)
        if output_interval is None
        else output_interval
    )
    return Experiment(
        start_time=start,
        stop_time=stop,
        output_interval=interval,
        relative_tolerance=tolerance,
        step_size=solver_step,
    )


def estimate_internal_steps(
    md: ModelDescription, fmi_type: FmiType, experiment: Experiment
) -> float | None:
    """Internal steps the FMU will take: ``(stop - start) / fixedInternalStepSize`` for
    Co-Simulation FMUs that declare it (Roberts 3.0 CS default: about 1e11); otherwise the
    number of output intervals. ``None`` if unknown."""
    horizon = experiment.stop_time - experiment.start_time
    fixed = _fixed_internal_step(md) if fmi_type == "CoSimulation" else None
    steps = horizon / (experiment.output_interval if fixed is None else fixed)
    return steps if math.isfinite(steps) else None


def _binary_platforms(platforms: Sequence[str]) -> list[str]:
    """The entries of FMPy's platform list that are compiled binaries (not ``c-code``)."""
    return [name for name in platforms if name != "c-code"]


def platform_blocker(path: Path, md: ModelDescription) -> str | None:
    """Why FMPy can't run this FMU at all on this machine, or ``None`` if it can.

    Covers ScheduledExecution-only FMUs, source-only FMUs and FMUs without a binary for
    ``fmpy.platform``. Doesn't look at the experiment.
    """
    if md.coSimulation is None and md.modelExchange is None:
        return SCHEDULED_ONLY
    platforms = list(supported_platforms(str(path)))
    if fmpy.platform in platforms:
        return None
    binaries = _binary_platforms(platforms)
    advice = f"Use an FMU built for {fmpy.platform}."
    if binaries:
        return f"no binary for {fmpy.platform} (the FMU ships {', '.join(binaries)}). {advice}"
    if "c-code" in platforms:
        return f"source-only FMU: no binaries, and sources are not compiled. {advice}"
    return f"the archive has no binaries for any platform. {advice}"


def _step_limit_message(md: ModelDescription, fmi_type: FmiType, steps: float, limit: float) -> str:
    """The text for an estimate above the limit, with the options for getting under it."""
    options = []
    if fmi_type == "CoSimulation" and md.modelExchange is not None:
        options.append("use fmi_type='ModelExchange' (the solver chooses its own steps)")
    if fmi_type == "CoSimulation":
        options.append("shorten stop_time")
    else:
        options.append("shorten stop_time or raise output_interval")
    return (
        f"about {steps:.3g} internal steps, above the limit of {limit:.3g}. "
        f"Options: {'; '.join(options)}; or pass force=true with a timeout_s."
    )


def _check_interface(
    md: ModelDescription, fmi_type: FmiType, blocker: str | None, limits: Limits
) -> InterfaceCheck:
    """The default-experiment check for one declared interface."""
    if blocker is not None:
        return InterfaceCheck(fmi_type=fmi_type, runnable=False, reason=blocker)
    try:
        experiment = resolve_experiment(md, fmi_type)
    except UserError as exc:
        return InterfaceCheck(
            fmi_type=fmi_type, runnable=False, reason=f"the default experiment is invalid: {exc}"
        )
    steps = estimate_internal_steps(md, fmi_type, experiment)
    if steps is not None and steps > limits.max_internal_steps:
        message = _step_limit_message(md, fmi_type, steps, limits.max_internal_steps)
        return InterfaceCheck(
            fmi_type=fmi_type, runnable=False, estimated_steps=steps, reason=message
        )
    return InterfaceCheck(fmi_type=fmi_type, runnable=True, estimated_steps=steps)


def check_runnability(path: Path, md: ModelDescription, limits: Limits) -> Runnability:
    """Runnability of the FMU's default experiment on this platform, per interface.

    Blockers: no binary for ``fmpy.platform`` (source-only FMUs are not compiled in Phase 1;
    remoting is never used), ScheduledExecution only (Clocks: ``runnable: false``), and for
    Co-Simulation an estimate above ``limits.max_internal_steps`` (Roberts 3.0: the CS check
    fails with a pointer to Model Exchange, the ME check passes).
    """
    blocker = platform_blocker(path, md)
    checks = [_check_interface(md, name, blocker, limits) for name in _fmi_types(md)]
    return Runnability(
        runnable=any(check.runnable for check in checks),
        current_platform=fmpy.platform,
        platforms=list(supported_platforms(str(path))),
        interfaces=checks,
        reasons=[] if blocker is None else [blocker],
    )


def check_run(
    path: Path,
    md: ModelDescription,
    fmi_type: FmiType,
    experiment: Experiment,
    limits: Limits,
    force: bool,
) -> float | None:
    """Gate for ``run_start``: return the step estimate, or raise if the run must not start.

    Raises:
        UserError: FMI type not declared, ScheduledExecution, no binary for this platform, or
            estimate above ``limits.max_internal_steps`` without ``force`` (message names the
            estimate and suggests ModelExchange, a shorter stop_time, or force with timeout_s).
    """
    blocker = platform_blocker(path, md)
    if blocker is not None:
        explained = SCHEDULED_EXPLAINED if blocker == SCHEDULED_ONLY else blocker
        raise UserError(f"Can't run {path.name} here: {explained}")
    declared = _fmi_types(md)
    if fmi_type not in declared:
        raise UserError(
            f"{path.name} doesn't declare fmi_type={fmi_type!r}; it offers {', '.join(declared)}. "
            "Pass one of those as fmi_type."
        )
    steps = estimate_internal_steps(md, fmi_type, experiment)
    if steps is not None and steps > limits.max_internal_steps and not force:
        message = _step_limit_message(md, fmi_type, steps, limits.max_internal_steps)
        raise UserError(f"Refusing to start {path.name} ({fmi_type}): {message}")
    return steps


def require_variables(
    md: ModelDescription,
    names: Iterable[str],
    *,
    purpose: str,
    causalities: Sequence[str] | None = None,
) -> None:
    """Check that every name exists (and has an allowed causality, if given).

    Raises:
        UserError: e.g. "Unknown variable 'hh' in start_values. Close matches: h. Call
            fmu_variables to list names." Close matches come from ``difflib.get_close_matches``.
    """
    by_name = {variable.name: variable for variable in md.modelVariables}
    unknown: list[str] = []
    wrong_causality: list[Any] = []
    for name in names:
        variable = by_name.get(name)
        if variable is None:
            unknown.append(name)
        elif causalities is not None and variable.causality not in causalities:
            wrong_causality.append(variable)
    if unknown:
        close = difflib.get_close_matches(unknown[0], list(by_name), n=MAX_CLOSE_MATCHES)
        text = f"Unknown variable {unknown[0]!r} in {purpose}."
        if close:
            text += f" Close matches: {', '.join(close)}."
        if len(unknown) > 1:
            text += f" Also unknown: {', '.join(repr(name) for name in unknown[1:])}."
        raise UserError(f"{text} Call fmu_variables to list names.")
    if wrong_causality and causalities is not None:
        variable = wrong_causality[0]
        raise UserError(
            f"Variable {variable.name!r} in {purpose} has causality {variable.causality!r}, but "
            f"{purpose} accepts {' or '.join(causalities)}. Call fmu_variables with that "
            "causality to list valid names."
        )


def variable_types(md: ModelDescription) -> dict[str, str]:
    """Variable name -> FMPy type name (``Real``, ``Float64``, ``Integer``, ``Boolean``, ...)."""
    return {variable.name: variable.type for variable in md.modelVariables}


def _declared_attribute(variable: Any, attribute: str) -> str | None:
    """The variable's own attribute, else the one from its declared type, else ``None``."""
    own = getattr(variable, attribute)
    if own is not None:
        return str(own)
    declared = variable.declaredType
    inherited = None if declared is None else getattr(declared, attribute, None)
    return None if inherited is None else str(inherited)


def _variable_info(variable: Any) -> VariableInfo:
    """Convert one FMPy ``ModelVariable`` to the compact :class:`VariableInfo`."""
    description = variable.description
    return VariableInfo(
        name=variable.name,
        causality=variable.causality,
        variability=variable.variability,
        type=variable.type,
        unit=_declared_attribute(variable, "unit"),
        start=None if variable.start is None else str(variable.start),
        min=_declared_attribute(variable, "min"),
        max=_declared_attribute(variable, "max"),
        description=truncate(description, VARIABLE_DESCRIPTION_CHARS) if description else None,
    )


def _default_experiment(md: ModelDescription) -> DefaultExperiment | None:
    """The FMU's ``DefaultExperiment`` as floats, or ``None`` if the element is absent."""
    if md.defaultExperiment is None:
        return None
    return DefaultExperiment(
        start_time=_default_value(md, "startTime"),
        stop_time=_default_value(md, "stopTime"),
        step_size=_default_value(md, "stepSize"),
        tolerance=_default_value(md, "tolerance"),
    )


def _parameter_rank(variable: Any) -> int:
    """Sort key that puts tunable parameters before fixed ones."""
    return {"tunable": 0, "fixed": 1}.get(variable.variability, 2)


def inspect_fmu(config: Config, fmu: str) -> FmuInfo:
    """The ``fmu_inspect`` result for a workspace FMU, within ``Limits.inspect_max_items``.

    Raises:
        UserError: from :func:`agentic_fmi.fmu.catalog.resolve_fmu` or :func:`read_description`.
    """
    path = resolve_fmu(config, fmu)
    md = read_description(path)
    variables = list(md.modelVariables)
    cap = config.limits.inspect_max_items
    groups = {
        "inputs": [v for v in variables if v.causality == "input"],
        "outputs": [v for v in variables if v.causality == "output"],
        "parameters": sorted(
            (v for v in variables if v.causality in ("parameter", "structuralParameter")),
            key=_parameter_rank,
        ),
    }
    counts = dict(Counter(variable.causality for variable in variables))
    counts["total"] = len(variables)
    description = md.description
    return FmuInfo(
        path=relative_path(config, path),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        fmi_version=md.fmiVersion,
        model_name=md.modelName,
        description=truncate(description, config.limits.description_max_chars)
        if description
        else None,
        generation_tool=md.generationTool,
        interfaces=interfaces_of(md),
        default_experiment=_default_experiment(md),
        runnability=check_runnability(path, md, config.limits),
        counts=counts,
        inputs=[_variable_info(v) for v in groups["inputs"][:cap]],
        outputs=[_variable_info(v) for v in groups["outputs"][:cap]],
        parameters=[_variable_info(v) for v in groups["parameters"][:cap]],
        truncated=any(len(group) > cap for group in groups.values()),
    )


def _check_choice(label: str, value: str | None, allowed: tuple[str, ...]) -> None:
    """Raise :class:`UserError` if a filter value isn't one of ``allowed``."""
    if value is not None and value not in allowed:
        raise UserError(f"Unknown {label} {value!r}. Use one of: {', '.join(allowed)}.")


def list_variables(
    config: Config,
    fmu: str,
    *,
    causality: str | None = None,
    variability: str | None = None,
    name: str | None = None,
    type: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> VariablePage:
    """Filter and page the FMU's variables.

    ``name`` is a case-insensitive substring or ``fnmatch`` glob. ``type`` is a case-insensitive
    FMPy type name (``Real``, ``Float64``, ``Integer``, ``Boolean``, ...). ``limit`` defaults to
    ``Limits.page_default`` and is capped at ``Limits.page_max``. ``total`` counts all matches.

    Raises:
        UserError: unknown FMU, an unknown causality or variability, or an offset past the end
            (says what ``total`` is).
    """
    _check_choice("causality", causality, get_args(Causality))
    _check_choice("variability", variability, get_args(Variability))
    path = resolve_fmu(config, fmu)
    md = read_description(path)
    wanted_type = None if type is None else type.lower()
    selected = [
        variable
        for variable in md.modelVariables
        if (causality is None or variable.causality == causality)
        and (variability is None or variable.variability == variability)
        and (wanted_type is None or variable.type.lower() == wanted_type)
        and (not name or matches(variable.name, name))
    ]
    start, size = page_window(config, len(selected), offset, limit, "matching variables")
    page = selected[start : start + size]
    end = start + len(page)
    return VariablePage(
        fmu=relative_path(config, path),
        total=len(selected),
        offset=start,
        limit=size,
        items=[_variable_info(variable) for variable in page],
        next_offset=end if end < len(selected) else None,
    )
