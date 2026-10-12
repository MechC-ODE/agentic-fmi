"""The live gate's one wait loop (ADR 0010, ``live.LiveControl``) on a fake clock: the pause at
t0, resume, pacing with re-anchoring, single steps, early ends of a step sequence, sets checked
in the worker, chart selections (applied or rejected, never a stop or a pause), stop, the idle
limit, waiting excluded from the watchdog's clock, and the final sweep (exactly one outcome per
command file).

Collaborators: ``agentic_fmi.live`` (under test), ``agentic_fmi.store.inbox`` (outcomes),
``agentic_fmi.worker.ActiveClock`` (the watchdog's clock), ``live_support`` (server and pane
commands), the ``new_live_run`` fixture (a real run directory and spec of BouncingBall 2.0 CS).
The engine is a :class:`FakeLive`; nothing loads an FMU binary.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from live_support import command_events, descriptions_of, send, send_pane

from agentic_fmi.config import Limits, Timing
from agentic_fmi.errors import SetRefused
from agentic_fmi.live import LiveBuffer, LiveControl, _already_applied, live_columns
from agentic_fmi.store import inbox, runstore
from agentic_fmi.store.protocol import RunState
from agentic_fmi.worker import ActiveClock

H = 0.01
"""BouncingBall's communication step in these runs."""


class Clock:
    """A monotonic clock that moves only when slept or advanced, running scheduled actions."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0
        self.sleeps: list[float] = []
        self._actions: list[tuple[float, Callable[[], Any]]] = []

    def __call__(self) -> float:
        return self.now

    def at(self, delay: float, action: Callable[[], Any]) -> None:
        """Run ``action`` once the clock has moved ``delay`` seconds from now."""
        self._actions.append((self.now + delay, action))
        self._actions.sort(key=lambda item: item[0])

    def sleep(self, seconds: float) -> None:
        assert 0 <= seconds <= 0.05 + 1e-12, seconds  # never longer than one wait slice
        self.sleeps.append(seconds)
        self.slept += seconds
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        assert self.now < 1000.0 + 100_000, "the gate never returned"
        while self._actions and self._actions[0][0] <= self.now:
            _, action = self._actions.pop(0)
            action()


class FakeLive:
    """The engine's live handle: BouncingBall's values; a set takes its values in order and raises
    ``SetRefused`` (listing what it applied) at the first name in ``refuse``, as the engines do."""

    def __init__(self, refuse: tuple[str, ...] = ()) -> None:
        self.values = {"h": 1.0, "v": 0.0, "e": 0.7, "g": -9.81}
        self.refuse = refuse
        self.sets: list[dict[str, Any]] = []

    def get(self, names: Any) -> list[float | None]:
        return [self.values.get(name, 0.0) for name in names]

    def set(self, values: Any) -> None:
        applied: dict[str, Any] = {}
        for name, value in values.items():
            if name in self.refuse:
                raise SetRefused(f"the FMU refused to set {name}: fmi2Error", applied)
            applied[name] = self.values[name] = value
        self.sets.append(applied)


@dataclass
class Rig:
    """One live run directory with its gate on a fake clock."""

    run_path: Path
    control: LiveControl
    clock: Clock
    live: FakeLive
    waits: list[bool]
    stop: list[bool] = field(default_factory=list)

    def gate(self, t: float) -> bool:
        return self.control.gate(t, self.live)

    def later(self, delay: float, action: str, **arguments: Any) -> list[str]:
        """Send a server command after ``delay`` fake seconds; the list gets its ID."""
        sent: list[str] = []
        self.clock.at(delay, lambda: sent.append(send(self.run_path, action, **arguments)))
        return sent

    def outcome(self, command_id: str) -> Any:
        found = inbox.read_outcome(self.run_path, command_id)
        assert found is not None, f"no outcome for {command_id}"
        return found


@pytest.fixture
def rig(fmu: Callable[[str, str], Path], new_live_run: Callable[..., Path]) -> Callable[..., Rig]:
    """``rig(idle_limit_s=60, timing=Timing(), refuse=(), model="BouncingBall")`` builds a
    :class:`Rig` on a Reference FMU 2.0 (``model``)."""

    def build(
        idle_limit_s: float = 60.0,
        timing: Timing | None = None,
        refuse: tuple[str, ...] = (),
        model: str = "BouncingBall",
    ) -> Rig:
        path = fmu("2.0", model)
        run_path = new_live_run(path, idle_limit_s, stop_time=3.0, output_interval=H)
        spec = runstore.read_spec(run_path)
        descriptions = descriptions_of(path)
        limits = Limits()
        buffer = LiveBuffer(spec.run_id, live_columns(spec, descriptions, limits), limits)
        clock, waits, stop = Clock(), [], []
        control = LiveControl(
            run_path,
            spec,
            descriptions,
            buffer,
            stop_requested=lambda: bool(stop),
            on_wait=waits.append,
            timing=timing or Timing(),
            limits=limits,
            now=clock,
            sleep=clock.sleep,
        )
        return Rig(run_path, control, clock, FakeLive(refuse), waits, stop)

    return build


def test_the_run_waits_paused_at_t0_until_resumed(rig: Callable[..., Rig]) -> None:
    r = rig()
    assert r.control.view().state is RunState.RUNNING  # before the gate is reached
    seen: list[RunState] = []
    r.clock.at(0.2, lambda: seen.append(r.control.view().state))
    resume = r.later(0.3, "resume")
    assert r.gate(0.0) is True
    assert seen == [RunState.PAUSED]
    assert 0.3 <= r.clock.slept <= 0.3 + 0.1  # seen within one poll after it was written
    assert r.waits == [True, False]
    outcome = r.outcome(resume[0])
    assert (outcome.status, outcome.action, outcome.t) == ("applied", "resume", 0.0)
    view = r.control.view()
    assert (view.state, view.speed, view.last_cmd) == (RunState.RUNNING, 1.0, resume[0])
    [event] = command_events(r.run_path.parent.parent, r.run_path.name)
    assert resume[0] in event["message"] and "applied" in event["message"]


def test_pacing_follows_the_speed_and_re_anchors_on_resume_and_speed_change(
    rig: Callable[..., Rig],
) -> None:
    r = rig()
    r.later(0.1, "resume")  # speed 1: one step of 0.01 s per 0.01 s of wall time
    assert r.gate(0.0)
    start = r.clock.now
    for k in range(1, 11):
        assert r.gate(k * H)
    assert r.clock.now - start == pytest.approx(10 * H, abs=1e-9)

    r.later(0.0, "pause")
    r.clock.advance(0.2)  # the next poll is due
    pause_wall = r.clock.now
    r.later(5.0, "resume", speed=2.0)
    assert r.gate(11 * H)  # paused here for 5 s, then resumed at twice real time
    resumed = r.clock.now
    assert resumed - pause_wall == pytest.approx(5.0, abs=0.06)
    for k in range(12, 22):
        assert r.gate(k * H)
    # re-anchored at the resume: no catching up for the 5 s pause, then 10 steps at 2x
    assert r.clock.now - resumed == pytest.approx(10 * H / 2, abs=1e-9)
    assert r.control.view().speed == 2.0


def test_a_slow_step_is_not_waited_for(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.later(0.1, "resume")
    assert r.gate(0.0)
    r.clock.advance(0.5)  # a step that took longer than its paced wall time
    slept = r.clock.slept
    assert r.gate(H)
    assert r.clock.slept == slept


def test_step_n_takes_exactly_n_unpaced_steps_then_pauses(rig: Callable[..., Rig]) -> None:
    r = rig()
    step = r.later(0.1, "step", n=3)
    assert r.gate(0.0)
    slept = r.clock.slept
    assert r.gate(H) and r.gate(2 * H)
    assert r.clock.slept == slept  # single steps are not paced
    assert r.control.view().state is RunState.RUNNING
    assert inbox.read_outcome(r.run_path, step[0]) is None  # written at the n-th step
    resume = r.later(1.0, "resume")
    assert r.gate(3 * H)  # the third step: paused again until the resume
    outcome = r.outcome(step[0])
    assert (outcome.status, outcome.t, outcome.message) == ("applied", 3 * H, None)
    assert r.clock.slept - slept >= 1.0
    assert r.outcome(resume[0]).t == 3 * H


def test_a_resume_during_steps_ends_them_early(rig: Callable[..., Rig]) -> None:
    r = rig()
    step = r.later(0.1, "step", n=10)
    assert r.gate(0.0)
    resume = r.later(0.0, "resume")
    for k in range(1, 4):
        r.clock.advance(0.04)  # each step takes 40 ms; the inbox is polled every 0.1 s
        assert r.gate(k * H)
    outcome = r.outcome(step[0])
    assert (
        outcome.status == "applied"
        and outcome.message == "3 of 10 steps before a resume ended them"
    )
    assert r.outcome(resume[0]).status == "applied"
    assert r.control.view().state is RunState.RUNNING


def test_step_is_refused_while_running_and_while_stepping(rig: Callable[..., Rig]) -> None:
    r = rig()
    first = r.later(0.1, "step", n=5)
    second = r.later(0.1, "step", n=1)
    assert r.gate(0.0)
    assert r.outcome(second[0]).status == "rejected"
    assert "still running (0 of 5 steps)" in (r.outcome(second[0]).message or "")
    resume = r.later(0.0, "resume")
    r.clock.advance(0.2)
    assert r.gate(H)
    assert r.outcome(first[0]).message == "1 of 5 steps before a resume ended them"
    assert r.outcome(resume[0]).status == "applied"
    third = r.later(0.0, "step", n=1)
    r.clock.advance(0.2)
    assert r.gate(2 * H)
    rejected = r.outcome(third[0])
    assert rejected.status == "rejected" and "pause it first" in (rejected.message or "")


def test_pause_resume_notes_and_the_speed_limit(rig: Callable[..., Rig]) -> None:
    r = rig()
    pause = r.later(0.1, "pause")
    too_fast = r.later(0.2, "resume", speed=5000.0)
    resume = r.later(0.3, "resume", speed=10.0)
    again = r.later(0.3, "resume", speed=20.0)
    assert r.gate(0.0)
    assert r.outcome(pause[0]).message == "already paused"
    assert r.outcome(too_fast[0]).status == "rejected"
    assert "over the limit of 1000" in (r.outcome(too_fast[0]).message or "")
    assert r.outcome(resume[0]).message is None
    assert r.outcome(again[0]).message == "already running; speed set to 20"
    assert r.control.view().speed == 20.0


def test_sets_are_checked_in_the_worker_and_a_bad_one_leaves_the_run_going(
    rig: Callable[..., Rig],
) -> None:
    r = rig(refuse=("v",))
    good = r.later(0.1, "set", values={"e": 0.6})
    fixed_id: list[str] = []
    r.clock.at(0.1, lambda: fixed_id.append(send_pane(r.run_path, "set", values={"g": 1})))
    out_of_range = r.later(0.1, "set", values={"e": 2.0})
    unknown = r.later(0.1, "set", values={"ee": 0.5})
    resume = r.later(0.3, "resume")
    assert r.gate(0.0)
    assert r.outcome(good[0]).status == "applied" and r.live.sets == [{"e": 0.6}]
    refused_fixed = r.outcome(fixed_id[0])
    assert (refused_fixed.status, refused_fixed.source) == ("rejected", "pane")
    assert "fixed parameter" in (refused_fixed.message or "")
    assert "outside its declared range [0.5, 1]" in (r.outcome(out_of_range[0]).message or "")
    assert "Close matches: e" in (r.outcome(unknown[0]).message or "")
    assert r.outcome(resume[0]).status == "applied"
    assert r.live.sets == [{"e": 0.6}]


def test_an_engine_refusal_is_a_rejected_set(rig: Callable[..., Rig]) -> None:
    r = rig(refuse=("e",))
    refused = r.later(0.1, "set", values={"e": 0.6})
    r.later(0.2, "resume")
    assert r.gate(0.0)
    outcome = r.outcome(refused[0])
    assert outcome.status == "rejected" and "refused to set e" in (outcome.message or "")
    assert "already applied" not in (outcome.message or "")  # nothing was set before it


def test_a_set_refused_partway_names_the_values_already_applied(rig: Callable[..., Rig]) -> None:
    r = rig(model="Feedthrough", refuse=("Float64_continuous_input",))
    values = {"Float64_tunable_parameter": 8.0, "Int32_input": 3, "Float64_continuous_input": 1.5}
    refused = r.later(0.1, "set", values=values)
    r.later(0.2, "resume")
    assert r.gate(0.0)
    outcome = r.outcome(refused[0])
    assert outcome.status == "rejected"
    assert outcome.message == (
        "the FMU refused to set Float64_continuous_input: fmi2Error; "
        "already applied: Float64_tunable_parameter=8, Int32_input=3"
    )
    assert r.live.values["Float64_tunable_parameter"] == 8.0  # they really stayed set
    assert "Float64_continuous_input" not in r.live.values
    assert r.control.view().state is RunState.RUNNING  # and the run went on


def test_the_applied_values_in_a_rejection_are_formatted_and_capped() -> None:
    assert _already_applied({}) == ""
    assert _already_applied({"a": 8.0, "b": True, "c": 3, "d": 0.1234567891234}) == (
        "; already applied: a=8, b=true, c=3, d=0.1234567891"
    )
    many = {f"v{i}": float(i) for i in range(13)}
    assert _already_applied(many).endswith("v9=9 and 3 more")


def test_a_stop_command_stops_and_rejects_what_follows(rig: Callable[..., Rig]) -> None:
    r = rig()
    stop = r.later(0.1, "stop")
    after = r.later(0.1, "resume")
    assert r.gate(0.0) is False
    assert r.outcome(stop[0]).status == "applied"
    late = r.outcome(after[0])
    assert late.status == "rejected" and stop[0] in (late.message or "")
    assert r.control.reason is None


def test_a_stop_request_is_seen_within_one_slice(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.clock.at(1.0, lambda: r.stop.append(True))
    assert r.gate(0.0) is False
    assert 1.0 <= r.clock.slept <= 1.0 + 0.05 + 1e-9
    assert r.waits == [True, False]


def test_the_idle_limit_counts_paused_time_only(rig: Callable[..., Rig]) -> None:
    r = rig(idle_limit_s=2.0)
    r.later(1.5, "resume")  # a command restarts the idle count
    assert r.gate(0.0)
    r.clock.advance(30.0)  # running time never counts
    assert r.gate(H)
    r.later(0.0, "pause")
    r.clock.advance(0.2)
    paused_at = r.clock.now
    assert r.gate(2 * H) is False
    assert r.clock.now - paused_at == pytest.approx(2.0, abs=0.06)
    reason = r.control.reason
    assert reason is not None and "idle limit" in reason and "t=0.02" in reason


def test_waiting_is_excluded_from_the_watchdog_clock(rig: Callable[..., Rig]) -> None:
    r = rig(idle_limit_s=1000.0)
    watchdog = ActiveClock(now=r.clock)
    r.control.on_wait = lambda waiting: watchdog.pause() if waiting else watchdog.resume()
    r.later(100.0, "resume")  # paused at t0 for 100 s
    assert r.gate(0.0)
    for k in range(1, 101):
        r.clock.advance(0.004)  # each step computes for 4 ms, paced to 10 ms
        assert r.gate(k * H)
    assert r.clock.now - 1000.0 > 100.0
    assert watchdog.elapsed() == pytest.approx(100 * 0.004, abs=1e-6)


def test_finish_ends_a_step_and_rejects_every_leftover_once(rig: Callable[..., Rig]) -> None:
    r = rig()
    step = r.later(0.1, "step", n=5)
    assert r.gate(0.0)
    assert r.gate(H) and r.gate(2 * H)
    left = [send(r.run_path, "pause"), send_pane(r.run_path, "resume")]
    torn = r.run_path / inbox.COMMANDS_DIR / "9999999999999-pane-0000000f.json"
    torn.write_text('{"protocol": 1')
    r.control.finish(2 * H)
    assert r.outcome(step[0]).message == "2 of 5 steps before the run ended"
    for command_id in left:
        outcome = r.outcome(command_id)
        assert outcome.status == "rejected" and "run ended at t=0.02" in (outcome.message or "")
    unreadable = r.outcome(torn.stem)
    assert unreadable.action is None and unreadable.status == "rejected"
    commands = sorted(p.stem for p in (r.run_path / inbox.COMMANDS_DIR).glob("*.json"))
    outcomes = sorted(p.stem for p in (r.run_path / inbox.OUTCOMES_DIR).glob("*.json"))
    assert commands == outcomes
    events = command_events(r.run_path.parent.parent, r.run_path.name)
    assert len(events) == len(commands)
    r.control.finish(2 * H)  # a second sweep finds nothing new
    assert len(command_events(r.run_path.parent.parent, r.run_path.name)) == len(commands)


def test_the_inbox_is_not_listed_more_often_than_its_periods(
    rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    r = rig(timing=replace(Timing(), inbox_poll_s=0.1, live_wait_slice_s=0.05))
    listings: list[float] = []
    real_scandir = os.scandir

    def counting_scandir(path: Any) -> Any:
        listings.append(r.clock.now)
        return real_scandir(path)

    monkeypatch.setattr(inbox.os, "scandir", counting_scandir)
    r.later(1.0, "resume", speed=1000.0)
    assert r.gate(0.0)
    paused_listings = len(listings)
    assert 18 <= paused_listings <= 22  # every 50 ms slice while paused for 1 s
    for k in range(1, 201):
        r.clock.advance(0.005)
        assert r.gate(k * H)
    assert len(listings) - paused_listings <= 11  # at most every 0.1 s while stepping (1 s)


def test_the_buffer_is_sampled_at_the_pause(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.later(0.1, "resume")
    r.live.values["h"] = 0.75
    assert r.gate(0.0)
    snapshot = r.control.buffer.snapshot()
    assert snapshot is not None and snapshot.t == 0.0
    assert {column.name: column.value for column in snapshot.values} == {"h": 0.75, "v": 0.0}
    assert snapshot.time == [0.0] and snapshot.series == {"h": [0.75], "v": [0.0]}
    assert r.control.buffer.snapshot() is None  # nothing new since


def test_a_chart_while_paused_selects_signals_and_changes_nothing_else(
    rig: Callable[..., Rig],
) -> None:
    r = rig()
    chart = r.later(0.1, "chart", names=["v"])
    seen: list[tuple[RunState, float, tuple[str, ...]]] = []
    r.clock.at(
        0.3,
        lambda: seen.append(
            (r.control.view().state, r.control.view().speed, r.control.buffer.charted)
        ),
    )
    resume = r.later(0.5, "resume")
    assert r.gate(0.0) is True  # a chart never falls through to stop
    outcome = r.outcome(chart[0])
    assert (outcome.status, outcome.action, outcome.message) == ("applied", "chart", None)
    assert seen == [(RunState.PAUSED, 1.0, ("v",))]  # still paused, same speed, new selection
    assert r.outcome(resume[0]).status == "applied" and r.control.reason is None
    snapshot = r.control.buffer.snapshot()
    assert snapshot is not None and snapshot.series == {"v": [0.0]}
    assert [c.name for c in snapshot.values] == ["h", "v"]
    [first, second] = command_events(r.run_path.parent.parent, r.run_path.name)
    assert "chart applied" in first["message"] and "resume applied" in second["message"]


def test_a_rejected_chart_leaves_the_selection_and_the_run_as_they_were(
    rig: Callable[..., Rig],
) -> None:
    r = rig()
    unknown = r.later(0.1, "chart", names=["vv"])
    too_many = r.later(0.1, "chart", names=["h", "v", "e", "g"])
    resume = r.later(0.3, "resume")
    assert r.gate(0.0) is True
    rejected = r.outcome(unknown[0])
    assert rejected.status == "rejected" and "Close matches: v" in (rejected.message or "")
    assert "at most 3 signals" in (r.outcome(too_many[0]).message or "")
    assert r.control.buffer.charted == ("h", "v")
    assert r.outcome(resume[0]).status == "applied" and r.control.reason is None


def test_a_chart_does_not_end_a_step_sequence_or_change_the_speed(rig: Callable[..., Rig]) -> None:
    r = rig()
    step = r.later(0.1, "step", n=5)
    assert r.gate(0.0)
    chart = r.later(0.0, "chart", names=[])
    for k in range(1, 5):
        r.clock.advance(0.04)  # each step takes 40 ms; the inbox is polled every 0.1 s
        assert r.gate(k * H)
    assert r.outcome(chart[0]).status == "applied" and r.control.buffer.charted == ()
    assert inbox.read_outcome(r.run_path, step[0]) is None  # the steps go on
    assert r.control.view().state is RunState.RUNNING
    resume = r.later(1.0, "resume")
    assert r.gate(5 * H)  # the fifth step: the sequence ends and the run pauses again
    ended = r.outcome(step[0])
    assert (ended.status, ended.message) == ("applied", None)
    assert r.outcome(resume[0]).status == "applied"
    assert r.control.view().speed == 1.0


def test_a_chart_while_running_keeps_the_run_running_at_its_speed(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.later(0.1, "resume", speed=10.0)
    assert r.gate(0.0)
    chart = r.later(0.0, "chart", names=["v", "h"])
    r.clock.advance(0.2)
    assert r.gate(H)
    assert r.outcome(chart[0]).status == "applied"
    assert r.control.buffer.charted == ("v", "h")
    assert (r.control.view().state, r.control.view().speed) == (RunState.RUNNING, 10.0)
    assert r.control.reason is None


def test_a_chart_counts_as_a_command_for_the_idle_limit(rig: Callable[..., Rig]) -> None:
    r = rig(idle_limit_s=2.0)
    r.later(1.5, "chart", names=["v"])  # restarts the idle count like any command
    start = r.clock.now
    assert r.gate(0.0) is False
    assert r.clock.now - start == pytest.approx(1.5 + 2.0, abs=0.15)
    reason = r.control.reason
    assert reason is not None and "idle limit" in reason


def test_the_history_is_kept_for_a_column_charted_later(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.later(0.1, "resume", speed=1000.0)
    assert r.gate(0.0)
    for k in range(1, 6):
        r.live.values["v"] = -float(k)
        assert r.gate(k * H)
    chart = r.later(0.0, "chart", names=["v"])
    r.clock.advance(0.2)  # the next poll is due
    r.live.values["v"] = -6.0
    assert r.gate(6 * H)
    assert r.outcome(chart[0]).status == "applied"
    snapshot = r.control.buffer.snapshot()
    assert snapshot is not None and list(snapshot.series) == ["v"]
    assert snapshot.series["v"] == [0.0, -1.0, -2.0, -3.0, -4.0, -5.0, -6.0]
    assert len(snapshot.time) == 7
