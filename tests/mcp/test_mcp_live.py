"""Live runs through the MCP tools with real workers (ADR 0010): ``run_start(live=true)`` pauses at
t0, every ``run_control`` action on BouncingBall Co-Simulation, ``run_status(values=[...])``, the
wait bounds and the refusal texts the model sees.

Collaborators: ``mcp_support.Session`` (the ``session`` fixture), the ``fmu`` fixture and
``tests/system_fmus.py`` (a Model Exchange-only synthetic FMU for the refusal), and
``agentic_fmi.store.runstore`` to read the result a test checks numerically. Workers are real
processes; the fixture's cleanup stops or kills any a test leaves, so none outlives its test.
The last test runs a live system through the same tools; OMSimulator executes it in the worker.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from mcp_support import Session
from system_fmus import PLANT_VARIABLES, write_fmu

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import SystemSpec

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]
H = 0.01
APEX_RATIO_TOLERANCE = 0.03


def start_live(session: Session, **options: Any) -> dict[str, Any]:
    """Start a live BouncingBall run (h = 0.01, stop at 3 s unless told otherwise); it is
    paused at t0 when this returns."""
    arguments: dict[str, Any] = {"stop_time": 3.0, "output_interval": H, "wait_s": 30}
    view = session.ok("run_start", fmu=BALL, live=True, **{**arguments, **options})
    assert view["state"] == "paused", view
    return view


def run_path_of(session: Session, run_id: str) -> Path:
    return session.config.store_dir / runstore.RUNS_DIR / run_id


def control(session: Session, run_id: str, action: str, **arguments: Any) -> dict[str, Any]:
    return session.ok("run_control", run_id=run_id, action=action, **arguments)


def apexes(height: np.ndarray) -> list[float]:
    """Interior local maxima of the height: the apex after each bounce."""
    return [
        float(height[k])
        for k in range(1, len(height) - 1)
        if height[k - 1] < height[k] >= height[k + 1]
    ]


def values_at(
    session: Session, run_id: str, t: float, names: list[str], timeout_s: float = 10.0
) -> dict[str, Any]:
    """``run_status(values=names)`` once ``live.json`` has the sample taken at ``t`` (the worker
    writes it on its next heartbeat after the pause)."""
    limit = time.monotonic() + timeout_s
    while True:
        view = session.ok("run_status", run_id=run_id, values=names)
        found = re.match(r"Values at t=(\S+?)\. ", view["hint"])
        if found is not None and float(found.group(1)) == pytest.approx(t):
            return view
        assert time.monotonic() < limit, f"no live.json sample at t={t}: {view['hint']}"
        time.sleep(0.05)


# --- run_start(live=true) -------------------------------------------------------------------


def test_run_start_live_returns_paused_at_t0_and_the_run_holds_a_slot(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    started = time.monotonic()
    view = start_live(session)
    assert time.monotonic() - started < 25  # it returned at the pause, not at wait_s
    assert (view["state"], view["t"], view["speed"], view["fmi_type"]) == (
        "paused",
        0.0,
        1.0,
        "CoSimulation",
    )
    assert view["hint"].startswith(
        "Paused at t=0. Continue with run_control('r1', action='resume')"
    )
    spec = runstore.read_spec(run_path_of(session, "r1"))
    assert (spec.live, spec.idle_limit_s) == (True, session.config.timing.live_idle_limit_s)
    assert session.ok("workspace_info")["active_runs"] == ["r1"]  # paused is active
    assert session.ok("run_list", state="paused")["total"] == 1
    assert session.ok("run_status", run_id="r1")["state"] == "paused"


# --- the actions ----------------------------------------------------------------------------


def test_pause_resume_and_the_speed_of_a_running_run(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session, stop_time=30.0)["run_id"]
    resumed = control(session, run_id, "resume", speed=1.0)
    assert (resumed["state"], resumed["speed"]) == ("running", 1.0)
    assert resumed["hint"].startswith("Applied resume at t=0.")
    faster = control(session, run_id, "resume", speed=2.0)
    assert faster["speed"] == 2.0
    assert "(already running; speed set to 2)" in faster["hint"]
    time.sleep(0.4)
    paused = control(session, run_id, "pause")
    assert paused["state"] == "paused" and 0.0 < paused["t"] < 30.0
    assert paused["hint"].startswith(f"Applied pause at t={paused['t']:g}")
    again = control(session, run_id, "pause")
    assert "(already paused)" in again["hint"] and again["t"] == paused["t"]
    control(session, run_id, "resume", speed=1000.0)
    done = session.wait_for(run_id, {"finished"})
    assert done["speed"] == 1000.0 and done["summary"]["n_points"] == 3001
    assert "reason" not in done and done["hint"].startswith("Use run_plot")


def test_a_step_advances_n_steps_or_at_least_dt_and_pauses_again(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    five = control(session, run_id, "step", n=5)
    assert five["state"] == "paused" and five["t"] == pytest.approx(0.05)
    assert five["hint"].startswith("Applied step at t=0.05.")
    dt = control(session, run_id, "step", dt=0.025)  # ceil(2.5) = 3 steps of 0.01 s
    assert dt["t"] == pytest.approx(0.08) and dt["state"] == "paused"
    assert "dt=0.025 s is 3 step(s) of 0.01 s" in dt["hint"]
    one = control(session, run_id, "step")
    assert one["t"] == pytest.approx(0.09)


def test_set_e_then_the_apex_ratio_follows_the_new_e(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    control(session, run_id, "step", n=20)
    applied = control(session, run_id, "set", values={"e": 0.6})
    assert applied["state"] == "paused" and applied["hint"].startswith("Applied set at t=0.2.")
    control(session, run_id, "resume", speed=1000.0)
    assert session.wait_for(run_id, {"finished"})["summary"]["n_points"] == 301
    height = runstore.read_result(run_path_of(session, run_id), ["h"])["h"]
    found = apexes(height)
    assert len(found) >= 2
    # e acts at a bounce and the first is at t = 0.45, so the set at t = 0.2 holds from the start:
    # the ratio of two apexes is e squared (0.36), not the FMU's default 0.7 squared (0.49)
    assert found[1] / found[0] == pytest.approx(0.36, rel=APEX_RATIO_TOLERANCE)


def test_stop_ends_the_run_with_its_partial_result(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    control(session, run_id, "step", n=3)
    started = time.monotonic()
    stopped = control(session, run_id, "stop", wait_s=30)
    assert time.monotonic() - started < 15  # it waits for the run to end, not for wait_s
    assert stopped["state"] == "stopped" and stopped["summary"]["n_points"] == 4
    assert stopped["hint"].startswith("Applied stop at t=0.03.")
    assert "partial result is kept" in stopped["hint"]
    assert "takes no more commands" in session.error("run_control", run_id=run_id, action="pause")


def test_a_finished_run_takes_no_more_commands(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session, stop_time=0.2)["run_id"]
    control(session, run_id, "resume", speed=1000.0)
    session.wait_for(run_id, {"finished"})
    text = session.error("run_control", run_id=run_id, action="resume")
    assert "finished and takes no more commands" in text and f"run_results('{run_id}')" in text


# --- waits ----------------------------------------------------------------------------------


def test_wait_s_zero_returns_at_once_and_the_command_still_lands(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session, stop_time=30.0)["run_id"]
    started = time.monotonic()
    queued = control(session, run_id, "resume", speed=1.0, wait_s=0)
    assert time.monotonic() - started < 2.0
    assert queued["hint"].startswith(("The worker hasn't answered", "Applied resume"))
    session.wait_for(run_id, {"running"})  # the worker applied it all the same


def test_an_unanswered_command_waits_exactly_wait_s_and_names_its_id(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    pid = runstore.read_status(run_path_of(session, run_id)).pid
    assert pid is not None
    os.killpg(pid, signal.SIGSTOP)  # a worker that does not answer
    try:
        started = time.monotonic()
        view = control(session, run_id, "step", n=2, wait_s=1)
        elapsed = time.monotonic() - started
        assert 1.0 <= elapsed < 3.5
        command = next((run_path_of(session, run_id) / "commands").iterdir()).stem
        assert f"the command is queued as {command}" in view["hint"]
        assert view["state"] == "paused"
    finally:
        os.killpg(pid, signal.SIGCONT)
    session.wait_for(run_id, {"paused"})  # the step ran after the worker woke
    assert session.ok("run_status", run_id=run_id)["state"] == "paused"


def test_the_wait_cannot_be_longer_than_the_tool_allows(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    assert session.error("run_control", run_id=run_id, action="pause", wait_s=61)


# --- run_status(values) ---------------------------------------------------------------------


def test_run_status_values_reports_recorded_outputs_with_units(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    start = values_at(session, run_id, 0.0, ["h", "v"])
    assert start["values"] == [
        {"name": "h", "unit": "m", "value": 1.0},
        {"name": "v", "unit": "m/s", "value": 0.0},
    ]
    assert start["state"] == "paused" and "summary" not in start
    control(session, run_id, "step", n=10)
    later = values_at(session, run_id, 0.1, ["v", "h"])
    assert [c["name"] for c in later["values"]] == ["v", "h"]
    speed, height = later["values"][0]["value"], later["values"][1]["value"]
    assert speed == pytest.approx(-9.81 * 0.1, rel=0.01) and 0.9 < height < 1.0


def test_run_status_values_refusals(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    run_id = start_live(session)["run_id"]
    values_at(session, run_id, 0.0, ["h"])  # live.json exists from here on
    unknown = session.error("run_status", run_id=run_id, values=["hh"])
    assert "'hh' is not a recorded output of r1" in unknown
    assert "Close matches: h" in unknown and "recorded outputs: h, v, h_ft" in unknown
    assert "outputs=[...]" in unknown
    assert session.error("run_status", run_id=run_id, values=[f"y{i}" for i in range(26)])
    batch = session.ok("run_start", fmu=BALL, stop_time=0.5, wait_s=30)
    assert batch["state"] == "finished"
    text = session.error("run_status", run_id=batch["run_id"], values=["h"])
    assert "only available for live runs" in text and "run_results" in text


# --- refusals -------------------------------------------------------------------------------


def test_run_start_live_refusals(session: Session, fmu: FmuFactory, workspace: Path) -> None:
    fmu("3.0", "BouncingBall")
    write_fmu(workspace / "me.fmu", PLANT_VARIABLES, interfaces=("ModelExchange",))
    only_me = session.error("run_start", fmu="me.fmu", live=True)
    assert "me.fmu is a Model Exchange FMU" in only_me
    assert "live control needs Co-Simulation" in only_me and "drop live=true" in only_me
    asked = session.error("run_start", fmu=BALL, live=True, fmi_type="ModelExchange")
    assert "fmi_type='ModelExchange' can't be live" in asked
    assert runstore.list_run_ids(session.config.store_dir) == []  # no ID was claimed


def test_run_control_refusals_name_the_fix(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    batch = session.ok("run_start", fmu=BALL, stop_time=0.5, wait_s=30)["run_id"]
    text = session.error("run_control", run_id=batch, action="pause")
    assert "batch run, which takes no commands" in text and "live=true" in text

    run_id = start_live(session, stop_time=30.0)["run_id"]
    assert "n (communication steps) or dt (seconds), not both" in session.error(
        "run_control", run_id=run_id, action="step", n=2, dt=0.1
    )
    assert "pause takes no arguments; remove n" in session.error(
        "run_control", run_id=run_id, action="pause", n=2
    )
    on_output = session.error("run_control", run_id=run_id, action="set", values={"h": 1.0})
    assert "h is an output, which the FMU computes" in on_output
    unknown = session.error("run_control", run_id=run_id, action="set", values={"hh": 1.0})
    assert "Unknown variable 'hh'. Close matches: h" in unknown
    assert "outside its declared range" in session.error(
        "run_control", run_id=run_id, action="set", values={"e": 0.1}
    )
    assert session.error("run_control", run_id=run_id, action="set")  # values required
    # a speed outside (0, 1000] is refused by the tool's own schema, before the server sees it
    assert "1000" in session.error("run_control", run_id=run_id, action="resume", speed=5000)
    assert session.error("run_control", run_id=run_id, action="resume", speed=0)
    control(session, run_id, "resume", speed=1.0)
    running = session.error("run_control", run_id=run_id, action="step", n=2)
    assert "needs a paused run, but r2 is running" in running and "action='pause'" in running


# --- a live system --------------------------------------------------------------------------


def test_a_live_system_run_through_the_same_tools(
    session: Session, fmu: FmuFactory, workspace: Path
) -> None:
    """One BouncingBall (FMI 2.0) as a system on OMSimulator: paused at t0, a step, a set of
    ``B.e`` checked by the server against the component's description, then the apex ratio."""
    fmu("2.0", "BouncingBall")
    document = {
        "version": 1,
        "components": {"B": {"fmu": "2.0/BouncingBall.fmu"}},
        "connections": [],
        "experiment": {"start_time": 0, "stop_time": 3, "step_size": H},
    }
    (workspace / "ball.json").write_text(json.dumps(document), encoding="utf-8")
    view = session.ok("run_start", system="ball.json", live=True, wait_s=60)
    run_id = view["run_id"]
    assert (view["kind"], view["state"], view["speed"]) == ("system", "paused", 1.0)
    assert view["hint"].startswith("Paused at t=0.")
    spec = runstore.read_job_spec(run_path_of(session, run_id))
    assert isinstance(spec, SystemSpec) and spec.live
    assert "B.h is an output" in session.error(
        "run_control", run_id=run_id, action="set", values={"B.h": 1.0}
    )
    assert "names no variable of a component" in session.error(
        "run_control", run_id=run_id, action="set", values={"e": 0.6}
    )
    assert control(session, run_id, "step", n=20)["t"] == pytest.approx(0.2)
    assert values_at(session, run_id, 0.2, ["B.h"])["values"][0]["unit"] == "m"
    control(session, run_id, "set", values={"B.e": 0.6})
    control(session, run_id, "resume", speed=1000.0)
    assert session.wait_for(run_id, {"finished"}, timeout_s=60)["summary"]["n_points"] == 301
    height = runstore.read_result(run_path_of(session, run_id), ["B.h"])["B.h"]
    found = apexes(height)
    assert len(found) >= 2 and found[1] / found[0] == pytest.approx(0.36, rel=APEX_RATIO_TOLERANCE)
