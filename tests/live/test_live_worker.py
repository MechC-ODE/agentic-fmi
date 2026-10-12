"""Live runs through real workers (ADR 0010) on BouncingBall Co-Simulation, FMI 2.0 and 3.0:
paused at t0 with ``live.json`` and a beating ``heartbeat_mono``, ``run_stop`` within the grace
while paused, a set of ``e`` after single steps (the apex ratio becomes e²), bad pane sets
rejected while the run goes on, the chart selection (a ``chart`` command changes the series of
``live.json``, with the full history, while the run stays paused), the idle limit, leftovers
rejected at the end, batch runs that never touch the inbox, and the supervisor's monotonic
staleness.

Collaborators: ``agentic_fmi.worker`` (under test, as ``python -m agentic_fmi worker``),
``agentic_fmi.live`` and ``agentic_fmi.store.inbox`` (run by it), ``agentic_fmi.supervisor``
(``run_stop`` and reconciliation), the fixtures of ``tests/live/conftest.py`` and
``live_support`` (commands in the server's and the pane's way).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from live_support import command, command_events, outcome_of, send, send_pane

from agentic_fmi.config import Timing, load_config
from agentic_fmi.store import inbox, runstore
from agentic_fmi.store.protocol import ErrorCode, LiveSnapshot, RunState, RunStatus, utc_now
from agentic_fmi.supervisor import Supervisor, heartbeat_age

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
AwaitStatus = Callable[..., RunStatus]

H = 0.01
VERSIONS = ["2.0", "3.0"]


def _paused(status: RunStatus) -> bool:
    return status.state is RunState.PAUSED


def _ended(status: RunStatus) -> bool:
    return status.state in (RunState.FINISHED, RunState.FAILED, RunState.STOPPED)


def _start(
    fmu: FmuFactory, new_live_run: RunBuilder, spawn: Spawn, version: str, **options: object
) -> tuple[Path, subprocess.Popen[bytes]]:
    run_path = new_live_run(
        fmu(version, "BouncingBall"), stop_time=3.0, output_interval=H, **options
    )
    return run_path, spawn(run_path)


def _live_json(run_path: Path, ready: Callable[[LiveSnapshot], bool]) -> LiveSnapshot:
    """The first ``live.json`` that satisfies ``ready`` (the heartbeat writes it a few times a
    second); fails the test after 10 s."""
    deadline = time.monotonic() + 10.0
    while True:
        snapshot = runstore.read_live(run_path)
        if snapshot is not None and ready(snapshot):
            return snapshot
        assert time.monotonic() < deadline, f"live.json never satisfied the test: {snapshot}"
        time.sleep(0.05)


def _apexes(h: np.ndarray) -> list[float]:
    """Interior local maxima of the height (the apexes after each bounce)."""
    return [float(h[k]) for k in range(1, len(h) - 1) if h[k - 1] < h[k] >= h[k + 1]]


@pytest.mark.parametrize("version", VERSIONS)
def test_a_live_run_waits_paused_at_t0_beating_with_live_json(
    fmu: FmuFactory,
    new_live_run: RunBuilder,
    spawn: Spawn,
    await_status: AwaitStatus,
    version: str,
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, version)
    paused = await_status(run_path, _paused)
    assert (paused.t, paused.speed, paused.progress) == (0.0, 1.0, 0.0)
    assert paused.heartbeat_mono is not None
    assert abs(time.monotonic() - paused.heartbeat_mono) < 2.0  # one machine, one clock
    later = await_status(
        run_path, lambda s: (s.heartbeat_mono or 0) >= (paused.heartbeat_mono or 0) + 1.0
    )
    assert later.state is RunState.PAUSED and later.t == 0.0  # beating, not stepping
    snapshot = runstore.read_live(run_path)
    assert snapshot is not None and snapshot.t == 0.0
    assert [c.name for c in snapshot.values][:2] == ["h", "v"]
    assert snapshot.values[0].value == 1.0 and snapshot.values[0].unit == "m"
    assert snapshot.time == [0.0]
    stop = send(run_path, "stop")
    assert outcome_of(run_path, stop).status == "applied"
    assert child.wait(timeout=20) == 0
    status = runstore.read_status(run_path)
    assert status.state is RunState.STOPPED and status.last_cmd == stop and status.reason is None
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.n_points == 1 and summary.t_end == 0.0


def test_run_stop_ends_a_paused_run_within_the_grace(
    fmu: FmuFactory,
    new_live_run: RunBuilder,
    spawn: Spawn,
    await_status: AwaitStatus,
    workspace: Path,
    store_dir: Path,
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, "2.0")
    await_status(run_path, _paused)
    supervisor = Supervisor(load_config(workspace, env={}))
    started = time.monotonic()
    status = supervisor.stop(run_path.name)
    elapsed = time.monotonic() - started
    assert status.state is RunState.STOPPED and status.error is None
    assert elapsed < Timing().stop_grace_s  # the worker ended it itself, never killed
    assert child.wait(timeout=10) == 0
    summary = runstore.read_summary(run_path)  # only the worker writes a summary
    assert summary is not None and summary.n_points == 1
    stopped = [
        line
        for line in (store_dir / runstore.EVENTS_FILE).read_text().splitlines()
        if '"type":"stopped"' in line and f'"run_id":"{run_path.name}"' in line
    ]
    assert len(stopped) == 1 and '"source":"worker"' in stopped[0]


@pytest.mark.parametrize("version", VERSIONS)
def test_setting_e_after_single_steps_makes_the_apex_ratio_e_squared(
    fmu: FmuFactory,
    new_live_run: RunBuilder,
    new_run: RunBuilder,
    spawn: Spawn,
    run_worker: Callable[[Path], RunStatus],
    await_status: AwaitStatus,
    version: str,
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, version)
    await_status(run_path, _paused)
    step = send(run_path, "step", n=20)
    stepped = outcome_of(run_path, step)
    assert stepped.status == "applied" and stepped.t == pytest.approx(20 * H)
    assert await_status(run_path, lambda s: _paused(s) and s.last_cmd == step).t == pytest.approx(
        20 * H
    )
    set_e = send(run_path, "set", values={"e": 0.6})
    assert outcome_of(run_path, set_e).status == "applied"
    resume = send(run_path, "resume", speed=1000.0)
    assert outcome_of(run_path, resume).t == pytest.approx(20 * H)
    assert child.wait(timeout=30) == 0
    status = runstore.read_status(run_path)
    assert status.state is RunState.FINISHED and status.reason is None and status.speed == 1000.0
    live = runstore.read_result(run_path)
    apexes = _apexes(live["h"])
    assert len(apexes) >= 2
    assert apexes[1] / apexes[0] == pytest.approx(0.36, rel=0.03)
    # e acts only at a bounce, and the first is at t = 0.45: the set at t = 0.2 equals e = 0.6 from
    # the start
    batch = new_run(
        fmu(version, "BouncingBall"), start_values={"e": 0.6}, stop_time=3.0, output_interval=H
    )
    assert run_worker(batch).state is RunState.FINISHED
    reference = runstore.read_result(batch)
    np.testing.assert_allclose(live["h"], reference["h"], rtol=0, atol=1e-12)
    snapshot = runstore.read_live(run_path)
    assert snapshot is not None and snapshot.t == pytest.approx(3.0)
    assert len(snapshot.time) == 301 and snapshot.time[-1] == pytest.approx(3.0)


def test_bad_pane_sets_are_rejected_and_the_run_goes_on(
    fmu: FmuFactory, new_live_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, "2.0")
    await_status(run_path, _paused)
    fixed = send_pane(run_path, "set", values={"g": 1.0})
    out_of_range = send_pane(run_path, "set", values={"e": 2.0})
    refused = outcome_of(run_path, fixed)
    assert (refused.status, refused.source) == ("rejected", "pane")
    assert "fixed parameter" in (refused.message or "")
    assert "outside its declared range" in (outcome_of(run_path, out_of_range).message or "")
    assert runstore.read_status(run_path).state is RunState.PAUSED
    torn = command("pane", "resume", speed=1000.0)
    path = run_path / inbox.COMMANDS_DIR / f"{torn.id}.json"
    data = torn.model_dump_json().encode()
    path.write_bytes(data[:17])  # the pane's write is not atomic: a reader may see a torn file
    time.sleep(0.5)
    path.write_bytes(data)
    assert outcome_of(run_path, torn.id).status == "applied"
    assert child.wait(timeout=30) == 0
    status = runstore.read_status(run_path)
    assert status.state is RunState.FINISHED and status.last_cmd == torn.id
    events = command_events(run_path.parent.parent, run_path.name)
    assert [e["message"].split()[0] for e in events] == [fixed, out_of_range, torn.id]


@pytest.mark.parametrize("version", VERSIONS)
def test_a_chart_command_changes_the_series_of_live_json_with_the_full_history(
    fmu: FmuFactory,
    new_live_run: RunBuilder,
    spawn: Spawn,
    await_status: AwaitStatus,
    version: str,
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, version)
    await_status(run_path, _paused)
    first = _live_json(run_path, lambda s: True)
    assert list(first.series)[:2] == ["h", "v"]  # the default: the first outputs (3.0 has h_ft)
    assert outcome_of(run_path, send(run_path, "step", n=20)).status == "applied"
    await_status(run_path, lambda s: _paused(s) and s.t == pytest.approx(20 * H))
    only_v = send(run_path, "chart", names=["v"])
    assert outcome_of(run_path, only_v).status == "applied"
    snapshot = _live_json(run_path, lambda s: list(s.series) == ["v"])
    assert len(snapshot.time) == len(snapshot.series["v"]) == 21  # t0 and 20 steps
    assert snapshot.series["v"][0] == 0.0 and snapshot.series["v"][-1] < -1.0  # falling
    assert snapshot.t == pytest.approx(20 * H) and runstore.read_status(run_path).last_cmd == only_v
    both = send(run_path, "chart", names=["h", "v"])  # h was not charted, but its history is kept
    assert outcome_of(run_path, both).status == "applied"
    again = _live_json(run_path, lambda s: list(s.series) == ["h", "v"])
    assert again.time == snapshot.time and again.series["v"] == snapshot.series["v"]
    assert again.series["h"][0] == 1.0 and again.series["h"][-1] < 1.0
    nothing = send_pane(run_path, "chart", names=["nope"])
    rejected = outcome_of(run_path, nothing)
    assert rejected.status == "rejected" and "not a recorded output" in (rejected.message or "")
    assert runstore.read_status(run_path).state is RunState.PAUSED  # no chart stops or resumes
    stop = send(run_path, "stop")
    assert outcome_of(run_path, stop).status == "applied"
    assert child.wait(timeout=20) == 0
    assert runstore.read_status(run_path).state is RunState.STOPPED
    final = runstore.read_live(run_path)  # kept after the run ends, with its selection
    assert final is not None and list(final.series) == ["h", "v"]


def test_the_idle_limit_stops_a_paused_run_with_a_reason(
    fmu: FmuFactory, new_live_run: RunBuilder, spawn: Spawn
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, "3.0", idle_limit_s=1.0)
    assert child.wait(timeout=30) == 0
    status = runstore.read_status(run_path)
    assert status.state is RunState.STOPPED and status.error is None
    assert status.reason is not None and "idle limit" in status.reason
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.n_points == 1


def test_commands_left_at_the_end_are_rejected_and_a_late_lower_name_is_applied(
    fmu: FmuFactory, new_live_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path, child = _start(fmu, new_live_run, spawn, "2.0")
    await_status(run_path, _paused)
    garbage = run_path / inbox.COMMANDS_DIR / "9999999999998-pane-0000000a.json"
    garbage.parent.mkdir(exist_ok=True)
    garbage.write_text('{"protocol": 1, "id": "99')  # in its grace for 2 s, holding later names
    pause = command("server", "pause").model_copy(update={"id": "9999999999999-server-0000000b"})
    inbox.write_command(run_path, pause)
    time.sleep(0.3)  # the worker has seen both
    resume = send(run_path, "resume", speed=1000.0)  # a lower name than both, written later
    assert outcome_of(run_path, resume).status == "applied"
    assert child.wait(timeout=30) == 0
    assert runstore.read_status(run_path).state is RunState.FINISHED
    unreadable = outcome_of(run_path, garbage.stem)
    assert unreadable.status == "rejected" and unreadable.action is None
    late = outcome_of(run_path, pause.id)
    assert late.status == "rejected" and "the run ended at t=3" in (late.message or "")
    commands = sorted(p.stem for p in (run_path / inbox.COMMANDS_DIR).glob("*.json"))
    outcomes = sorted(p.stem for p in (run_path / inbox.OUTCOMES_DIR).glob("*.json"))
    assert commands == outcomes and len(commands) == 3
    assert len(command_events(run_path.parent.parent, run_path.name)) == 3


def test_batch_runs_never_read_the_inbox_or_write_live_json(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: Callable[[Path], RunStatus]
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"), stop_time=3.0, output_interval=H)
    send(run_path, "pause")
    status = run_worker(run_path)
    assert status.state is RunState.FINISHED and status.heartbeat_mono is not None
    assert status.speed is None and status.last_cmd is None and status.reason is None
    assert not (run_path / inbox.OUTCOMES_DIR).exists()
    assert not (run_path / runstore.LIVE_FILE).exists()


def test_staleness_prefers_the_monotonic_heartbeat(
    fmu: FmuFactory, new_run: RunBuilder, workspace: Path
) -> None:
    hour_ago = utc_now() - timedelta(hours=1)
    run_path = new_run(fmu("2.0", "BouncingBall"))
    queued = runstore.read_status(run_path)
    fresh_mono = queued.model_copy(
        update={
            "state": RunState.RUNNING,
            "heartbeat_at": hour_ago,
            "updated_at": hour_ago,
            "heartbeat_mono": time.monotonic(),
        }
    )
    assert heartbeat_age(fresh_mono) < 1.0
    assert heartbeat_age(fresh_mono.model_copy(update={"heartbeat_mono": None})) > 3500
    runstore.write_status(run_path, fresh_mono)
    supervisor = Supervisor(load_config(workspace, env={}))
    assert supervisor.reconcile(run_path.name).state is RunState.RUNNING  # a wall-clock jump
    stale = fresh_mono.model_copy(
        update={"heartbeat_at": utc_now(), "heartbeat_mono": time.monotonic() - 60.0}
    )
    runstore.write_status(run_path, stale)
    taken = supervisor.reconcile(run_path.name)
    assert taken.state is RunState.FAILED and taken.error is not None
    assert taken.error.code is ErrorCode.WORKER_LOST
