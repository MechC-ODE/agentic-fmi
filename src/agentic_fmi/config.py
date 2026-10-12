"""The single source of configuration for Agentic FMI: workspace, run store, limits, timings.

Collaborators: ``cli.py`` calls :func:`load_config` once for ``afmi mcp --workspace``. The MCP
server passes the resulting :class:`Config` to its tools through the server lifespan, never
through module globals. The worker uses :class:`Timing` and :class:`Limits` defaults, because
the per-run settings it needs (``timeout_s``, a live run's ``idle_limit_s``) are recorded in
``spec.json``. The only environment variable read is ``AFMI_STORE_DIR``
([ADR 0007](../../docs/adr/0007-run-store-location-ids-and-supervision.md)); the pane resolves
the same variable the same way (ADR 0010), and ``scripts/pane_fixtures.py`` turns
:func:`load_config` cases into the pane's discovery fixtures so the two can't drift.
It also holds the pinned OMSimulator identity (:data:`OMSIMULATOR_PIN`), which ``oms_pin.py``
checks on the server and in the worker, and the system and SSP limits (ADR 0009).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

STORE_DIR_ENV = "AFMI_STORE_DIR"
"""Environment variable that relocates the run store, e.g. off the Windows bind mount."""

STORE_DIR_NAME = ".agentic-fmi"
"""Default run-store directory name inside the workspace."""

GENERATED_DIR_NAME = "generated"
"""Directory inside the workspace where ``fmu_build`` writes ``<name>/<name>.fmu`` (ADR 0008)."""

IMPORTED_DIR_NAME = "imported"
"""Directory inside the workspace where ``system_import`` writes ``<name>/<name>.json`` and the
FMUs it took out of the SSP (ADR 0009)."""


@dataclass(frozen=True)
class OmsPin:
    """The OMSimulator package the worker may load: its distribution version and the sha256 of
    its package tree as computed by :func:`agentic_fmi.oms_pin.package_digest` (ADR 0009). It
    detects a changed install; it does not prove where the bytes came from."""

    version: str
    sha256: str


OMSIMULATOR_PIN = OmsPin(
    version="3.0.0.post198",
    sha256="660de5354584aa509806736d6e6f292429f5ebb19282308caaa998e79407fdd5",
)
"""The one pinned OMSimulator identity. The tree digest covers ``libOMSimulator.so`` (sha256
``818b054f…02b0fb``), the Python sources and the bundled XSDs of the Ubuntu 24.04 build that
the sdist downloads; a reinstall that fetches other bytes (another distribution's tarball, a
replaced nightly) no longer matches, and system runs are refused until this constant is
reviewed and updated."""

SYSTEM_DEFAULT_STEP_S = 1e-3
"""Communication step written by ``system_import`` when an SSP states none (OMSimulator's own
``oms_ma`` default), with a warning to choose one consciously. A system file must always state
its step; this default is never applied silently at run time."""

SYSTEM_DEFAULT_STOP_S = 1.0
"""Horizon ``system_import`` writes (start time plus this) when an SSP's ``DefaultExperiment``
states no stop time, with a warning; ``run_start`` can override it."""


@dataclass(frozen=True)
class Limits:
    """Context-economy budgets for tool output (principle 2). Tools shrink output to fit."""

    json_budget_bytes: int = 8_192
    """Default size budget for one tool's JSON text."""
    json_hard_cap_bytes: int = 16_384
    """Absolute cap; reachable only when the caller explicitly asks for more points or items."""
    results_default_points: int = 100
    """Points per variable that ``run_results`` returns by default (LTTB downsampling)."""
    results_max_points: int = 500
    """Largest ``max_points`` that ``run_results`` accepts."""
    results_max_variables: int = 8
    """Variables per ``run_results`` call."""
    significant_digits: int = 6
    """Significant digits for every float a tool returns."""
    page_default: int = 50
    """Default page size for paged lists (``fmu_list``, ``fmu_variables``, ``run_list``)."""
    page_max: int = 200
    """Largest page size a caller may request."""
    inspect_max_items: int = 25
    """Items per list inside ``fmu_inspect`` and ``run_status`` summaries."""
    log_tail_bytes: int = 2_048
    """Bytes of ``log.txt`` attached to a failed run's error."""
    message_max_chars: int = 500
    """Characters of an error message or event message."""
    plot_max_bytes: int = 200_000
    """Size cap for one PNG returned by ``run_plot``."""
    plot_max_runs: int = 6
    """Runs per ``run_plot`` or ``run_compare`` call."""
    plot_max_variables: int = 6
    """Variables (subplots) per ``run_plot`` call."""
    description_max_chars: int = 2_048
    """Length cap for each MCP tool description."""
    max_internal_steps: float = 1e8
    """Step-count estimate above which ``run_start`` refuses a run unless ``force`` is set."""
    build_source_max_bytes: int = 262_144
    """Largest source file ``fmu_build`` accepts (256 KiB)."""
    build_diagnostics_max_chars: int = 2_000
    """Length cap of a failed build's diagnostics text (the ``ToolError`` or ``error.message``)."""
    build_diagnostics_max_lines: int = 10
    """``path:line[:col]: severity: message`` lines in a failed build's diagnostics."""
    build_max_warnings: int = 5
    """Warnings shown in a build result (``build.json`` keeps up to the diagnostics line cap)."""
    build_result_max_bytes: int = 2_048
    """JSON budget of a finished build's view (``fmu_build``, ``run_status``)."""
    system_file_max_bytes: int = 262_144
    """Largest system file (JSON) or parameter set (``.ssv``) read from the workspace (256 KiB)."""
    system_max_components: int = 64
    """Components per system."""
    system_max_connections: int = 1_024
    """Connections per system."""
    system_max_cells: float = 5e6
    """Rows times columns (``time`` included) of a system run's result, above which ``run_start``
    refuses unless ``force`` is set. A system run records one row per communication step, as
    float64 in memory (about 24 bytes a cell at peak) and as ``%.17g`` text in ``result.csv``
    (about 20 bytes a cell), so the cap keeps both near 100 MB."""
    ssp_max_bytes: int = 2 * 1024**3
    """Largest ``.ssp`` archive ``system_import`` opens (on-disk size)."""
    ssp_max_members: int = 1_000
    """Entries in an SSP's zip directory; more is refused before anything is read."""
    ssp_xml_max_bytes: int = 8 * 1024**2
    """Decompressed size cap of one SSD or SSV member, counted while streaming."""
    ssp_member_max_bytes: int = 1024**3
    """Decompressed size cap of one embedded FMU, counted while streaming."""
    ssp_total_max_bytes: int = 2 * 1024**3
    """Decompressed bytes ``system_import`` reads from one SSP in total."""
    live_points: int = 500
    """History points per recorded column the worker keeps in memory, and publish in
    ``live.json`` for the charted ones: the last communication points of a live run (ADR 0010)."""
    live_variables: int = 3
    """Charted variables in ``live.json``: the default chart selection (the first recorded scalar
    outputs) and the most a ``chart`` command may name."""
    live_max_columns: int = 200
    """Recorded scalar outputs whose current value ``live.json`` carries (``run_status(values)``
    and the pane read them); outputs past this are not in it."""
    live_max_bytes: int = 100_000
    """Size cap of ``live.json``; the writer drops the oldest history points until it fits."""
    live_values_max: int = 25
    """Names per ``run_status(values=[...])``."""
    live_set_max_values: int = 25
    """Variables per ``set`` command."""
    stall_repeats: int = 100
    """Consecutive gate calls at the same simulation time that count as a stalled step (an FMU
    that discards a step without terminating; FMPy retries it forever). The engine then fails
    the run with ``simulation_error``. Event iterations repeat a time only a few times."""


@dataclass(frozen=True)
class Timing:
    """Supervision timings shared by the server and the workers (ADR 0002, ADR 0007)."""

    heartbeat_interval_s: float = 0.5
    """How often the worker's heartbeat thread rewrites ``status.json``."""
    heartbeat_stale_s: float = 5.0
    """A running worker whose heartbeat is older than this is killed and marked failed."""
    spawn_timeout_s: float = 15.0
    """A run still ``queued`` this long after creation is treated as a worker that never started."""
    stop_grace_s: float = 2.0
    """Time between SIGTERM and SIGKILL in ``run_stop``."""
    kill_confirm_s: float = 2.0
    """How long the server waits for a killed worker to disappear before giving up."""
    default_timeout_s: float = 600.0
    """Run timeout (non-paused wall time) when ``run_start`` gets no ``timeout_s``."""
    max_timeout_s: float = 86_400.0
    """Largest ``timeout_s`` a caller may request."""
    default_wait_s: float = 10.0
    """How long ``run_start`` waits for the run to finish before returning its status."""
    max_wait_s: float = 60.0
    """Largest ``wait_s`` a caller may request."""
    poll_interval_s: float = 0.1
    """Polling period while ``run_start`` waits or ``run_stop`` waits for the worker."""
    build_timeout_s: float = 1_800.0
    """Wall-time limit of a build job; the worker's watchdog then kills its process group,
    the build tool included (ADR 0008). Not a tool parameter yet."""
    build_default_wait_s: float = 45.0
    """How long ``fmu_build`` waits for the build by default: below the ~60 s at which some
    clients cancel a stdio tool call. Capped at :attr:`max_wait_s` like ``run_start``."""
    live_wait_slice_s: float = 0.05
    """Longest single sleep of a live run's gate while it waits (paused, pacing, between single
    steps), so a command, a stop or SIGTERM is seen within it (ADR 0010)."""
    inbox_poll_s: float = 0.1
    """How often a live run that is stepping lists its ``commands/`` inbox (a paused one lists
    it every wait slice). Batch runs never poll."""
    inbox_grace_s: float = 2.0
    """How long the worker retries a command file that doesn't parse yet (the pane's writes are
    not atomic) before it rejects it."""
    live_idle_limit_s: float = 1_800.0
    """Seconds a live run may stay paused before its worker stops it (``stopped`` with the
    partial result and a reason), so an orphaned run frees its ``max_workers`` slot. Recorded
    in the spec as ``idle_limit_s``."""
    live_default_speed: float = 1.0
    """Pacing factor of a live run until a ``resume`` sets another: simulated seconds per wall
    second. Batch runs are never paced."""
    live_max_speed: float = 1_000.0
    """Largest pacing factor ``resume`` accepts (effectively full speed for most models)."""
    control_default_wait_s: float = 5.0
    """How long ``run_control`` waits for the command's outcome by default (capped at
    :attr:`max_wait_s`)."""


@dataclass(frozen=True)
class Config:
    """Immutable configuration for one MCP server process. Build it with :func:`load_config`."""

    workspace: Path
    """Absolute, resolved workspace root (the Claude Code project directory)."""
    store_dir: Path
    """Absolute run-store directory: ``$AFMI_STORE_DIR`` or ``<workspace>/.agentic-fmi``."""
    allowed_roots: tuple[Path, ...]
    """Resolved directories FMUs and build sources may be loaded from: the workspace only."""
    generated_dir: Path
    """Absolute ``<workspace>/generated``: build outputs; never a build source."""
    imported_dir: Path
    """Absolute ``<workspace>/imported``: ``system_import`` outputs, one new directory each."""
    max_workers: int = 4
    """Maximum number of runs in state ``queued`` or ``running`` at once."""
    limits: Limits = field(default_factory=Limits)
    timing: Timing = field(default_factory=Timing)


def load_config(workspace: Path, env: Mapping[str, str] | None = None) -> Config:
    """Build the :class:`Config` for ``workspace``.

    ``env`` defaults to ``os.environ`` and exists so tests can pass their own mapping. A relative
    ``AFMI_STORE_DIR`` is resolved against the workspace. Nothing is created on disk here; the
    run store creates its directories on first use.

    Raises:
        ValueError: ``workspace`` does not exist or is not a directory.
    """
    environ = os.environ if env is None else env
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace {root} is not a directory; pass an existing --workspace")
    override = environ.get(STORE_DIR_ENV, "").strip()
    store_dir = (
        (root / Path(override).expanduser()).resolve() if override else root / STORE_DIR_NAME
    )
    return Config(
        workspace=root,
        store_dir=store_dir,
        allowed_roots=(root,),
        generated_dir=root / GENERATED_DIR_NAME,
        imported_dir=root / IMPORTED_DIR_NAME,
    )
