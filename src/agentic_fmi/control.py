"""Server-side live control (ADR 0010): the ``run_control`` use case and ``run_status(values)``.

Kept apart from ``runs.py`` (live control lives in its own module).
``runs.start_run(live=True)`` creates the live run; this module talks to it afterwards, only
through the run store: it writes a command with ``store.inbox.write_command`` (exclusive create,
``source="server"``), waits for its outcome (``store.inbox.read_outcome``, O(1) per poll) and
reads ``live.json`` (``runstore.read_live``). It never writes ``status.json`` and never signals
the worker; ``run_stop`` keeps its SIGTERM path.

Rules for :func:`control_run`:

- The run must be a live run (``spec.live``) that is ``queued``, ``running`` or ``paused``
  (reconciled first). A batch run, a build job or a finished run is refused with the next step
  (a batch run: "start it with run_start(live=true)").
- ``step``: only from ``paused`` (else "pause it first"); ``n`` or ``dt`` (not both; default
  ``n = 1``); ``n = ceil(dt / h - 1e-9)`` with ``h`` the communication step
  (``experiment.output_interval`` for one FMU, ``master.step_size`` for a system).
- ``set``: ``values`` required; checked with ``live.check_sets`` against the model
  descriptions the server parses itself (``fmu.inspect.read_description``, XML only), so a bad
  set is a ``UserError`` and never reaches the worker. ``n``, ``dt``, ``speed`` refused.
- ``resume``: optional ``speed`` in ``(0, Timing.live_max_speed]``.
- ``pause`` and ``stop``: no arguments.
- ``chart`` belongs to the pane (``Command.names``, applied by the worker): refused here, before
  anything is read or written. The MCP tool's ``action`` literal doesn't list it either.
- Then wait until the outcome exists, the run is terminal, or ``wait_s`` (default
  ``Timing.control_default_wait_s``, capped at ``Timing.max_wait_s``) has passed, polling
  every ``Timing.poll_interval_s``; never longer. A rejected outcome raises ``UserError`` with
  its message; an applied one returns the view with a hint naming the outcome ("Applied step at
  t=2.5 (...). Paused at t=2.5. ..."). The worker publishes state through its heartbeat, up to a
  heartbeat after the outcome, so an applied command's wait also lasts until ``status.last_cmd``
  has caught up (and, for ``stop``, until the run has ended): the view then shows the effect.
  No outcome in time: the view with "the command is queued as <id>; check run_status".
- A run killed by its timeout, or lost, gets no final inbox sweep (ADR 0010), so a command
  pending then never gets an outcome: the wait also ends when the run is terminal, and the
  hint says the command was probably not applied.

:func:`run_values` answers ``run_status(values=[...])`` from ``live.json`` (recorded scalar
outputs only).

Collaborators: ``runs.py`` (``view_of``, the reconciled ``RunView``; ``wait_seconds``),
``live.py`` (``check_sets``), ``store/inbox.py``, ``store/runstore.py``, ``store/protocol.py``,
``fmu/inspect.py``, ``supervisor.Supervisor`` (reconcile), ``config.Config``. Never imports
``engines/`` or ``worker.py``.
"""

from __future__ import annotations

import difflib
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentic_fmi import runs
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.inspect import ModelDescription, read_description
from agentic_fmi.live import SINGLE_FMU, LiveSpec, check_sets
from agentic_fmi.runs import RunView
from agentic_fmi.store import inbox, runstore
from agentic_fmi.store.protocol import (
    TERMINAL_STATES,
    BuildSpec,
    Command,
    CommandAction,
    CommandOutcome,
    LiveColumn,
    LiveSnapshot,
    RunState,
    RunStatus,
    SetValue,
    SystemSpec,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor

STEP_TOLERANCE = 1e-9
"""Slack in ``n = ceil(dt / h - STEP_TOLERANCE)``, so ``dt = 0.3`` at ``h = 0.1`` is 3 steps."""

MAX_LISTED_NAMES = 12
"""Names listed in a refusal that shows what is available."""

TAKES = {
    "pause": "no arguments",
    "stop": "no arguments",
    "resume": "only an optional speed",
    "step": "n or dt",
    "set": "only values",
}
"""What each action accepts, for the refusal of a stray argument."""


@dataclass(frozen=True)
class _Request:
    """A validated command and a note for the reply ("dt=0.1 is 10 steps of 0.01 s")."""

    command: Command
    note: str = ""


def control_run(
    config: Config,
    supervisor: Supervisor,
    run_id: str,
    action: CommandAction,
    *,
    n: int | None = None,
    dt: float | None = None,
    values: Mapping[str, SetValue] | None = None,
    speed: float | None = None,
    wait_s: float | None = None,
) -> RunView:
    """Send one command to a live run and describe the run after its outcome (module rules).

    Raises:
        UserError: a refused request (not live, wrong state, bad arguments, a set that
            ``live.check_sets`` refuses, the pane-only ``chart``) or a rejected outcome, with
            the fix.
    """
    if action == "chart":
        raise UserError(
            "chart is a pane action (the live pane's chart picker), not a run_control action. "
            "Call run_control with pause, resume, step, set or stop, and read values with "
            f"run_status('{run_id}', values=[...])."
        )
    status = supervisor.reconcile(run_id)
    run_path = runstore.run_dir(config.store_dir, run_id)
    spec = _live_spec(run_path, status)
    wait = runs.wait_seconds(
        config.timing, config.timing.control_default_wait_s if wait_s is None else wait_s
    )
    request = _request(config, spec, status, action, n=n, dt=dt, values=values, speed=speed)
    _send(run_path, request.command)
    outcome, status = _await(config, supervisor, run_path, request.command, wait)
    return _answer(config, request, outcome, status, wait)


def run_values(
    config: Config, supervisor: Supervisor, run_id: str, names: Sequence[str]
) -> RunView:
    """``run_status(run_id, values=names)``: the reconciled view plus ``values``, the named
    outputs' current values with units from ``live.json`` (the sample's ``t`` leads the hint).

    At most ``Limits.live_values_max`` names, each a recorded output of the run (else "add it to
    outputs: start a new run with outputs=[...]", with close matches); a recorded output that
    ``live.json`` doesn't carry (an array, or past ``Limits.live_max_columns``) is named as such.
    A run without ``live.json`` is refused: a batch run points to ``run_results``, a live run
    before its first sample says to retry after a moment.

    Raises:
        UserError: as above.
    """
    status = supervisor.reconcile(run_id)
    run_path = runstore.run_dir(config.store_dir, run_id)
    spec = runstore.read_job_spec(run_path)
    rid = status.run_id
    if isinstance(spec, BuildSpec) or not spec.live:
        raise UserError(
            f"Values are only available for live runs, and {rid} is not one. Start a live run "
            "with run_start(..., live=true); read a finished batch run with run_results or "
            "run_plot."
        )
    wanted = list(dict.fromkeys(names))
    limit = config.limits.live_values_max
    if not wanted or len(wanted) > limit:
        raise UserError(
            f"Name 1 to {limit} recorded outputs in values (got {len(wanted)}); split a longer "
            "list over several calls."
        )
    snapshot = runstore.read_live(run_path)
    if snapshot is None:
        raise UserError(
            f"Run {rid} has no values yet: its worker writes the first sample moments after it "
            f"starts. Call run_status('{rid}', values=[...]) again in a second."
        )
    columns = _columns(config, spec, snapshot, wanted)
    return _noted(runs.view_of(config, status, values=columns), f"Values at t={snapshot.t:g}.")


# --- refusals and the command ---------------------------------------------------------------


def _live_spec(run_path: Path, status: RunStatus) -> LiveSpec:
    """The spec of a run that can take commands.

    Raises:
        UserError: a build job, a batch run, or a run that has ended, each with the next step.
    """
    spec = runstore.read_job_spec(run_path)
    rid = status.run_id
    if isinstance(spec, BuildSpec):
        raise UserError(
            f"{rid} is an fmu_build job, which takes no commands. Cancel it with "
            f"run_stop('{rid}'), or start a live simulation with run_start(fmu=..., live=true)."
        )
    if not spec.live:
        raise UserError(
            f"Run {rid} is a batch run, which takes no commands. Start a live run with "
            f"run_start(..., live=true) to control it; stop a batch run with run_stop('{rid}')."
        )
    if status.state in TERMINAL_STATES:
        why = "" if status.reason is None else f" ({status.reason.rstrip('.')})"
        read = (
            f"Read its result with run_results('{rid}') or run_plot"
            if status.state is not RunState.FAILED
            else f"run_status('{rid}') has the error"
        )
        raise UserError(
            f"Run {rid} is {status.state.value}{why} and takes no more commands. {read}, or "
            "start a new live run with run_start(..., live=true)."
        )
    return spec


def _request(
    config: Config,
    spec: LiveSpec,
    status: RunStatus,
    action: CommandAction,
    *,
    n: int | None,
    dt: float | None,
    values: Mapping[str, SetValue] | None,
    speed: float | None,
) -> _Request:
    """The command for ``action`` with checked arguments, or ``UserError`` with the fix."""
    given = {"n": n, "dt": dt, "values": values, "speed": speed}
    allowed = {"step": ("n", "dt"), "set": ("values",), "resume": ("speed",)}.get(action, ())
    stray = [name for name, value in given.items() if value is not None and name not in allowed]
    if stray:
        raise UserError(
            f"{action} takes {TAKES[action]}; remove {', '.join(stray)} and call run_control again."
        )
    steps: int | None = None
    checked: dict[str, SetValue] | None = None
    pace: float | None = None
    note = ""
    if action == "step":
        _require_paused(status)
        steps, note = _step_count(spec, n, dt)
    elif action == "set":
        checked = _checked_values(config, spec, values)
    elif action == "resume" and speed is not None:
        pace = _checked_speed(config, speed)
    command = Command(
        id=inbox.new_command_id("server"),
        source="server",
        issued_at=utc_now(),
        action=action,
        n=steps,
        values=checked,
        speed=pace,
    )
    return _Request(command, note)


def _require_paused(status: RunStatus) -> None:
    rid = status.run_id
    if status.state is RunState.PAUSED:
        return
    if status.state is RunState.QUEUED:
        raise UserError(
            f"Run {rid} is still queued; a step needs a paused run. Wait until run_status('{rid}') "
            "says paused, then step."
        )
    raise UserError(
        f"A step needs a paused run, but {rid} is {status.state.value}. Pause it first with "
        f"run_control('{rid}', action='pause'), then step."
    )


def _step_count(spec: LiveSpec, n: int | None, dt: float | None) -> tuple[int, str]:
    """Whole communication steps to take, and a note naming the conversion from ``dt``."""
    if n is not None and dt is not None:
        raise UserError("Pass n (communication steps) or dt (seconds), not both.")
    if dt is None:
        steps = 1 if n is None else n
        if steps < 1:
            raise UserError(f"n must be at least 1 (got {steps}).")
        return steps, ""
    if not math.isfinite(dt) or dt <= 0:
        raise UserError(f"dt must be a positive number of seconds (got {dt:g}).")
    h = spec.master.step_size if isinstance(spec, SystemSpec) else spec.experiment.output_interval
    steps = max(1, math.ceil(dt / h - STEP_TOLERANCE))
    return steps, f"dt={dt:g} s is {steps} step(s) of {h:g} s (the communication step)."


def _checked_values(
    config: Config, spec: LiveSpec, values: Mapping[str, SetValue] | None
) -> dict[str, SetValue]:
    """The values of a ``set``, converted to their variables' types, or ``UserError``."""
    if not values:
        raise UserError("set needs values, e.g. values={'e': 0.6} (systems: '<component>.<name>').")
    try:
        return check_sets(spec, _descriptions(spec), values, config.limits)
    except ValueError as error:
        raise UserError(str(error)) from error


def _descriptions(spec: LiveSpec) -> dict[str, ModelDescription]:
    """The model descriptions ``check_sets`` needs, parsed from the FMUs' XML (never loaded)."""
    if isinstance(spec, SystemSpec):
        return {name: read_description(Path(c.fmu.path)) for name, c in spec.components.items()}
    return {SINGLE_FMU: read_description(Path(spec.fmu.path))}


def _checked_speed(config: Config, speed: float) -> float:
    top = config.timing.live_max_speed
    if not math.isfinite(speed) or not 0 < speed <= top:
        raise UserError(
            f"speed must be above 0 and at most {top:g} (1 = real time; got {speed:g}). Pass a "
            "factor in that range."
        )
    return speed


def _send(run_path: Path, command: Command) -> None:
    """Write the command file; a command over the size limit is a ``UserError``."""
    try:
        inbox.write_command(run_path, command)
    except ValueError as error:
        raise UserError(
            f"The command doesn't fit one file ({error}). Set fewer variables per call."
        ) from error


# --- waiting for the outcome ----------------------------------------------------------------


def _await(
    config: Config, supervisor: Supervisor, run_path: Path, command: Command, wait_s: float
) -> tuple[CommandOutcome | None, RunStatus]:
    """Poll until the command is settled (module rules) or ``wait_s`` has passed; return its
    outcome, if any, and the last reconciled status. Status is read before the outcome, so a
    terminal status means any final-sweep outcome is already visible."""
    run_id = run_path.name
    deadline = time.monotonic() + wait_s
    outcome: CommandOutcome | None = None
    while True:
        status = supervisor.reconcile(run_id)
        if outcome is None:
            outcome = inbox.read_outcome(run_path, command.id)
        if _settled(command, outcome, status) or time.monotonic() >= deadline:
            return outcome, status
        time.sleep(min(config.timing.poll_interval_s, max(deadline - time.monotonic(), 0.0)))


def _settled(command: Command, outcome: CommandOutcome | None, status: RunStatus) -> bool:
    """Whether the reply can be built: the run has ended, the command was rejected, or its
    effect shows in the status (``stop``: the run has ended)."""
    if status.state in TERMINAL_STATES:
        return True
    if outcome is None:
        return False
    if outcome.status == "rejected":
        return True
    if command.action == "stop":
        return False
    return status.last_cmd is not None and status.last_cmd >= command.id


def _answer(
    config: Config,
    request: _Request,
    outcome: CommandOutcome | None,
    status: RunStatus,
    wait_s: float,
) -> RunView:
    """The reply: the view with a hint naming the outcome, or the rejection as an error."""
    command, rid = request.command, status.run_id
    if outcome is not None and outcome.status == "rejected":
        raise UserError(_rejection(command, outcome, status))
    view = runs.view_of(config, status)
    if outcome is None and status.state in TERMINAL_STATES:
        hint = (
            f"The run {status.state.value} before the worker answered {command.action} "
            f"{command.id}, so it was probably not applied (a run that hit its timeout or was "
            "lost answers nothing)."
        )
    elif outcome is None:
        hint = (
            f"The worker hasn't answered within {wait_s:g} s: the command is queued as "
            f"{command.id}. Check run_status('{rid}') to see its effect."
        )
    else:
        detail = f" ({outcome.message})" if outcome.message else ""
        hint = f"Applied {command.action} at t={outcome.t:g}{detail}."
    return _noted(view, f"{request.note} {hint}".strip())


def _rejection(command: Command, outcome: CommandOutcome, status: RunStatus) -> str:
    """The error text of a rejected command: the worker's message, and the next step if the run
    has ended meanwhile."""
    message = (outcome.message or "no reason given").rstrip(".")
    text = f"The worker rejected {command.action} at t={outcome.t:g}: {message}."
    if status.state in TERMINAL_STATES:
        text += (
            f" The run is {status.state.value}: read it with run_results('{status.run_id}') or "
            "start a new live run with run_start(..., live=true)."
        )
    return text


def _noted(view: RunView, note: str) -> RunView:
    """``view`` with ``note`` in front of its hint."""
    return view.model_copy(update={"hint": f"{note} {view.hint}" if view.hint else note})


# --- run_status(values) ----------------------------------------------------------------------


def _columns(
    config: Config, spec: LiveSpec, snapshot: LiveSnapshot, wanted: Sequence[str]
) -> list[LiveColumn]:
    """The snapshot's columns for ``wanted``, or ``UserError`` naming what is wrong."""
    carried = {column.name: column for column in snapshot.values}
    missing = [name for name in wanted if name not in carried]
    if missing:
        raise _unavailable(config, spec, snapshot, missing)
    return [carried[name] for name in wanted]


def _unavailable(
    config: Config, spec: LiveSpec, snapshot: LiveSnapshot, missing: Sequence[str]
) -> UserError:
    """Why ``missing`` names have no live value: recorded but not carried, or not recorded."""
    rid = snapshot.run_id
    recorded = set(spec.outputs)
    uncarried = [name for name in missing if name in recorded]
    if uncarried:
        return UserError(
            f"live.json doesn't carry {', '.join(map(repr, uncarried))}: live values cover scalar "
            f"outputs, at most {config.limits.live_max_columns} of them (arrays and the rest are "
            f"in the result: run_results('{rid}') after the run ends)."
        )
    available = [column.name for column in snapshot.values]
    close = difflib.get_close_matches(missing[0], available, n=3)
    hint = f" Close matches: {', '.join(close)}." if close else ""
    listed = ", ".join(available[:MAX_LISTED_NAMES]) + (
        ", ..." if len(available) > MAX_LISTED_NAMES else ""
    )
    names = ", ".join(map(repr, missing))
    verb = "is not a recorded output" if len(missing) == 1 else "are not recorded outputs"
    return UserError(
        f"{names} {verb} of {rid}.{hint} Live values come from the recorded outputs: {listed}. "
        "To watch another variable, start a new run with outputs=[...] naming it."
    )
