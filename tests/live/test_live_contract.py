"""The live contract (ADR 0010) on both engines through real workers: a live run starts paused at
t0 with ``live.json`` and a beating heartbeat, ``step n`` advances exactly n communication steps,
pause and resume, resume with a speed paces the run, a set acts from the next step (Real,
Integer, Boolean and Enumeration inputs), a stop while paused ends the run promptly as
``stopped``, waiting is excluded from the timeout, and the idle limit ends a paused run.

Each test runs once per engine on the Reference FMU Feedthrough 2.0 (Co-Simulation; every
output follows its unconnected input): as an FMU run (``RunSpec``, the FMPy engine) and as a
one-component system ``M`` (``SystemSpec``, the OMSimulator engine), so the only differences
are the engine and the ``M.`` prefix of a system's names.

Collaborators: ``agentic_fmi.worker`` (under test, as ``python -m agentic_fmi worker``),
``engines/fmpy_engine.py`` and ``engines/oms_engine.py`` (run by it), ``agentic_fmi.live`` (the
gate), the fixtures of ``tests/live/conftest.py`` (``new_live_run``, ``new_live_system_run``,
``spawn``, ``await_status``) and ``live_support`` (commands and outcomes).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from live_support import outcome_of, send

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState, RunStatus, SetValue

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
AwaitStatus = Callable[..., RunStatus]

COMPONENT = "M"
H = 0.01
STOP = 1.0
FAST = 1000.0
"""The speed that finishes a run at once (``Timing.live_max_speed``)."""

OUTPUTS = ["Float64_continuous_output", "Int32_output", "Boolean_output", "Enumeration_output"]
SETS: Mapping[str, SetValue] = {
    "Float64_continuous_input": 2.5,
    "Int32_input": 7,
    "Boolean_input": True,
    "Enumeration_input": 2,
}
"""One value per type family; each input drives the output of the same position in OUTPUTS."""


@dataclass(frozen=True)
class LiveEngine:
    """How a test starts a live run of Feedthrough on one engine and names its variables."""

    name: str
    new_live_run: RunBuilder
    new_live_system_run: RunBuilder

    def start(self, fmu_path: Path, *, stop_time: float = STOP, **options: float) -> Path:
        """A queued live run recording :data:`OUTPUTS` every :data:`H` up to ``stop_time``;
        ``options``: ``idle_limit_s``, ``timeout_s``."""
        if self.name == "fmpy":
            return self.new_live_run(
                fmu_path, outputs=OUTPUTS, stop_time=stop_time, output_interval=H, **options
            )
        return self.new_live_system_run(
            {COMPONENT: fmu_path},
            outputs=[self.column(name) for name in OUTPUTS],
            step_size=H,
            stop_time=stop_time,
            **options,
        )

    def column(self, name: str) -> str:
        """The run's name of variable ``name`` (a result column or a set's key)."""
        return name if self.name == "fmpy" else f"{COMPONENT}.{name}"


@pytest.fixture(params=["fmpy", "oms"])
def engine(
    request: pytest.FixtureRequest, new_live_run: RunBuilder, new_live_system_run: RunBuilder
) -> LiveEngine:
    if request.param == "oms":
        request.applymarker(pytest.mark.oms)
    return LiveEngine(request.param, new_live_run, new_live_system_run)


@pytest.fixture
def feedthrough(fmu: FmuFactory) -> Path:
    return fmu("2.0", "Feedthrough")


def _paused(status: RunStatus) -> bool:
    return status.state is RunState.PAUSED


def _paused_after(command_id: str) -> Callable[[RunStatus], bool]:
    return lambda s: _paused(s) and s.last_cmd == command_id


def _finish(run_path: Path, child: subprocess.Popen[bytes], timeout_s: float = 30.0) -> RunStatus:
    assert child.wait(timeout=timeout_s) == 0, (run_path / runstore.LOG_FILE).read_text()
    return runstore.read_status(run_path)


def test_a_live_run_starts_paused_at_t0_and_beats_with_live_json(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = engine.start(feedthrough)
    child = spawn(run_path)
    paused = await_status(run_path, _paused)
    assert (paused.t, paused.progress, paused.speed) == (0.0, 0.0, 1.0)
    later = await_status(
        run_path, lambda s: (s.heartbeat_mono or 0) >= (paused.heartbeat_mono or 0) + 1.0
    )
    assert later.state is RunState.PAUSED and later.t == 0.0  # beating, not stepping
    snapshot = runstore.read_live(run_path)
    assert snapshot is not None and snapshot.t == 0.0 and snapshot.time == [0.0]
    assert [c.name for c in snapshot.values] == [engine.column(name) for name in OUTPUTS]
    assert snapshot.values[0].value == 0.0
    stop = send(run_path, "stop")
    status = _finish(run_path, child)
    assert status.state is RunState.STOPPED and status.reason is None and status.last_cmd == stop
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.n_points == 1 and summary.t_end == 0.0


def test_step_n_advances_exactly_n_communication_steps(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = engine.start(feedthrough)
    child = spawn(run_path)
    await_status(run_path, _paused)
    for n, t in ((3, 3 * H), (1, 4 * H), (6, 10 * H)):
        step = send(run_path, "step", n=n)
        stepped = outcome_of(run_path, step)
        assert (stepped.status, stepped.message) == ("applied", None)
        assert stepped.t == pytest.approx(t, abs=1e-12)
        assert await_status(run_path, _paused_after(step)).t == pytest.approx(t, abs=1e-12)
    send(run_path, "stop")
    assert _finish(run_path, child).state is RunState.STOPPED
    result = runstore.read_result(run_path)
    np.testing.assert_allclose(result["time"], np.arange(11) * H, rtol=0, atol=1e-12)


def test_pause_holds_the_run_and_resume_continues_it(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = engine.start(feedthrough, stop_time=10.0)
    child = spawn(run_path)
    await_status(run_path, _paused)
    assert outcome_of(run_path, send(run_path, "resume")).status == "applied"
    await_status(run_path, lambda s: s.state is RunState.RUNNING and (s.t or 0) > 0.1)
    pause = send(run_path, "pause")
    paused_at = outcome_of(run_path, pause).t
    held = await_status(run_path, _paused_after(pause))
    assert 0.1 < held.t == pytest.approx(paused_at, abs=1e-12)
    still = await_status(
        run_path, lambda s: (s.heartbeat_mono or 0) > (held.heartbeat_mono or 0) + 0.4
    )
    assert still.state is RunState.PAUSED and still.t == held.t  # beating, not stepping
    assert outcome_of(run_path, send(run_path, "resume", speed=FAST)).status == "applied"
    status = _finish(run_path, child)
    assert status.state is RunState.FINISHED and status.t == pytest.approx(10.0)
    assert status.speed == FAST and status.reason is None
    result = runstore.read_result(run_path)
    np.testing.assert_allclose(result["time"], np.arange(1001) * H, rtol=0, atol=1e-9)


def test_resume_with_a_speed_paces_the_run(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    speed = 2.0
    run_path = engine.start(feedthrough)
    child = spawn(run_path)
    await_status(run_path, _paused)
    started = time.monotonic()
    resume = send(run_path, "resume", speed=speed)
    status = _finish(run_path, child)
    elapsed = time.monotonic() - started
    assert outcome_of(run_path, resume).status == "applied"
    assert status.state is RunState.FINISHED and status.speed == speed
    assert STOP / speed <= elapsed < STOP / speed + 5.0  # paced, never early


def test_a_set_acts_from_the_next_step(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = engine.start(feedthrough)
    child = spawn(run_path)
    await_status(run_path, _paused)
    assert outcome_of(run_path, send(run_path, "step", n=5)).t == pytest.approx(5 * H)
    values = {engine.column(name): value for name, value in SETS.items()}
    applied = outcome_of(run_path, send(run_path, "set", values=values))
    assert (applied.status, applied.message) == ("applied", None)
    assert applied.t == pytest.approx(5 * H)
    assert outcome_of(run_path, send(run_path, "resume", speed=FAST)).status == "applied"
    assert _finish(run_path, child).state is RunState.FINISHED
    result = runstore.read_result(run_path)
    assert len(result) == 101
    for output, value in zip(OUTPUTS, SETS.values(), strict=True):
        column = np.asarray(result[engine.column(output)], dtype=float)
        assert np.all(column[:6] == column[0]) and column[0] != float(value), output
        assert np.all(column[6:] == float(value)), output  # from the step after the set on


def test_a_stop_while_paused_ends_the_run_promptly_as_stopped(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = engine.start(feedthrough)
    child = spawn(run_path)
    await_status(run_path, _paused)
    step = send(run_path, "step", n=2)
    await_status(run_path, _paused_after(step))
    started = time.monotonic()
    stop = send(run_path, "stop")
    status = _finish(run_path, child)
    assert time.monotonic() - started < 5.0
    stopped = outcome_of(run_path, stop)
    assert stopped.status == "applied" and stopped.t == pytest.approx(2 * H)
    assert status.state is RunState.STOPPED and status.error is None and status.reason is None
    assert status.last_cmd == stop and status.t == pytest.approx(2 * H)
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.state is RunState.STOPPED and summary.n_points == 3
    for scratch in runstore.SCRATCH_DIRS:
        assert not (run_path / scratch).exists()


def test_waiting_is_excluded_from_the_timeout(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn, await_status: AwaitStatus
) -> None:
    timeout_s = 3.0
    run_path = engine.start(feedthrough, timeout_s=timeout_s)
    child = spawn(run_path)
    paused = await_status(run_path, _paused)
    await_status(
        run_path,
        lambda s: (s.heartbeat_mono or 0) >= (paused.heartbeat_mono or 0) + timeout_s + 0.5,
    )
    assert runstore.read_status(run_path).state is RunState.PAUSED  # not killed meanwhile
    assert outcome_of(run_path, send(run_path, "resume", speed=FAST)).status == "applied"
    status = _finish(run_path, child)
    assert status.state is RunState.FINISHED and status.error is None


def test_the_idle_limit_ends_a_paused_run(
    engine: LiveEngine, feedthrough: Path, spawn: Spawn
) -> None:
    run_path = engine.start(feedthrough, idle_limit_s=1.0)
    status = _finish(run_path, spawn(run_path))
    assert status.state is RunState.STOPPED and status.error is None
    assert status.reason is not None and "idle limit" in status.reason
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.n_points == 1 and summary.t_end == 0.0
