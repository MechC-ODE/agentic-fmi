"""The ninja line and the ninja view: pure functions over a ``RunView``.

Collaborators: ``agentic_fmi.style`` (the code under test), ``agentic_fmi.runs`` (``RunView``,
``CompactSummary``) and ``agentic_fmi.store.protocol`` (``RunState``, ``RunError``). No store, no
server, no process.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentic_fmi.runs import CompactSummary, RunView
from agentic_fmi.store.protocol import ErrorCode, LiveColumn, RunError, RunState, VariableSummary
from agentic_fmi.style import NINJA_RULE, confirmation, ninja_line, ninja_view

ENDED_EARLY = (
    "Ended early at t=2.5 of t_stop=3: the FMU ended the simulation itself (its message is in "
    "log.txt). Use run_plot or run_results on 'r12', or run_compare against another run."
)


def _view(state: str, **fields: Any) -> RunView:
    defaults: dict[str, Any] = {
        "run_id": "r12",
        "state": RunState(state),
        "fmu": "3.0/BouncingBall.fmu",
        "fmi_type": "CoSimulation",
        "progress": 0.0,
    }
    return RunView(**{**defaults, **fields})


def _summary() -> CompactSummary:
    return CompactSummary(
        t_end=3.0,
        n_points=301,
        wall_time_s=0.2,
        variables=[VariableSummary(name="h", min=0.0, max=1.0, final=0.5)],
    )


@pytest.mark.parametrize(
    ("view", "line"),
    [
        (_view("queued"), "🥷 r12 in: BouncingBall queued."),
        (_view("queued", speed=1.0), "🥷 r12 in: BouncingBall queued (live)."),
        (
            _view("running", t=1.2, t_stop=3.0),
            "🥷 r12 in: BouncingBall running, t=1.2 of 3 s.",
        ),
        (_view("running"), "🥷 r12 in: BouncingBall running."),
        (
            _view("running", t=1.2, t_stop=3.0, speed=1.0),
            "🥷 r12 in: BouncingBall running (live), t=1.2 of 3 s.",
        ),
        (
            _view("paused", t=0.0, t_stop=3.0, speed=1.0),
            "🥷 r12 in: BouncingBall paused at t=0 (live).",
        ),
        (_view("finished", t=3.0, t_stop=3.0), "🥷 r12 done: BouncingBall, t=3 s."),
        (
            _view("finished", t=2.5, t_stop=3.0, hint=ENDED_EARLY),
            "🥷 r12 done: BouncingBall, t=2.5 s. Ended early (t_stop=3 s).",
        ),
        (_view("stopped", t=1.2, t_stop=3.0), "🥷 r12 stopped at t=1.2 s."),
        (_view("stopped"), "🥷 r12 stopped."),
        (
            _view("failed", error=RunError(code=ErrorCode.SIMULATION_ERROR, message="x")),
            "🥷 r12 failed: simulation_error.",
        ),
        (_view("failed"), "🥷 r12 failed."),
    ],
)
def test_the_line_of_a_run_in_every_state(view: RunView, line: str) -> None:
    assert ninja_line(view) == line


def test_a_system_run_uses_the_system_name() -> None:
    view = _view("finished", kind="system", fmu="systems/loop.json", t=10.0, t_stop=10.0)
    assert ninja_line(view) == "🥷 r12 done: loop, t=10 s."


@pytest.mark.parametrize(
    ("view", "line"),
    [
        (_view("queued", kind="build", fmu="generated/pi2/pi2.fmu"), "🥷 r12 building pi2."),
        (_view("running", kind="build", fmu="generated/pi2/pi2.fmu"), "🥷 r12 building pi2."),
        (_view("finished", kind="build", fmu="generated/pi2/pi2.fmu"), "🥷 pi2 built."),
        (
            _view("stopped", kind="build", fmu="generated/pi2/pi2.fmu"),
            "🥷 r12 build of pi2 stopped.",
        ),
        (
            _view(
                "failed",
                kind="build",
                fmu="generated/pi2/pi2.fmu",
                error=RunError(code=ErrorCode.BUILD_FAILED, message="pi2.py:7: SyntaxError"),
            ),
            "🥷 pi2 build failed: build_failed.",
        ),
    ],
)
def test_the_line_of_a_build_job(view: RunView, line: str) -> None:
    assert ninja_line(view) == line


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (3.0, "3"),
        (1.2, "1.2"),
        (0.1 + 0.2, "0.3"),
        (1.23456, "1.235"),
        (0.000123456, "0.0001235"),
        (12345.0, "12340"),
        (-0.0, "0"),
        (1.0e-12, "1e-12"),
    ],
)
def test_numbers_have_at_most_four_significant_digits(value: float, text: str) -> None:
    view = _view("stopped", t=value)
    assert ninja_line(view) == f"🥷 r12 stopped at t={text} s."


def test_the_ninja_view_drops_the_summary_the_build_facts_and_the_log_tail() -> None:
    error = RunError(code=ErrorCode.SIMULATION_ERROR, message="solver failed", log_tail="x" * 500)
    view = _view("failed", error=error, summary=_summary(), build={"sha256": "a" * 64})
    ninja = ninja_view(view)
    assert ninja.summary is None and ninja.build is None
    assert ninja.error is not None
    assert ninja.error.code is ErrorCode.SIMULATION_ERROR
    assert ninja.error.message == "solver failed" and ninja.error.log_tail is None
    assert ninja.hint == f"{ninja_line(view)} {NINJA_RULE}"
    assert view.summary is not None and view.error is error  # the input is not changed


def test_the_ninja_view_of_a_finished_run_is_the_line_and_the_rule() -> None:
    view = _view("finished", t=3.0, t_stop=3.0, progress=1.0, summary=_summary())
    ninja = ninja_view(view)
    assert ninja.summary is None
    assert ninja.hint is not None and ninja.hint.startswith("🥷 r12 done: BouncingBall, t=3 s. ")
    assert ninja.hint.endswith("reply with this line only; give details only if the user asks.")
    assert (ninja.run_id, ninja.state, ninja.fmu, ninja.t, ninja.progress) == (
        "r12",
        RunState.FINISHED,
        "3.0/BouncingBall.fmu",
        3.0,
        1.0,
    )


def test_the_ninja_view_keeps_the_values_that_were_asked_for() -> None:
    values = [LiveColumn(name="h", unit="m", value=0.5)]
    view = _view("running", t=1.0, t_stop=3.0, speed=1.0, values=values)
    assert ninja_view(view).values == values
    assert ninja_view(_view("running")).values is None


def test_the_ninja_view_of_a_build_has_no_facts() -> None:
    view = _view("finished", kind="build", fmu="generated/pi2/pi2.fmu", build={"sha256": "a" * 64})
    ninja = ninja_view(view)
    assert ninja.build is None and ninja.kind == "build"
    assert ninja.hint is not None and ninja.hint.startswith("🥷 pi2 built. ")


def test_the_confirmations() -> None:
    assert confirmation("ninja") == "🥷 Ninja mode on: status lines only."
    assert confirmation("talkative") == "Talkative mode on: full summaries."
