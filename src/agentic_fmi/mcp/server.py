"""The MCP server ``fmi``: eighteen thin tool adapters over the core functions.

Each tool reads the :class:`AppState` from the lifespan, calls one core function, and returns
either compact JSON (``Annotated[CallToolResult, Model]``: minified text plus the same data as
structured content, validated against ``Model``'s published output schema) or, for
``run_plot``, an image. ``fmu_build`` and ``run_control`` return the same minified JSON and
structured content (a ``RunView``) without a published output schema: ``RunView``'s schema
already appears in three tools. ``tools/list`` must stay under 32 KB (ADR 0009 raised it from
26 KB for the dedicated system tools ``system_check``, ``system_import`` and
``system_export``; ADR 0010 fitted ``run_control``, ``run_start(live)`` and
``run_status(values)`` by trimming descriptions: 31.1 KB). ``run_control`` and
``run_status(values)`` call ``control.py`` (live runs, ADR 0010).

Response styles: ``response_style`` stores "ninja" (the default) or "talkative" in the run
store's ``preferences.json``. In ninja mode the adapters that return a ``RunView`` (``run_start``,
``run_status``, ``run_stop``, ``run_control``, ``fmu_build``) pass it through ``style.ninja_view``
(see :func:`_styled`): no summary, build facts or log tail, and a one-line status as the hint.
Refusals stay ``ToolError`` with their full text, and the result tools are unchanged.

:class:`agentic_fmi.errors.UserError` becomes ``ToolError``, which the
model sees as an ``is_error`` result reading ``Error executing tool <name>: <message>``;
:class:`agentic_fmi.errors.ProtocolError` and ``OSError`` become a short ``ToolError`` with a
hint. Any other exception reaches the model only as ``Error executing tool <name>``; its
traceback stays in the server log (stderr).

Output budget (principle 2): the core functions and ``budget.py`` cut a result to fit
``Limits.json_budget_bytes`` (the hard cap ``json_hard_cap_bytes`` when the caller asked for more
than the default page size) and say so through ``next_offset`` or ``truncated``; the adapters
here only refuse a result over the hard cap. Published schemas drop pydantic's ``title`` noise,
which is most of the ``tools/list`` payload.

Facts verified against mcp 2.3.0 (recorded in ADR 0001): sync tools run on worker threads; the
stdio transport points fd 1 at stderr while serving, so stray prints and child output can't
corrupt the channel, but anything printed *before* ``MCPServer.run`` can. The pyproject pins
``mcp<2.4`` because :func:`_lean_tool` patches ``Tool.parameters`` and
``fn_metadata.output_schema``, which later minor versions may change.

Never loads FMU binaries: nothing here imports ``engines/`` or ``worker`` (ruff TID251 also
bans FMPy's loading APIs, ``pythonfmu`` and ``OMSimulator``). Never runs a build tool either:
``fmu_build`` validates and creates a job whose worker runs it (``runs.start_build``). System
tools parse SSD, SSV and ``modelDescription.xml`` only; systems run in workers.

Collaborators: ``config.Config``, ``supervisor.Supervisor``, ``runs.py``, ``budget.py``,
``fmu/catalog.py``, ``fmu/inspect.py``, ``fmu/validate.py``, ``store/runstore.py``,
``system.py`` (``system_check``), ``ssp.py`` (``system_import``, ``system_export``),
``style.py`` (``ninja_view``, ``confirmation``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Final, Literal, TypeVar, cast

import anyio.to_thread
import fmpy
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from agentic_fmi import __version__, budget, control, runs, ssp
from agentic_fmi.analysis.compare import Comparison
from agentic_fmi.config import Config, Timing
from agentic_fmi.errors import ProtocolError, UserError
from agentic_fmi.fmu import catalog
from agentic_fmi.fmu.catalog import FmuPage
from agentic_fmi.fmu.inspect import FmuInfo, VariablePage, inspect_fmu, list_variables
from agentic_fmi.fmu.validate import ValidationReport, validate_fmu
from agentic_fmi.runs import RunPage, RunResults, RunView
from agentic_fmi.ssp import SystemExport, SystemImport
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    BUILD_NAME_PATTERN,
    InputSignal,
    Preferences,
    ResponseStyle,
    RunState,
    StartValue,
    Versions,
)
from agentic_fmi.style import confirmation, ninja_view
from agentic_fmi.supervisor import Supervisor
from agentic_fmi.system import SystemCheck, check_system

T = TypeVar("T")

SERVER_NAME = "fmi"
INSTRUCTIONS = (
    "Explore and simulate FMUs (FMI 2.0 and 3.0) in this workspace. Start with fmu_list or "
    "fmu_inspect; run with run_start, then read run_status, run_results, run_compare and "
    "run_plot. Build FMUs from .py or .mo files with fmu_build. Connect FMUs in a system "
    "file (JSON), check it with system_check and run it with run_start(system=...); "
    "system_import and system_export convert SSP files. Tools return compact summaries and "
    "plots, never raw time series. Reply style: ninja by default (reply to run, build and "
    "live-control results with their 🥷 line only); call response_style when the user asks "
    "for talkative (full summaries) or ninja mode. Never download or install anything on your "
    "own; for an example, build a ready-made one (see the fmi-expert skill)."
)

PROBLEM_MAX_CHARS = 300
"""Longest store or file-system error text passed on to the model."""


@dataclass(frozen=True)
class AppState:
    """What the lifespan yields to every tool: the one Config and this server's Supervisor."""

    config: Config
    supervisor: Supervisor


class WorkspaceInfo(BaseModel):
    """The ``workspace_info`` result."""

    workspace: str
    store_dir: str
    platform: str
    versions: dict[str, str]
    fmu_count: int
    active_runs: list[str]
    max_workers: int
    limits: dict[str, int | float]
    """The budgets a caller can steer: points, pages, plot runs and variables, step guard."""


class StyleResult(BaseModel):
    """The ``response_style`` result: the style in force and the line that confirms it."""

    style: ResponseStyle
    line: str


FmuArg = Annotated[
    str, Field(description="FMU path relative to the workspace, as listed by fmu_list.")
]
RunIdArg = Annotated[str, Field(description="Run ID such as 'r3'.", pattern=r"^r[1-9]\d*$")]
OffsetArg = Annotated[int, Field(ge=0, description="Index of the first item to return.")]
LimitArg = Annotated[int, Field(ge=1, le=200, description="Page size (max 200).")]

TIMING: Final = Timing()
"""The default timings. ``run_start`` and ``fmu_build`` publish their ``wait_s`` bound and
default (and ``run_start`` its ``timeout_s`` default) from here, so ``config.py`` stays the one
place that sets them. A constant of the frozen dataclass, read only when the tools are
defined; the running server uses ``AppState.config``."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES_STORE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
BUILDS = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
"""``fmu_build``: runs the source's code in a worker and, with ``overwrite``, replaces files."""
WRITES_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
"""``system_import`` and ``system_export``: write new workspace files only, never replace."""


def _state(ctx: Context) -> AppState:
    """The lifespan state of the current request."""
    return cast(AppState, ctx.request_context.lifespan_context)


def _json(model: BaseModel, hard_cap: int) -> CallToolResult:
    """Minified JSON text plus structured content (both without nulls).

    Raises:
        ToolError: the text exceeds ``hard_cap`` bytes, which is a bug in the core function's
            budgeting; the message asks the caller to narrow the request.
    """
    data = budget.compact_data(model)
    text = budget.compact_json(data)
    size = len(text.encode("utf-8"))
    if size > hard_cap:
        raise ToolError(
            f"The result is {size} bytes, over the {hard_cap} byte output cap. Narrow the "
            "request (a smaller limit, fewer variables or runs) and call again."
        )
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def _reply(state: AppState, model: BaseModel) -> CallToolResult:
    """:func:`_json` with the configured hard cap."""
    return _json(model, state.config.limits.json_hard_cap_bytes)


def _styled(state: AppState, view: RunView) -> RunView:
    """``view`` in the stored response style: its ninja view, or unchanged in talkative mode.
    Read on each call; a missing or unreadable ``preferences.json`` means ninja (the default)."""
    if runstore.read_preferences(state.config.store_dir).response_style == "ninja":
        return ninja_view(view)
    return view


def _call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a core function, translating its errors into ``ToolError`` with a next step."""
    try:
        return fn(*args, **kwargs)
    except UserError as exc:
        raise ToolError(str(exc)) from exc
    except ProtocolError as exc:
        raise ToolError(
            f"Run store problem: {_clip(str(exc))} A file in the store is damaged or was "
            "written by another Agentic FMI version. Call run_list to see the runs that still "
            "read fine; start new runs for the others."
        ) from exc
    except OSError as exc:
        raise ToolError(
            f"File system problem: {_clip(exc.strerror or str(exc))}. Check that the workspace "
            "and its .agentic-fmi store are writable and have free space, then retry."
        ) from exc


def _clip(text: str) -> str:
    return text if len(text) <= PROBLEM_MAX_CHARS else text[: PROBLEM_MAX_CHARS - 3] + "..."


def _mcp_version() -> str:
    try:
        return version("mcp")
    except PackageNotFoundError:
        return "unknown"


def _workspace_info(state: AppState) -> WorkspaceInfo:
    config, limits = state.config, state.config.limits
    steerable = {
        "results_default_points": limits.results_default_points,
        "results_max_points": limits.results_max_points,
        "results_max_variables": limits.results_max_variables,
        "page_default": limits.page_default,
        "page_max": limits.page_max,
        "plot_max_runs": limits.plot_max_runs,
        "plot_max_variables": limits.plot_max_variables,
        "max_internal_steps": limits.max_internal_steps,
        "default_timeout_s": config.timing.default_timeout_s,
        "default_wait_s": config.timing.default_wait_s,
        "max_wait_s": config.timing.max_wait_s,
    }
    return WorkspaceInfo(
        workspace=str(config.workspace),
        store_dir=str(config.store_dir),
        platform=fmpy.platform,
        versions={**Versions.current().model_dump(), "mcp": _mcp_version()},
        fmu_count=len(catalog.find_fmus(config)),
        active_runs=state.supervisor.peek_active_run_ids(),
        max_workers=config.max_workers,
        limits=steerable,
    )


def workspace_info(ctx: Context) -> Annotated[CallToolResult, WorkspaceInfo]:
    """Show the workspace, run store, platform, tool versions, FMU count, active runs and output
    limits. Call this first if unsure where FMUs are or why a run was refused."""
    state = _state(ctx)
    return _reply(state, _call(_workspace_info, state))


def fmu_list(
    ctx: Context,
    pattern: Annotated[
        str | None, Field(description="Case-insensitive substring or glob on the path.")
    ] = None,
    offset: OffsetArg = 0,
    limit: LimitArg = 50,
) -> Annotated[CallToolResult, FmuPage]:
    """List FMUs in the workspace (paged, sorted by path) with FMI version, interfaces and
    whether they can run here. Pass an entry's path as the fmu argument of other tools. A page
    cut to fit the output budget continues at next_offset."""
    state = _state(ctx)
    page = _call(catalog.list_fmus, state.config, pattern, offset, limit)
    return _reply(state, budget.fit_page(page, state.config.limits, limit))


def fmu_inspect(ctx: Context, fmu: FmuArg) -> Annotated[CallToolResult, FmuInfo]:
    """Describe an FMU from its modelDescription.xml: FMI version, interfaces (CS, ME, SE),
    platforms and runnability with reasons, default experiment, variable counts, and up to 25
    inputs, outputs and parameters (tunable first). Use fmu_variables for the full list."""
    state = _state(ctx)
    info = _call(inspect_fmu, state.config, fmu)
    return _reply(state, budget.fit_info(info, state.config.limits))


def fmu_variables(
    ctx: Context,
    fmu: FmuArg,
    causality: Annotated[
        Literal["parameter", "calculatedParameter", "input", "output", "local", "independent"]
        | None,
        Field(description="Only variables with this causality."),
    ] = None,
    variability: Annotated[
        Literal["constant", "fixed", "tunable", "discrete", "continuous"] | None,
        Field(description="Only variables with this variability."),
    ] = None,
    name: Annotated[
        str | None, Field(description="Case-insensitive substring or glob on the name.")
    ] = None,
    type: Annotated[
        str | None,
        Field(description="Only variables of this type, e.g. Real, Float64, Integer, Boolean."),
    ] = None,
    offset: OffsetArg = 0,
    limit: LimitArg = 50,
) -> Annotated[CallToolResult, VariablePage]:
    """List an FMU's variables with causality, variability, type, unit, start, min and max,
    filtered and paged; total is the number of matches. A page cut to fit the output budget
    continues at next_offset."""
    state = _state(ctx)
    page = _call(
        list_variables,
        state.config,
        fmu,
        causality=causality,
        variability=variability,
        name=name,
        type=type,
        offset=offset,
        limit=limit,
    )
    return _reply(state, budget.fit_page(page, state.config.limits, limit))


def fmu_validate(ctx: Context, fmu: FmuArg) -> Annotated[CallToolResult, ValidationReport]:
    """Check an FMU's modelDescription.xml and archive against the FMI standard with FMPy
    (schema, names, model structure, start values, units). Problems are listed, not raised."""
    state = _state(ctx)
    report = _call(validate_fmu, state.config, fmu)
    return _reply(state, budget.fit_report(report, state.config.limits))


def run_start(
    ctx: Context,
    fmu: Annotated[
        str | None,
        Field(description="FMU path relative to the workspace, as listed by fmu_list."),
    ] = None,
    system: Annotated[
        str | None,
        Field(description="Instead of fmu: a system file (JSON), run by OMSimulator."),
    ] = None,
    paramset: Annotated[
        str | None,
        Field(description="Systems: an .ssv parameter set, applied over the system file."),
    ] = None,
    start_values: Annotated[
        dict[str, StartValue] | None,
        Field(description="Values by variable name, e.g. {'e': 0.5}; systems: {'PI.Kp': 12}."),
    ] = None,
    stop_time: Annotated[
        float | None, Field(description="Default: the FMU's default experiment.")
    ] = None,
    start_time: Annotated[float | None, Field(description="Default: the FMU's.")] = None,
    output_interval: Annotated[
        float | None,
        Field(gt=0, description="Default: FMPy's, at most ~1000 rows. Systems: the step h."),
    ] = None,
    relative_tolerance: Annotated[float | None, Field(gt=0)] = None,
    inputs: Annotated[
        dict[str, InputSignal] | None,
        Field(
            description="Input signals by input name: constant, step, ramp or table, e.g. "
            "{'kind':'step','time':1,'before':0,'after':2}."
        ),
    ] = None,
    outputs: Annotated[
        list[str] | None, Field(description="Variables to record. Default: the outputs.")
    ] = None,
    fmi_type: Annotated[
        Literal["CoSimulation", "ModelExchange"] | None,
        Field(description="Default: CoSimulation if available."),
    ] = None,
    solver: Annotated[
        Literal["CVode", "Euler"] | None, Field(description="Model Exchange solver; CVode.")
    ] = None,
    timeout_s: Annotated[
        float | None,
        Field(gt=0, description=f"Wall-time limit. Default {TIMING.default_timeout_s:g} s."),
    ] = None,
    wait_s: Annotated[
        float,
        Field(ge=0, le=TIMING.max_wait_s, description="Seconds to wait for completion."),
    ] = TIMING.default_wait_s,
    force: Annotated[
        bool, Field(description="Run even if the step-count estimate is huge.")
    ] = False,
    live: Annotated[
        bool, Field(description="Start paused at t0, paced, for run_control. Co-Simulation.")
    ] = False,
) -> Annotated[CallToolResult, RunView]:
    """Simulate an FMU, or a system file with OMSimulator, in an isolated worker. Returns the
    summary if it finishes within wait_s, else the run ID and progress (poll with run_status).
    Every run records its spec, the FMUs' sha256 and tool versions. Refuses runs that can't
    work here, naming the fix."""
    state = _state(ctx)
    view = _call(
        runs.start_run,
        state.config,
        state.supervisor,
        fmu,
        system=system,
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
    return _reply(state, _styled(state, view))


def run_status(
    ctx: Context,
    run_id: RunIdArg,
    values: Annotated[
        list[str] | None,
        Field(max_length=25, description="Live runs: current values of these outputs."),
    ] = None,
) -> Annotated[CallToolResult, RunView]:
    """State, progress and simulation time of a run; its summary (min, max, final per variable)
    when finished or stopped, or its error with a log tail when failed."""
    state = _state(ctx)
    app = (state.config, state.supervisor, run_id)
    view = _call(control.run_values, *app, values) if values else _call(runs.get_run, *app)
    return _reply(state, _styled(state, view))


def run_stop(ctx: Context, run_id: RunIdArg) -> Annotated[CallToolResult, RunView]:
    """Stop a queued or running run (SIGTERM, then SIGKILL after a grace period). The partial
    result is kept when the worker stops cleanly."""
    state = _state(ctx)
    view = _call(runs.stop_run, state.config, state.supervisor, run_id)
    return _reply(state, _styled(state, view))


def run_control(
    ctx: Context,
    run_id: RunIdArg,
    action: Literal["pause", "resume", "step", "set", "stop"],
    n: Annotated[int | None, Field(ge=1, description="step: steps (default 1).")] = None,
    dt: Annotated[float | None, Field(gt=0, description="step: at least dt seconds.")] = None,
    values: Annotated[
        dict[str, bool | int | float] | None,
        Field(description="set: e.g. {'e': 0.6}; systems {'PI.Kp': 12}."),
    ] = None,
    speed: Annotated[
        float | None,
        Field(gt=0, le=TIMING.live_max_speed, description="resume: 1 = real time."),
    ] = None,
    wait_s: Annotated[
        float, Field(ge=0, le=TIMING.max_wait_s, description="Seconds to wait for the outcome.")
    ] = TIMING.control_default_wait_s,
) -> CallToolResult:
    """Control a live run (run_start(live=true)): pause; resume, optionally at a new speed;
    step a paused run; set inputs and tunable parameters (from the next step); stop. Returns
    the run after the command's outcome; read values with run_status(values=[...])."""
    state = _state(ctx)
    view = _call(
        control.control_run,
        state.config,
        state.supervisor,
        run_id,
        action,
        n=n,
        dt=dt,
        values=values,
        speed=speed,
        wait_s=wait_s,
    )
    return _reply(state, _styled(state, view))


def run_list(
    ctx: Context,
    state: Annotated[RunState | None, Field(description="Only runs in this state.")] = None,
    offset: OffsetArg = 0,
    limit: LimitArg = 50,
) -> Annotated[CallToolResult, RunPage]:
    """List runs in this workspace, newest first, with state and FMU. A page cut to fit the
    output budget continues at next_offset."""
    app = _state(ctx)
    page = _call(
        runs.list_runs, app.config, app.supervisor, state=state, offset=offset, limit=limit
    )
    return _reply(app, page)


def run_results(
    ctx: Context,
    run_id: RunIdArg,
    variables: Annotated[
        list[str] | None, Field(max_length=8, description="Up to 8 variables.")
    ] = None,
    max_points: Annotated[
        int, Field(ge=4, le=500, description="Points per variable after LTTB downsampling.")
    ] = 100,
    start_time: Annotated[float | None, Field(description="Window start.")] = None,
    stop_time: Annotated[float | None, Field(description="Window end.")] = None,
) -> Annotated[CallToolResult, RunResults]:
    """Downsampled time series (LTTB, endpoints and extrema kept, 6 significant digits) of a
    finished or stopped run. Prefer run_plot to look at shapes and run_compare for differences."""
    state = _state(ctx)
    results = _call(
        runs.get_results,
        state.config,
        run_id,
        variables=variables,
        max_points=max_points,
        start_time=start_time,
        stop_time=stop_time,
    )
    return _reply(state, results)


def run_plot(
    ctx: Context,
    run_ids: Annotated[list[str], Field(min_length=1, max_length=6, description="1 to 6 runs.")],
    variables: Annotated[
        list[str] | None, Field(max_length=6, description="Up to 6 variables, one panel each.")
    ] = None,
    title: Annotated[str | None, Field(max_length=120)] = None,
) -> list[Image | TextContent]:
    """Plot variables of one or more runs over time as a PNG (one panel per variable, one line
    per run). Also saved under the first run's plots/ directory."""
    state = _state(ctx)
    plot = _call(runs.plot_runs, state.config, run_ids, variables, title)
    return [Image(data=plot.png, format="png"), TextContent(type="text", text=plot.caption)]


def fmu_build(
    ctx: Context,
    source: Annotated[
        str,
        Field(
            description="Workspace-relative .py (one pythonfmu Fmi2Slave class) or .mo (a "
            "Modelica class named like the file) file."
        ),
    ],
    name: Annotated[
        str | None,
        Field(
            pattern=BUILD_NAME_PATTERN,
            description="Output generated/<name>/<name>.fmu. Default: the file name.",
        ),
    ] = None,
    overwrite: Annotated[
        bool, Field(description="Replace an earlier fmu_build output of this name.")
    ] = False,
    wait_s: Annotated[
        float,
        Field(
            ge=0,
            le=TIMING.max_wait_s,
            description="Seconds to wait before returning the run ID.",
        ),
    ] = TIMING.build_default_wait_s,
) -> CallToolResult:
    """Build an FMI 2.0 FMU from one workspace file: .py via pythonfmu (Co-Simulation) or .mo
    via OpenModelica (ModelExchange and CoSimulation). Runs as a job like run_start: finished
    within wait_s gives the FMU's path, sha256 and interfaces; compiler errors come back as
    path:line: messages to fix. Else poll run_status; run_stop cancels. Never edits the source."""
    state = _state(ctx)
    view = _call(
        runs.start_build,
        state.config,
        state.supervisor,
        source,
        name=name,
        overwrite=overwrite,
        wait_s=wait_s,
    )
    return _reply(state, _styled(state, view))


def run_compare(
    ctx: Context,
    run_ids: Annotated[
        list[str], Field(min_length=2, max_length=6, description="2 to 6 runs; first is baseline.")
    ],
    variables: Annotated[list[str] | None, Field(max_length=6)] = None,
) -> Annotated[CallToolResult, Comparison]:
    """Compare runs: spec differences (start values, inputs, experiment, FMU) and, per variable,
    min, max, final and max/RMS difference against the first run, across different time grids."""
    state = _state(ctx)
    return _reply(state, _call(runs.compare_runs, state.config, run_ids, variables))


def response_style(
    ctx: Context,
    style: Annotated[
        ResponseStyle | None, Field(description="Default: report the current style.")
    ] = None,
) -> Annotated[CallToolResult, StyleResult]:
    """Set the reply style: ninja (the default: run, build and live-control results come as one
    🥷 status line, which you reply with) or talkative (full summaries). Kept per workspace."""
    state = _state(ctx)
    store_dir = state.config.store_dir
    if style is None:
        style = runstore.read_preferences(store_dir).response_style
    else:
        _call(runstore.write_preferences, store_dir, Preferences(response_style=style))
    return _reply(state, StyleResult(style=style, line=confirmation(style)))


SystemArg = Annotated[str, Field(description="System file (JSON) relative to the workspace.")]


def system_check(ctx: Context, system: SystemArg) -> Annotated[CallToolResult, SystemCheck]:
    """Check a system file without running it: FMU paths, component and variable names,
    output-to-input causality, types, units, inputs with several or no drivers, parameters and
    the experiment (step_size required). Lists errors and warnings, each with its fix. Format:
    {"version":1,"components":{"PI":{"fmu":"a.fmu","parameters":{"Kp":10}}},
    "connections":[{"from":"PI.u","to":"P.F"}],"experiment":{"stop_time":10,"step_size":0.01}}."""
    state = _state(ctx)
    return _reply(state, _call(check_system, state.config, system))


def system_import(
    ctx: Context,
    source: Annotated[str, Field(description="Workspace-relative .ssp file.")],
    name: Annotated[
        str | None,
        Field(pattern=BUILD_NAME_PATTERN, description="Output imported/<name>/. Default: stem."),
    ] = None,
) -> Annotated[CallToolResult, SystemImport]:
    """Import a flat SSP from any tool as imported/<name>/<name>.json plus private FMU copies;
    its SSV parameter values become component parameters. Nested systems, parameter mappings
    and signal dictionaries are refused by name. Never modifies the .ssp."""
    state = _state(ctx)
    return _reply(state, _call(ssp.import_ssp, state.config, source, name))


def system_export(
    ctx: Context,
    system: SystemArg,
    dest: Annotated[str, Field(description="New workspace-relative .ssp path.")],
    paramset: Annotated[
        str | None, Field(description="An .ssv applied over the system file's values.")
    ] = None,
) -> Annotated[CallToolResult, SystemExport]:
    """Export a system file as a standard .ssp (SSD, FMUs, one SSV, the step) that other tools
    and OMSimulator open; repeated exports are byte-identical. Never overwrites."""
    state = _state(ctx)
    return _reply(state, _call(ssp.export_ssp, state.config, system, dest, paramset))


TOOLS: tuple[tuple[Callable[..., Any], ToolAnnotations], ...] = (
    (workspace_info, READ_ONLY),
    (fmu_list, READ_ONLY),
    (fmu_inspect, READ_ONLY),
    (fmu_variables, READ_ONLY),
    (fmu_validate, READ_ONLY),
    (run_start, WRITES_STORE),
    (run_status, WRITES_STORE),
    (run_stop, WRITES_STORE),
    (run_control, WRITES_STORE),
    (run_list, WRITES_STORE),
    (run_results, READ_ONLY),
    (run_plot, WRITES_STORE),
    (run_compare, READ_ONLY),
    (fmu_build, BUILDS),
    (system_check, READ_ONLY),
    (system_import, WRITES_FILES),
    (system_export, WRITES_FILES),
    (response_style, WRITES_STORE),
)
"""Tool functions in listing order. Tool and parameter names are a public interface: later
phases may only add optional parameters."""


def _collapse_null(node: dict[str, Any]) -> dict[str, Any]:
    """``{"anyOf": [X, {"type": "null"}], ...}`` as ``{**X, ...}``: results never carry nulls."""
    options = node.get("anyOf")
    null = {"type": "null"}
    if not isinstance(options, list) or len(options) != 2 or null not in options:
        return node
    other = options[0] if options[1] == null else options[1]
    return {**other, **{key: value for key, value in node.items() if key != "anyOf"}}


def _lean(node: Any, output: bool) -> Any:
    """A JSON schema without pydantic's ``title`` annotations and ``default: null`` entries
    (property names are kept). For a result schema also without defaults and, for object
    properties, null alternatives, because results are published without nulls and defaults."""
    if isinstance(node, list):
        return [_lean(item, output) for item in node]
    if not isinstance(node, dict):
        return node
    lean: dict[str, Any] = {}
    for key, value in node.items():
        if (key == "title" and isinstance(value, str)) or (
            key == "default" and (output or value is None)
        ):
            continue
        if key == "properties":
            lean[key] = {n: _lean(_collapse(s, output), output) for n, s in value.items()}
        elif key == "$defs":
            lean[key] = {n: _lean(s, output) for n, s in value.items()}
        else:
            lean[key] = _lean(value, output)
    return lean


def _collapse(prop: dict[str, Any], output: bool) -> dict[str, Any]:
    """:func:`_collapse_null` for a result schema; input schemas keep their null alternatives."""
    return _collapse_null(prop) if output else prop


def _lean_result_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """:func:`_lean` for a result schema, also without the class docstrings pydantic copies in
    as descriptions and without the field descriptions of nested models (``$defs``: protocol
    models such as ``RunError`` document the store files, not the tool's result). Results
    publish no field descriptions; the tool description and the result itself explain them
    (ADR 0010 trimmed these to fit ``run_control`` under the 32 KB ``tools/list`` cap)."""
    lean: dict[str, Any] = _lean(schema, output=True)
    lean.pop("description", None)
    for definition in lean.get("$defs", {}).values():
        definition.pop("description", None)
        for prop in definition.get("properties", {}).values():
            if isinstance(prop, dict):
                prop.pop("description", None)
    return lean


def _lean_tool(tool: Tool) -> Tool:
    """Shrink the schemas a tool publishes. Arguments and results are still validated by the
    pydantic models they came from."""
    tool.parameters = _lean(tool.parameters, output=False)
    metadata = tool.fn_metadata
    if metadata.output_schema is not None:
        metadata.output_schema = _lean_result_schema(metadata.output_schema)
    return tool


def build_server(config: Config) -> MCPServer[AppState]:
    """Create the MCP server with its lifespan and the eighteen tools.

    The lifespan creates the run store, builds the :class:`Supervisor`, reconciles runs left
    over from earlier servers (in a worker thread), and yields the :class:`AppState`. Workers
    outlive the server; nothing is killed on shutdown.
    """

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppState]) -> AsyncIterator[AppState]:
        runstore.ensure_store(config.store_dir)
        supervisor = Supervisor(config)
        await anyio.to_thread.run_sync(supervisor.reconcile_all)
        yield AppState(config=config, supervisor=supervisor)

    tools = [_lean_tool(Tool.from_function(fn, annotations=hints)) for fn, hints in TOOLS]
    return MCPServer(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        tools=tools,
    )


def serve_stdio(config: Config) -> None:
    """Run the server over stdio until the client disconnects. Print nothing to stdout before
    this call: the stdio transport only diverts fd 1 once it is serving."""
    build_server(config).run("stdio")
