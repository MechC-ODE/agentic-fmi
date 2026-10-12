"""Run lifecycle through the MCP tools with real workers: the recorded spec, polling, stop,
the step guard and timeout, the worker limit, a failed spawn, and reconciled run lists.

Collaborators: ``mcp_support.Session`` (the ``session`` fixture), the ``fmu`` fixture, and
``agentic_fmi.store.runstore`` to look at what the server wrote. Every long run is stopped or
killed by the fixture's cleanup, so no worker outlives its test.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from mcp_support import Session, open_session

from agentic_fmi.config import Config
from agentic_fmi.fmu.inspect import sha256_file
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState
from agentic_fmi.supervisor import Supervisor, is_alive

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]
LONG = {"stop_time": 1000.0, "output_interval": 0.001}
"""About a million steps: far longer than any test waits."""


def start_long(session: Session) -> str:
    """Start a run that won't finish by itself; return its ID once it is running."""
    view = session.ok("run_start", fmu=BALL, wait_s=0, **LONG)
    assert view["state"] in ("queued", "running")
    session.wait_for(view["run_id"], {"running"})
    return str(view["run_id"])


def kill_worker(session: Session, run_id: str, timeout_s: float = 10.0) -> None:
    """SIGKILL the run's worker group and wait until the worker runs no more (gone, or a zombie
    nobody has reaped yet). ``killpg`` returns before the kernel has ended the process: it stays
    alive in ``/proc`` for a few milliseconds (tens under CPU load), and a reconciliation in
    that window rightly still sees a live worker."""
    status = runstore.read_status(session.config.store_dir / runstore.RUNS_DIR / run_id)
    assert status.pid is not None
    os.killpg(status.pid, signal.SIGKILL)
    deadline = time.monotonic() + timeout_s
    while is_alive(status.pid, status.pid_start_time):
        assert time.monotonic() < deadline, f"worker {status.pid} survived SIGKILL"
        time.sleep(0.005)


def test_run_start_records_a_reproducible_spec_and_never_touches_the_fmu(
    session: Session, fmu: FmuFactory
) -> None:
    path = fmu("3.0", "BouncingBall")
    digest = sha256_file(path)
    view = session.ok("run_start", fmu=BALL, start_values={"e": 0.7}, wait_s=30)
    assert view["state"] == "finished"
    spec = runstore.read_spec(session.config.store_dir / runstore.RUNS_DIR / "r1")
    assert spec.fmu.sha256 == digest
    assert spec.fmu.path == str(path)
    assert spec.fmi_type == "CoSimulation"
    assert spec.start_values == {"e": 0.7}
    assert spec.outputs == ["h", "v", "h_ft"]
    assert spec.experiment.output_interval == 0.004  # FMPy's choice, made explicit
    assert spec.timeout_s == 600.0 and spec.force is False
    assert spec.versions.fmpy == "0.3.32" and spec.versions.agentic_fmi
    assert sha256_file(path) == digest
    lines = (session.config.store_dir / "events.jsonl").read_text().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["created", "started", "finished"]


def test_wait_zero_returns_at_once_and_status_polls_to_the_end(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    view = session.ok("run_start", fmu=BALL, wait_s=0, stop_time=3.0)
    assert view["state"] in ("queued", "running")
    assert f"run_status('{view['run_id']}')" in view["hint"]
    assert "summary" not in view
    done = session.wait_for(view["run_id"], {"finished"})
    assert done["summary"]["n_points"] == 751
    assert done["t"] == 3.0
    assert "error" not in done


def test_inputs_drive_the_fmu(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "Feedthrough")
    step = {"kind": "step", "time": 0.5, "before": 0.0, "after": 2.0}
    view = session.ok(
        "run_start",
        fmu="3.0/Feedthrough.fmu",
        inputs={"Float64_continuous_input": step},
        outputs=["Float64_continuous_output"],
        wait_s=30,
    )
    assert view["state"] == "finished", view
    (summary,) = view["summary"]["variables"]
    assert summary["name"] == "Float64_continuous_output"
    assert summary["min"] == 0.0 and summary["max"] == 2.0 and summary["final"] == 2.0


def test_stop_keeps_a_partial_result_and_a_second_stop_changes_nothing(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_long(session)
    assert "only finished or stopped" in session.error("run_results", run_id=run_id)
    stopped = session.ok("run_stop", run_id=run_id)
    assert stopped["state"] == "stopped"
    assert stopped["summary"]["n_points"] > 0
    assert stopped["summary"]["t_end"] < 1000.0
    assert "partial result" in stopped["hint"]
    assert session.ok("run_stop", run_id=run_id)["state"] == "stopped"
    assert session.ok("run_status", run_id=run_id)["state"] == "stopped"
    results = session.ok("run_results", run_id=run_id, variables=["h"], max_points=10)
    assert results["t_end"] == stopped["summary"]["t_end"]


@pytest.mark.slow
def test_roberts_is_refused_without_force_and_times_out_with_it(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "Roberts")
    message = session.error("run_start", fmu="3.0/Roberts.fmu")
    assert "ModelExchange" in message and "force=true" in message
    assert session.ok("run_list")["total"] == 0  # a refused run leaves no trace
    view = session.ok("run_start", fmu="3.0/Roberts.fmu", force=True, timeout_s=5, wait_s=0)
    assert view["state"] in ("queued", "running")
    failed = session.wait_for(view["run_id"], {"failed"}, timeout_s=30)
    assert failed["error"]["code"] == "timeout"
    assert "timeout_s" in failed["error"]["message"]
    assert "run_start" in failed["hint"]
    spec = runstore.read_spec(session.config.store_dir / runstore.RUNS_DIR / view["run_id"])
    assert spec.force is True and spec.timeout_s == 5.0 and spec.estimated_steps is not None


def test_the_worker_limit_refuses_the_next_run_and_frees_up_after_a_stop(
    mcp_config: Config, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    with open_session(replace(mcp_config, max_workers=1)) as session:
        first = start_long(session)
        message = session.error("run_start", fmu=BALL, wait_s=0)
        assert f"already queued or running ({first})" in message
        assert "limit is 1" in message and "run_stop" in message
        assert session.ok("workspace_info")["active_runs"] == [first]
        session.ok("run_stop", run_id=first)
        second = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)
        assert second["state"] == "finished"


def test_a_failed_spawn_is_recorded_as_internal_error(
    session: Session, fmu: FmuFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fmu("3.0", "BouncingBall")

    def refuse(self: Supervisor, run_dir: Path) -> int:
        raise OSError(errno.ENOMEM, "Cannot allocate memory")

    monkeypatch.setattr(Supervisor, "spawn", refuse)
    view = session.ok("run_start", fmu=BALL, wait_s=5)
    assert view["state"] == "failed"
    assert view["error"]["code"] == "internal_error"
    assert "could not be started" in view["error"]["message"]
    assert "Cannot allocate memory" in view["error"]["message"]
    assert session.ok("run_status", run_id="r1")["state"] == "failed"
    assert session.ok("run_list", state="failed")["items"][0]["run_id"] == "r1"
    events = [
        json.loads(x) for x in (session.config.store_dir / "events.jsonl").read_text().splitlines()
    ]
    assert [(e["type"], e["source"]) for e in events] == [
        ("created", "server"),
        ("failed", "server"),
    ]
    assert session.ok("workspace_info")["active_runs"] == []


def test_run_list_reconciles_states_pages_and_filters(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    finished = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)
    lost = start_long(session)
    running = start_long(session)
    kill_worker(session, lost)

    page = session.ok("run_list")
    states = {item["run_id"]: item["state"] for item in page["items"]}
    assert states == {finished["run_id"]: "finished", lost: "failed", running: "running"}
    assert [item["run_id"] for item in page["items"]] == [running, lost, finished["run_id"]]
    assert page["total"] == 3 and "next_offset" not in page
    assert page["items"][0]["fmu_name"] == "BouncingBall"
    assert session.ok("run_status", run_id=lost)["error"]["code"] == "worker_lost"

    first_two = session.ok("run_list", limit=2)
    assert len(first_two["items"]) == 2 and first_two["next_offset"] == 2
    rest = session.ok("run_list", limit=2, offset=2)
    assert [i["run_id"] for i in rest["items"]] == [finished["run_id"]]
    assert session.ok("run_list", state="running")["total"] == 1
    assert session.ok("run_list", state="failed")["items"][0]["run_id"] == lost
    assert session.ok("run_list", state="stopped")["total"] == 0


def test_failed_and_unfinished_runs_have_no_results_to_read(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    good = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)["run_id"]
    lost = start_long(session)
    kill_worker(session, lost)
    session.wait_for(lost, {"failed"})
    for tool, arguments in (
        ("run_results", {"run_id": lost}),
        ("run_compare", {"run_ids": [good, lost]}),
        ("run_plot", {"run_ids": [good, lost]}),
    ):
        message = session.error(tool, **arguments)
        assert f"Run {lost} failed (worker_lost)" in message
        assert "start a new run" in message


def test_a_run_started_from_another_server_is_seen_after_its_reconciliation(
    mcp_config: Config, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    with open_session(mcp_config) as first:
        run_id = start_long(first)
        with open_session(mcp_config) as second:  # shares the store; did not spawn the worker
            assert second.ok("workspace_info")["active_runs"] == [run_id]
            assert second.ok("run_status", run_id=run_id)["state"] == "running"
            kill_worker(first, run_id)
            view = second.ok("run_status", run_id=run_id)
            assert view["state"] == "failed" and view["error"]["code"] == "worker_lost"
        assert first.ok("run_status", run_id=run_id)["state"] == "failed"


def test_workspace_info_counts_live_runs_without_reconciling(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_long(session)
    assert session.ok("workspace_info")["active_runs"] == [run_id]
    run_path = session.config.store_dir / runstore.RUNS_DIR / run_id
    kill_worker(session, run_id)

    assert session.ok("workspace_info")["active_runs"] == []  # a lost worker isn't counted ...
    assert runstore.read_status(run_path).state is RunState.RUNNING  # ... and nothing was written
    assert session.ok("run_status", run_id=run_id)["state"] == "failed"  # run_status reconciles
