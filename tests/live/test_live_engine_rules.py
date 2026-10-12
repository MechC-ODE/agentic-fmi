"""The engine-neutral end rules (ADR 0010) on FMPy through real workers: a step that never
advances is ``simulation_error`` (the stall check ends FMPy's endless retry of a discard
without terminate), and an FMU that ends the run before ``t_stop`` is ``finished`` with
``status.reason`` "ended early at t=...", batch and live; plus ``engines.base.ended_early`` and
the values ``_FmpyLive.set`` reports as applied when a later type group is refused.

OMSimulator's side (a discard or an FMI 2.0 terminate stalls its clock: ``simulation_error``; an
FMI 3.0 terminate request is ignored) is pinned by ``tests/worker/test_worker_system.py``.

Collaborators: ``agentic_fmi.worker`` (as ``python -m agentic_fmi worker``),
``agentic_fmi.engines.fmpy_engine`` and ``engines.base``, the ``discarder`` fixture (a C FMU
compiled for the test session), the Stair Reference FMUs, ``live_support``.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_support import outcome_of, send

from agentic_fmi.engines import fmpy_engine
from agentic_fmi.engines.base import ended_early
from agentic_fmi.errors import SetRefused
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState, RunStatus

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
Spawn = Callable[[Path], "subprocess.Popen[bytes]"]
RunWorker = Callable[[Path], RunStatus]
AwaitStatus = Callable[..., RunStatus]


def test_ended_early_needs_a_whole_step_short_and_no_stop() -> None:
    assert ended_early(9.0, 10.0, stopped=False, step=0.2)
    assert not ended_early(9.0, 10.0, stopped=True, step=0.2)
    assert not ended_early(10.0, 10.0, stopped=False, step=0.2)
    assert not ended_early(0.9, 1.0, stopped=False, step=0.3)  # the grid's last point
    assert ended_early(0.6, 1.0, stopped=False, step=0.3)
    assert ended_early(0.5, 1.0, stopped=False)


def test_a_set_refused_in_a_later_type_group_lists_the_groups_set_before_it() -> None:
    """``_FmpyLive.set`` makes one FMI call per type; a failing one names its own variables and
    the refusal's ``applied`` holds the earlier groups (a stand-in slave: no binary is loaded)."""
    slave = SimpleNamespace(calls=[])
    slave.setReal = lambda references, values: slave.calls.append(("Real", references, values))

    def refuse_integer(references: list[int], values: list[int]) -> None:
        raise RuntimeError("fmi2Error")

    slave.setInteger = refuse_integer
    variables = [
        SimpleNamespace(name=name, valueReference=reference, type=kind, dimensions=None)
        for reference, (name, kind) in enumerate([("k", "Real"), ("n", "Integer"), ("u", "Real")])
    ]
    md = SimpleNamespace(fmiVersion="2.0", modelVariables=variables)
    with pytest.raises(SetRefused, match="refused to set n: fmi2Error") as raised:
        fmpy_engine._FmpyLive(slave, md).set({"k": 2.0, "n": 3, "u": 1.0})
    assert raised.value.applied == {"k": 2.0, "u": 1.0}
    assert slave.calls == [("Real", [0, 2], [2.0, 1.0])]


def test_a_discard_without_terminate_is_a_simulation_error(
    discarder: Path, new_run: RunBuilder, run_worker: RunWorker
) -> None:
    run_path = new_run(discarder, stop_time=1.0, output_interval=0.1)
    started = time.monotonic()
    status = run_worker(run_path)
    assert time.monotonic() - started < 30  # no endless retry
    assert status.state is RunState.FAILED and status.error is not None
    assert status.error.code is ErrorCode.SIMULATION_ERROR
    assert "no progress at t=0.5" in status.error.message
    assert "without terminating" in status.error.message


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_an_fmu_that_terminates_ends_finished_with_a_reason(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker, version: str
) -> None:
    run_path = new_run(fmu(version, "Stair"), stop_time=10.0, output_interval=0.2)
    status = run_worker(run_path)
    assert status.state is RunState.FINISHED and status.error is None
    assert status.t == pytest.approx(9.0)
    assert status.reason is not None and status.reason.startswith("ended early at t=9 of t_stop=10")
    summary = runstore.read_summary(run_path)
    assert summary is not None and summary.t_end == pytest.approx(9.0)


def test_a_full_run_has_no_reason(
    fmu: FmuFactory, new_run: RunBuilder, run_worker: RunWorker
) -> None:
    run_path = new_run(fmu("2.0", "Stair"), stop_time=5.0, output_interval=0.2)
    status = run_worker(run_path)
    assert status.state is RunState.FINISHED and status.reason is None


def test_a_live_run_the_fmu_ends_is_finished_with_a_reason(
    fmu: FmuFactory, new_live_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = new_live_run(fmu("3.0", "Stair"), stop_time=10.0, output_interval=0.2)
    child = spawn(run_path)
    await_status(run_path, lambda s: s.state is RunState.PAUSED)
    resume = send(run_path, "resume", speed=1000.0)
    assert outcome_of(run_path, resume).status == "applied"
    assert child.wait(timeout=30) == 0
    status = runstore.read_status(run_path)
    assert status.state is RunState.FINISHED and status.t == pytest.approx(9.0)
    assert status.reason is not None and "ended early at t=9" in status.reason


def test_a_live_run_that_stalls_fails_and_sweeps_its_inbox(
    discarder: Path, new_live_run: RunBuilder, spawn: Spawn, await_status: AwaitStatus
) -> None:
    run_path = new_live_run(discarder, stop_time=1.0, output_interval=0.1)
    child = spawn(run_path)
    await_status(run_path, lambda s: s.state is RunState.PAUSED)
    resume = send(run_path, "resume", speed=1000.0)
    assert outcome_of(run_path, resume).status == "applied"
    assert child.wait(timeout=30) == 0
    status = runstore.read_status(run_path)
    assert status.state is RunState.FAILED and status.error is not None
    assert status.error.code is ErrorCode.SIMULATION_ERROR and status.last_cmd == resume
