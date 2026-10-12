"""The live path of ``runs.py`` (ADR 0010): what ``start_run(live=True)`` records and refuses, the
wait that ends at the pause, and the views of live runs (speed, paused and reason hints).

Collaborators: ``agentic_fmi.runs`` (under test), ``agentic_fmi.supervisor.Supervisor`` (real,
with ``spawn`` faked: no worker starts, so ``spec.json`` is inspected as written), the
``runstore`` documents, and the shared ``workspace`` and ``fmu`` fixtures. Synthetic FMUs come
from ``tests/system_fmus.py``. Nothing here loads an FMU binary.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from system_fmus import PLANT_VARIABLES, write_fmu

from agentic_fmi import runs
from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunSpec, RunState, RunStatus, utc_now
from agentic_fmi.supervisor import Supervisor

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]


@pytest.fixture
def config(workspace: Path) -> Config:
    return replace(
        load_config(workspace), timing=Timing(heartbeat_stale_s=4.0, poll_interval_s=0.02)
    )


@pytest.fixture
def supervisor(config: Config) -> Supervisor:
    return Supervisor(config)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace ``Supervisor.spawn``: no worker starts; the directories it was asked to start."""
    started: list[Path] = []

    def fake_spawn(self: Supervisor, run_dir: Path) -> int:
        started.append(Path(run_dir))
        return 12345

    monkeypatch.setattr(Supervisor, "spawn", fake_spawn)
    return started


def read_spec(config: Config, run_id: str) -> RunSpec:
    return runstore.read_spec(runstore.run_dir(config.store_dir, run_id))


# --- run_start(live=True) -------------------------------------------------------------------


def test_a_live_run_records_live_and_the_idle_limit_in_its_spec(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    view = runs.start_run(config, supervisor, BALL, live=True, wait_s=0)
    spec = read_spec(config, view.run_id)
    assert (spec.live, spec.idle_limit_s, spec.fmi_type) == (True, 1800.0, "CoSimulation")
    assert view.state is RunState.QUEUED and len(spawned) == 1
    assert view.hint is not None
    assert "run_status('r1')" in view.hint and "run_control('r1'" in view.hint


def test_a_batch_run_records_no_live_fields(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    view = runs.start_run(config, supervisor, BALL, wait_s=0)
    spec = read_spec(config, view.run_id)
    assert (spec.live, spec.idle_limit_s) == (False, None)
    assert view.hint is not None and "run_control" not in view.hint and view.speed is None


def test_a_live_run_picks_co_simulation_even_for_an_fmu_that_offers_both(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    runs.start_run(config, supervisor, BALL, live=True, fmi_type="CoSimulation", wait_s=0)
    assert read_spec(config, "r1").fmi_type == "CoSimulation"


def test_the_idle_limit_is_the_configured_one(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    configured = replace(config, timing=replace(config.timing, live_idle_limit_s=90.0))
    runs.start_run(configured, supervisor, BALL, live=True, wait_s=0)
    assert read_spec(config, "r1").idle_limit_s == 90.0


def test_a_model_exchange_only_fmu_cannot_be_live(
    config: Config, supervisor: Supervisor, workspace: Path, spawned: list[Path]
) -> None:
    write_fmu(workspace / "me.fmu", PLANT_VARIABLES, interfaces=("ModelExchange",))
    with pytest.raises(UserError) as caught:
        runs.start_run(config, supervisor, "me.fmu", live=True, wait_s=0)
    text = str(caught.value)
    assert "me.fmu is a Model Exchange FMU" in text and "live control needs Co-Simulation" in text
    assert "Run it as a batch run: drop live=true" in text
    assert runstore.list_run_ids(config.store_dir) == [] and spawned == []


def test_a_live_run_cannot_ask_for_model_exchange(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    with pytest.raises(UserError, match=r"fmi_type='ModelExchange' can't be live.*batch run"):
        runs.start_run(config, supervisor, BALL, live=True, fmi_type="ModelExchange", wait_s=0)
    assert runstore.list_run_ids(config.store_dir) == [] and spawned == []


def test_a_live_run_counts_toward_max_workers(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    limited = replace(config, max_workers=1)
    runs.start_run(limited, supervisor, BALL, live=True, wait_s=0)
    with pytest.raises(UserError, match="limit is 1"):
        runs.start_run(limited, supervisor, BALL, live=True, wait_s=0)


def test_run_start_waits_until_the_run_is_paused(
    config: Config,
    supervisor: Supervisor,
    fmu: FmuFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait ends at ``paused`` (not at the end of ``wait_s``), whichever way the status
    changes; ``reconcile`` is replaced by a plain read so no worker is needed."""
    fmu("3.0", "BouncingBall")
    monkeypatch.setattr(Supervisor, "spawn", lambda self, run_dir: 12345)
    monkeypatch.setattr(
        Supervisor,
        "reconcile",
        lambda self, run_id: runstore.read_status(runstore.run_dir(config.store_dir, run_id)),
    )

    def pause_soon() -> None:
        time.sleep(0.3)
        path = runstore.run_dir(config.store_dir, "r1")
        paused = runstore.read_status(path).model_copy(
            update={"state": RunState.PAUSED, "t": 0.0, "speed": 1.0, "updated_at": utc_now()}
        )
        runstore.write_status(path, paused)

    timer = threading.Thread(target=pause_soon)
    timer.start()
    started = time.monotonic()
    view = runs.start_run(config, supervisor, BALL, live=True, wait_s=20)
    timer.join()
    assert 0.3 <= time.monotonic() - started < 5.0
    assert (view.state, view.t, view.speed) == (RunState.PAUSED, 0.0, 1.0)
    assert view.hint is not None
    assert view.hint.startswith("Paused at t=0. Continue with run_control('r1', action='resume')")


def test_a_batch_run_start_does_not_stop_waiting_at_paused(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    """Only a live start returns at ``paused``; the wait of a batch run is unchanged."""
    fmu("3.0", "BouncingBall")
    started = time.monotonic()
    view = runs.start_run(config, supervisor, BALL, wait_s=0.3)
    assert time.monotonic() - started >= 0.3 and view.state is RunState.QUEUED


# --- views ----------------------------------------------------------------------------------


def status_of(run_id: str, **fields: object) -> RunStatus:
    base = {"run_id": run_id, "t_start": 0.0, "t_stop": 3.0, "updated_at": utc_now()}
    return RunStatus.model_validate({**base, **fields})


@pytest.fixture
def live_id(config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]) -> str:
    fmu("3.0", "BouncingBall")
    return runs.start_run(config, supervisor, BALL, live=True, wait_s=0).run_id


def test_a_paused_view_names_the_time_the_actions_and_the_speed(
    config: Config, live_id: str
) -> None:
    view = runs.view_of(config, status_of(live_id, state="paused", t=0.25, speed=2.5))
    assert view.state is RunState.PAUSED and view.speed == 2.5 and view.t == 0.25
    assert view.hint == (
        "Paused at t=0.25. Continue with run_control('r1', action='resume'), or use 'step', "
        "'set' or 'stop'; read outputs with run_status('r1', values=[...])."
    )


def test_a_running_live_view_says_how_to_steer_it(config: Config, live_id: str) -> None:
    view = runs.view_of(config, status_of(live_id, state="running", t=1.0, speed=1.0))
    assert view.hint is not None
    assert view.hint.startswith("Poll with run_status('r1'); stop it with run_stop('r1').")
    assert "Steer it with run_control('r1', action='pause'), 'set' or 'stop'." in view.hint


def test_the_reason_of_an_ended_run_leads_the_hint(config: Config, live_id: str) -> None:
    reason = (
        "Paused for more than 60 s without a command (the idle limit), so the worker stopped "
        "the run at t=0.5 with its partial result."
    )
    stopped = runs.view_of(config, status_of(live_id, state="stopped", t=0.5, reason=reason))
    assert stopped.hint is not None and stopped.hint.startswith(
        reason + " Stopped without a result"
    )
    early = "ended early at t=1.5 of t_stop=3: the FMU ended the simulation itself"
    finished = runs.view_of(config, status_of(live_id, state="finished", t=1.5, reason=early))
    assert finished.hint is not None
    assert finished.hint.startswith("Ended early at t=1.5 of t_stop=3: the FMU ended the sim")
    assert finished.hint.endswith("run_compare against another run.")


def test_an_explicit_hint_replaces_the_computed_one(config: Config, live_id: str) -> None:
    view = runs.view_of(config, status_of(live_id, state="paused", t=0.0), hint="custom")
    assert view.hint == "custom"


def test_a_batch_view_has_no_speed_and_keeps_its_hint(
    config: Config, supervisor: Supervisor, fmu: FmuFactory, spawned: list[Path]
) -> None:
    fmu("3.0", "BouncingBall")
    run_id = runs.start_run(config, supervisor, BALL, wait_s=0).run_id
    view = runs.view_of(config, status_of(run_id, state="running", t=1.0))
    assert view.speed is None
    assert view.hint == f"Poll with run_status('{run_id}'); stop it with run_stop('{run_id}')."
