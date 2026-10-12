"""The server side of live runs in ``control.py`` (ADR 0010), on fabricated runs and a stand-in
worker: the refusals, the command a request becomes, how long the wait lasts, how outcomes are
reported, and ``run_status(values)``.

Collaborators: ``agentic_fmi.control`` (under test), ``agentic_fmi.runs`` (``view_of``),
``agentic_fmi.store.inbox`` and ``runstore`` (the command and outcome files, ``live.json``), the
shared ``workspace`` and ``fmu`` fixtures. No worker process runs: ``_Status`` stands in for the
supervisor (it reads ``status.json`` as written), and :func:`answer` plays the worker for one
command (its outcome, then the status change the heartbeat would publish). Model descriptions
come from the Reference FMUs' XML; no binary is loaded.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agentic_fmi import budget, control
from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.inspect import read_description, sha256_file
from agentic_fmi.store import inbox, runstore
from agentic_fmi.store.protocol import (
    Command,
    CommandOutcome,
    Constant,
    Experiment,
    FmuRef,
    LiveColumn,
    LiveSnapshot,
    RunError,
    RunSpec,
    RunState,
    RunStatus,
    Versions,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor

FmuFactory = Callable[[str, str], Path]
MakeRun = Callable[..., Path]

H = 0.01
"""The communication step of the fabricated runs."""


class _Status:
    """Stands in for ``Supervisor``: ``reconcile`` returns ``status.json`` as written."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def reconcile(self, run_id: str) -> RunStatus:
        return runstore.read_status(runstore.run_dir(self.config.store_dir, run_id))


@pytest.fixture
def config(workspace: Path) -> Config:
    return replace(load_config(workspace), timing=Timing(poll_interval_s=0.01, max_wait_s=5.0))


@pytest.fixture
def supervisor(config: Config) -> Supervisor:
    return cast(Supervisor, _Status(config))


def write_status(run_path: Path, **changes: Any) -> RunStatus:
    """Update ``status.json`` the way the worker's heartbeat would."""
    current = runstore.read_status(run_path)
    updated = current.model_copy(update={"updated_at": utc_now(), **changes})
    runstore.write_status(run_path, updated)
    return updated


@pytest.fixture
def make_run(config: Config, fmu: FmuFactory) -> MakeRun:
    """``make_run(fmu_name="BouncingBall", state=PAUSED, live=True, inputs=None, ...)``
    fabricates a run of a Reference FMU (3.0, Co-Simulation) in the store and returns its
    directory."""

    def build(
        name: str = "BouncingBall",
        *,
        state: RunState = RunState.PAUSED,
        live: bool = True,
        inputs: dict[str, Any] | None = None,
        outputs: tuple[str, ...] = ("h", "v", "h_ft"),
        t: float = 0.0,
        **status: Any,
    ) -> Path:
        path = fmu("3.0", name)
        run_id, run_path = runstore.claim_run_id(config.store_dir)
        spec = RunSpec(
            run_id=run_id,
            created_at=utc_now(),
            fmu=FmuRef(
                path=str(path),
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
                fmi_version="3.0",
                model_name=name,
            ),
            fmi_type="CoSimulation",
            experiment=Experiment(start_time=0.0, stop_time=3.0, output_interval=H),
            inputs=inputs or {},
            outputs=list(outputs),
            timeout_s=60.0,
            versions=Versions.current(),
            live=live,
            idle_limit_s=60.0 if live else None,
        )
        runstore.write_spec(run_path, spec)
        runstore.write_status(
            run_path,
            RunStatus(
                run_id=run_id,
                state=state,
                t=t,
                t_start=0.0,
                t_stop=3.0,
                updated_at=utc_now(),
                speed=1.0 if live else None,
                **status,
            ),
        )
        return run_path

    return build


def commands_of(run_path: Path) -> list[Command]:
    """The command files in the run's inbox, in name order."""
    directory = run_path / inbox.COMMANDS_DIR
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    return [Command.model_validate_json(file.read_text()) for file in files]


def answer(
    run_path: Path,
    *,
    delay_s: float = 0.0,
    verdict: str = "applied",
    message: str | None = None,
    t: float = 0.5,
    then: Callable[[Command], None] | None = None,
    then_delay_s: float = 0.0,
) -> threading.Thread:
    """Play the worker for the next command: after ``delay_s`` write its outcome, then after
    ``then_delay_s`` call ``then(command)`` (the status change the heartbeat would publish)."""

    def work() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = commands_of(run_path)
            if found:
                break
            time.sleep(0.002)
        else:
            return
        command = found[0]
        time.sleep(delay_s)
        outcome = CommandOutcome(
            run_id=run_path.name,
            id=command.id,
            source="server",
            action=command.action,
            status=verdict,  # type: ignore[arg-type]
            t=t,
            at=utc_now(),
            message=message,
        )
        inbox.write_outcome(run_path, outcome)
        time.sleep(then_delay_s)
        if then is not None:
            then(command)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def control_view(
    config: Config, supervisor: Supervisor, run_path: Path, action: str, **options: Any
) -> Any:
    options.setdefault("wait_s", 0)
    return control.control_run(config, supervisor, run_path.name, action, **options)  # type: ignore[arg-type]


# --- refusals ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("live", "state", "expected"),
    [
        (False, RunState.RUNNING, "batch run, which takes no commands.*run_start.*live=true"),
        (True, RunState.FINISHED, "finished and takes no more commands.*run_results"),
        (True, RunState.STOPPED, "stopped and takes no more commands.*run_results"),
        (True, RunState.FAILED, "failed and takes no more commands.*run_status"),
    ],
)
def test_a_run_that_cannot_take_commands_is_refused_with_the_next_step(
    config: Config,
    supervisor: Supervisor,
    make_run: MakeRun,
    live: bool,
    state: RunState,
    expected: str,
) -> None:
    run_path = make_run(live=live, state=state)
    with pytest.raises(UserError, match=expected):
        control_view(config, supervisor, run_path, "pause")
    assert commands_of(run_path) == []  # nothing was written


def test_the_idle_limit_reason_is_part_of_the_refusal(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(state=RunState.STOPPED, reason="Paused for more than 60 s")
    with pytest.raises(UserError, match=r"stopped \(Paused for more than 60 s\) and takes no more"):
        control_view(config, supervisor, run_path, "resume")


def test_an_unknown_run_is_refused(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    make_run()
    with pytest.raises(UserError, match="There is no run r99"):
        control.control_run(config, supervisor, "r99", "pause")
    with pytest.raises(UserError, match="There is no run r99"):
        control.run_values(config, supervisor, "r99", ["h"])


@pytest.mark.parametrize(
    ("action", "arguments", "expected"),
    [
        ("pause", {"n": 3}, "pause takes no arguments; remove n"),
        ("stop", {"speed": 2.0}, "stop takes no arguments; remove speed"),
        ("resume", {"values": {"e": 0.6}}, "resume takes only an optional speed; remove values"),
        ("set", {"values": {"e": 0.6}, "dt": 1.0}, "set takes only values; remove dt"),
        ("step", {"speed": 1.0, "values": {"e": 1}}, "step takes n or dt; remove values, speed"),
    ],
)
def test_a_stray_argument_is_refused(
    config: Config,
    supervisor: Supervisor,
    make_run: MakeRun,
    action: str,
    arguments: dict[str, Any],
    expected: str,
) -> None:
    run_path = make_run()
    with pytest.raises(UserError, match=expected):
        control_view(config, supervisor, run_path, action, **arguments)
    assert commands_of(run_path) == []


def test_chart_is_a_pane_action_and_never_reaches_the_worker(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    with pytest.raises(UserError, match=r"chart is a pane action.*run_status\('r1', values="):
        control_view(config, supervisor, run_path, "chart")
    assert commands_of(run_path) == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RunState.RUNNING, "needs a paused run, but r1 is running.*action='pause'"),
        (RunState.QUEUED, "r1 is still queued.*paused"),
    ],
)
def test_a_step_needs_a_paused_run(
    config: Config, supervisor: Supervisor, make_run: MakeRun, state: RunState, expected: str
) -> None:
    run_path = make_run(state=state)
    with pytest.raises(UserError, match=expected):
        control_view(config, supervisor, run_path, "step", n=2)
    assert commands_of(run_path) == []


def test_n_and_dt_together_are_refused(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    with pytest.raises(UserError, match=r"n .* or dt .*, not both"):
        control_view(config, supervisor, run_path, "step", n=2, dt=0.5)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [({"dt": 0.0}, "dt must be a positive"), ({"dt": float("nan")}, "dt must be a positive")],
)
def test_a_bad_dt_is_refused(
    config: Config,
    supervisor: Supervisor,
    make_run: MakeRun,
    arguments: dict[str, float],
    expected: str,
) -> None:
    with pytest.raises(UserError, match=expected):
        control_view(config, supervisor, make_run(), "step", **arguments)


@pytest.mark.parametrize("speed", [0.0, -1.0, 1000.5, float("inf"), float("nan")])
def test_a_speed_outside_the_bounds_is_refused(
    config: Config, supervisor: Supervisor, make_run: MakeRun, speed: float
) -> None:
    run_path = make_run()
    with pytest.raises(UserError, match="speed must be above 0 and at most 1000 "):
        control_view(config, supervisor, run_path, "resume", speed=speed)
    assert commands_of(run_path) == []


@pytest.mark.parametrize(
    ("name", "values", "expected"),
    [
        ("BouncingBall", {"h": 1.0}, "h is an output, which the FMU computes"),
        ("BouncingBall", {"hh": 1.0}, "Unknown variable 'hh'. Close matches: h"),
        ("BouncingBall", {"g": 9.0}, "fixed parameter"),
        ("BouncingBall", {"e": 0.1}, "outside its declared range"),
        ("BouncingBall", {"e": True}, "needs a number"),
        ("Feedthrough", {"Boolean_input": 1}, "needs true or false"),
        ("Feedthrough", {"Int32_input": 2.5}, "needs a whole number"),
        ("Feedthrough", {"String_input": "x"}, "String variable"),
    ],
)
def test_a_set_that_check_sets_refuses_never_reaches_the_worker(
    config: Config,
    supervisor: Supervisor,
    make_run: MakeRun,
    name: str,
    values: dict[str, Any],
    expected: str,
) -> None:
    run_path = make_run(name)
    with pytest.raises(UserError, match=expected):
        control_view(config, supervisor, run_path, "set", values=values)
    assert commands_of(run_path) == []


def test_an_input_the_run_drives_cannot_be_set(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    name = "Float64_continuous_input"
    run_path = make_run("Feedthrough", inputs={name: Constant(value=1.0)})
    with pytest.raises(UserError, match="driven by the run's inputs table"):
        control_view(config, supervisor, run_path, "set", values={name: 2.0})


def test_a_set_without_values_is_refused(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    with pytest.raises(UserError, match="set needs values"):
        control_view(config, supervisor, make_run(), "set")


# --- the command a request becomes ----------------------------------------------------------


@pytest.mark.parametrize(
    ("dt", "steps"),
    [(0.025, 3), (0.05, 5), (0.3, 30), (0.01, 1), (0.001, 1), (0.0100000000001, 1), (1.0, 100)],
)
def test_dt_becomes_n_equals_ceil_of_dt_over_h(
    config: Config, supervisor: Supervisor, make_run: MakeRun, dt: float, steps: int
) -> None:
    run_path = make_run()
    view = control_view(config, supervisor, run_path, "step", dt=dt)
    (command,) = commands_of(run_path)
    assert (command.action, command.n, command.source) == ("step", steps, "server")
    assert command.values is None and command.speed is None
    assert view.hint is not None and f"{steps} step(s) of {H:g} s" in view.hint


def test_a_step_without_n_or_dt_is_one_step(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    control_view(config, supervisor, run_path, "step")
    assert [c.n for c in commands_of(run_path)] == [1]


def test_set_sends_the_values_converted_to_the_variable_types(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run("Feedthrough")
    values = {"Int32_input": 3.0, "Boolean_input": True, "Float64_continuous_input": 2}
    control_view(config, supervisor, run_path, "set", values=values)
    (command,) = commands_of(run_path)
    assert command.values == {
        "Int32_input": 3,
        "Boolean_input": True,
        "Float64_continuous_input": 2.0,
    }
    assert isinstance(command.values["Int32_input"], int)
    assert isinstance(command.values["Float64_continuous_input"], float)


def test_resume_carries_its_speed_and_pause_and_stop_nothing(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    control_view(config, supervisor, run_path, "resume", speed=1000.0)
    control_view(config, supervisor, run_path, "resume")
    control_view(config, supervisor, run_path, "pause")
    control_view(config, supervisor, run_path, "stop")
    assert [(c.action, c.speed) for c in commands_of(run_path)] == [
        ("resume", 1000.0),
        ("resume", None),
        ("pause", None),
        ("stop", None),
    ]


def test_a_command_is_a_valid_protocol_file_named_for_the_server(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    control_view(config, supervisor, run_path, "pause")
    (file,) = (run_path / inbox.COMMANDS_DIR).iterdir()
    command = inbox.parse_command(file.name, file.read_bytes())
    assert command.id.split("-")[1] == "server" and file.name == f"{command.id}.json"


def test_read_outcome_refuses_an_id_that_is_no_command_id(make_run: MakeRun) -> None:
    run_path = make_run()
    for bad in ("../status", "", "r1", "123-server-xyz"):
        with pytest.raises(ValueError, match="not a command ID"):
            inbox.read_outcome(run_path, bad)


# --- waiting and outcomes -------------------------------------------------------------------


def test_the_wait_ends_at_wait_s_when_the_worker_does_not_answer(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    started = time.monotonic()
    view = control_view(config, supervisor, run_path, "pause", wait_s=0.4)
    elapsed = time.monotonic() - started
    (command,) = commands_of(run_path)
    assert 0.4 <= elapsed < 1.5
    assert view.hint is not None
    assert f"queued as {command.id}" in view.hint and "run_status('r1')" in view.hint


def test_wait_s_zero_checks_once_and_returns(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    started = time.monotonic()
    view = control_view(config, supervisor, make_run(), "pause", wait_s=0)
    assert time.monotonic() - started < 0.5 and view.state is RunState.PAUSED


def test_wait_s_is_capped_at_max_wait_s(make_run: MakeRun, config: Config) -> None:
    capped = replace(config, timing=replace(config.timing, max_wait_s=0.3))
    run_path = make_run()
    started = time.monotonic()
    control.control_run(
        capped, cast(Supervisor, _Status(capped)), run_path.name, "pause", wait_s=60
    )
    assert 0.3 <= time.monotonic() - started < 1.5


def test_the_default_wait_is_the_control_default(config: Config, make_run: MakeRun) -> None:
    short = replace(config, timing=replace(config.timing, control_default_wait_s=0.3))
    run_path = make_run()
    started = time.monotonic()
    control.control_run(short, cast(Supervisor, _Status(short)), run_path.name, "pause")
    assert 0.3 <= time.monotonic() - started < 1.5


def test_a_run_that_ends_during_the_wait_ends_it_with_a_probably_not_applied_hint(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(state=RunState.RUNNING)
    error = RunError(code="worker_lost", message="gone")  # type: ignore[arg-type]
    timer = threading.Timer(
        0.2, write_status, args=(run_path,), kwargs={"state": RunState.FAILED, "error": error}
    )
    timer.start()
    started = time.monotonic()
    view = control_view(config, supervisor, run_path, "pause", wait_s=5)
    timer.join()
    assert time.monotonic() - started < 2.0
    assert view.state is RunState.FAILED
    assert view.hint is not None and "probably not applied" in view.hint


def test_an_applied_outcome_is_reported_with_the_workers_note_verbatim(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    answer(
        run_path,
        t=0.2,
        message="already paused",
        then=lambda c: write_status(run_path, last_cmd=c.id),
    )
    view = control_view(config, supervisor, run_path, "pause", wait_s=5)
    assert view.state is RunState.PAUSED
    assert view.hint is not None
    assert view.hint.startswith("Applied pause at t=0.2 (already paused). Paused at t=0")
    assert "run_control('r1', action='resume')" in view.hint


@pytest.mark.parametrize(
    "message",
    [
        "already running; speed set to 5",
        "3 of 5 steps before a resume ended them",
        "3 of 5 steps before a pause ended them",
        "3 of 5 steps before a stop ended them",
        "3 of 5 steps before the run ended",
    ],
)
def test_the_workers_other_notes_come_through_unchanged(
    config: Config, supervisor: Supervisor, make_run: MakeRun, message: str
) -> None:
    run_path = make_run()
    answer(run_path, message=message, then=lambda c: write_status(run_path, last_cmd=c.id))
    view = control_view(config, supervisor, run_path, "resume", wait_s=5)
    assert view.hint is not None and f"({message})." in view.hint


def test_the_wait_lasts_until_the_status_shows_the_command(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    """The heartbeat publishes state after the outcome: the reply waits for ``last_cmd``."""
    run_path = make_run()
    answer(
        run_path,
        verdict="applied",
        then_delay_s=0.3,
        then=lambda c: write_status(run_path, last_cmd=c.id, state=RunState.RUNNING, speed=50.0),
    )
    started = time.monotonic()
    view = control_view(config, supervisor, run_path, "resume", speed=50.0, wait_s=5)
    assert 0.3 <= time.monotonic() - started < 2.0
    assert view.state is RunState.RUNNING and view.speed == 50.0
    assert view.hint is not None and view.hint.startswith("Applied resume at t=0.5. Poll with")
    assert "Steer it with run_control('r1', action='pause')" in view.hint


def test_the_reply_is_not_held_past_wait_s_by_a_status_that_lags(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    answer(run_path)  # an outcome, but the status never shows it
    started = time.monotonic()
    view = control_view(config, supervisor, run_path, "pause", wait_s=0.5)
    assert 0.5 <= time.monotonic() - started < 1.5
    assert view.hint is not None and view.hint.startswith("Applied pause at t=0.5.")


def test_stop_waits_for_the_run_to_end(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    answer(
        run_path,
        then_delay_s=0.3,
        then=lambda c: write_status(
            run_path, state=RunState.STOPPED, last_cmd=c.id, finished_at=utc_now()
        ),
    )
    view = control_view(config, supervisor, run_path, "stop", wait_s=5)
    assert view.state is RunState.STOPPED
    assert view.hint is not None and view.hint.startswith("Applied stop at t=0.5.")
    assert "Stopped without a result" in view.hint  # no summary was fabricated


def test_a_rejected_command_raises_the_workers_message(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    answer(run_path, verdict="rejected", t=0.2, message="step needs a paused run; pause it first")
    with pytest.raises(UserError) as caught:
        control_view(config, supervisor, run_path, "resume", wait_s=5)
    assert str(caught.value) == (
        "The worker rejected resume at t=0.2: step needs a paused run; pause it first."
    )


def test_a_command_that_met_the_end_of_the_run_says_so_with_the_next_step(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(state=RunState.RUNNING)
    message = "the run ended at t=3 before this command was applied"
    answer(
        run_path,
        verdict="rejected",
        t=3.0,
        message=message,
        then=lambda c: write_status(run_path, state=RunState.FINISHED),
    )
    with pytest.raises(UserError) as caught:
        control_view(config, supervisor, run_path, "pause", wait_s=5)
    text = str(caught.value)
    assert message in text and "The run is finished" in text and "run_results('r1')" in text


# --- run_values -----------------------------------------------------------------------------


UNITS = {"h": "m", "v": "m/s", "h_ft": "m"}


def write_snapshot(run_path: Path, t: float = 0.2, **values: float | None) -> None:
    """Fabricate ``live.json`` with these column values (units from :data:`UNITS`)."""
    columns = [LiveColumn(name=n, unit=UNITS.get(n), value=v) for n, v in values.items()]
    snapshot = LiveSnapshot(
        run_id=run_path.name, seq=1, t=t, values=columns, time=[0.0, t], series={"h": [1.0, 0.9]}
    )
    runstore.write_live(run_path, snapshot)


def values_of(config: Config, supervisor: Supervisor, run_path: Path, *names: str) -> Any:
    return control.run_values(config, supervisor, run_path.name, list(names))


def test_values_come_from_live_json_with_units_and_the_time_in_the_hint(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(state=RunState.RUNNING, t=0.25)
    write_snapshot(run_path, t=0.2, h=0.98, v=-1.9, h_ft=0.97)
    view = values_of(config, supervisor, run_path, "v", "h")
    assert view.values == [
        LiveColumn(name="v", unit="m/s", value=-1.9),
        LiveColumn(name="h", unit="m", value=0.98),
    ]
    assert view.hint is not None and view.hint.startswith("Values at t=0.2. Poll with run_status")
    assert view.state is RunState.RUNNING and view.t == 0.25


def test_a_finished_live_run_still_answers_from_the_last_snapshot(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(state=RunState.FINISHED, t=3.0)
    write_snapshot(run_path, t=3.0, h=0.0, v=0.0, h_ft=0.0)
    view = values_of(config, supervisor, run_path, "h")
    assert view.values is not None and view.values[0].value == 0.0


def test_a_batch_run_has_no_values(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(live=False, state=RunState.FINISHED)
    with pytest.raises(UserError, match=r"only available for live runs.*run_results"):
        values_of(config, supervisor, run_path, "h")


def test_a_live_run_before_its_first_sample_says_to_retry(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    with pytest.raises(UserError, match=r"no values yet.*again in a second"):
        values_of(config, supervisor, make_run(), "h")


def test_at_most_twenty_five_names_are_accepted(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    write_snapshot(run_path, h=1.0, v=0.0, h_ft=0.0)
    with pytest.raises(UserError, match=r"Name 1 to 25 recorded outputs in values \(got 26\)"):
        values_of(config, supervisor, run_path, *[f"x{i}" for i in range(26)])
    with pytest.raises(UserError, match=r"Name 1 to 25 recorded outputs in values \(got 0\)"):
        values_of(config, supervisor, run_path)


def test_an_unrecorded_name_is_refused_with_close_matches_and_the_available_ones(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run()
    write_snapshot(run_path, h=1.0, v=0.0, h_ft=0.0)
    with pytest.raises(UserError) as caught:
        values_of(config, supervisor, run_path, "hh")
    text = str(caught.value)
    assert text.startswith("'hh' is not a recorded output of r1. Close matches: h")
    assert "recorded outputs: h, v, h_ft." in text and "outputs=[...]" in text


def test_a_recorded_output_that_live_json_does_not_carry_is_named_as_such(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(outputs=("h", "v", "h_ft", "der(h)"))
    write_snapshot(run_path, h=1.0, v=0.0, h_ft=0.0)
    with pytest.raises(UserError, match=r"live.json doesn't carry 'der\(h\)'"):
        values_of(config, supervisor, run_path, "h", "der(h)")


def test_the_view_with_values_stays_within_the_json_budget(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    names = tuple(f"y{i}" for i in range(25))
    run_path = make_run(outputs=names, state=RunState.RUNNING)
    write_snapshot(run_path, **{name: float(i) for i, name in enumerate(names)})
    view = values_of(config, supervisor, run_path, *names)
    assert view.values is not None and len(view.values) == 25
    assert budget.json_size(view) <= config.limits.json_budget_bytes


def test_unit_free_columns_and_missing_values_pass_through(
    config: Config, supervisor: Supervisor, make_run: MakeRun
) -> None:
    run_path = make_run(outputs=("h", "k"))
    write_snapshot(run_path, h=None, k=2.0)
    view = values_of(config, supervisor, run_path, "h", "k")
    assert view.values == [
        LiveColumn(name="h", unit="m", value=None),
        LiveColumn(name="k", unit=None, value=2.0),
    ]
    assert read_description(Path(runstore.read_spec(run_path).fmu.path)).modelName == "BouncingBall"
