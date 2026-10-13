"""The ready-made Python examples of the ``fmu-authoring`` skill, built exactly as shipped and run:
the bouncing ball (``bouncing_ball.py``) and the Python twin of the mass-spring-damper
(``MSD.py``). They are what Claude builds when a user asks for an example, so they must build
offline, without OpenModelica, validate, and behave as the physics says, first time.

Collaborators: ``skills/fmu-authoring/examples/`` and ``skills/system-builder/examples/loop.json``
(copied, never modified, into a temporary workspace), ``agentic_fmi.runs`` (``start_build``,
``start_run``), ``agentic_fmi.fmu.validate`` and ``agentic_fmi.store.runstore``, with
``tests/builds/build_support.py``. Each example builds once (about a second each); the closed-loop
test runs OMSimulator and is marked ``oms``.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from build_support import TEST_TIMING, kill_leftovers

from agentic_fmi import runs
from agentic_fmi.config import Config, load_config
from agentic_fmi.fmu.validate import validate_fmu
from agentic_fmi.runs import RunView
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import BuildRecord, Constant, RunState
from agentic_fmi.supervisor import Supervisor

SKILLS = Path(__file__).resolve().parents[2] / "skills"
EXAMPLES = SKILLS / "fmu-authoring" / "examples"
LOOP = SKILLS / "system-builder" / "examples" / "loop.json"
SOURCES = ("bouncing_ball.py", "MSD.py", "pi_controller.py")
BALL_FMU = "generated/bouncing_ball/bouncing_ball.fmu"
MSD_FMU = "generated/MSD/MSD.fmu"

G, E, H0 = 9.81, 0.7, 1.0
"""The ball's gravity (magnitude), restitution and start height, as shipped."""
FIRST_IMPACT = math.sqrt(2 * H0 / G)


@dataclass(frozen=True)
class Lab:
    """A workspace holding the Python examples and ``loop.json``, and the objects serving it."""

    config: Config
    supervisor: Supervisor

    def build(self, source: str) -> RunView:
        view = runs.start_build(self.config, self.supervisor, source, wait_s=60)
        assert view.state is RunState.FINISHED, view
        return view

    def run(self, fmu: str | None, **kwargs: Any) -> RunView:
        view = runs.start_run(self.config, self.supervisor, fmu, wait_s=120, **kwargs)
        assert view.state is RunState.FINISHED, view
        return view

    def record(self, name: str) -> BuildRecord:
        record = runstore.read_build_record(self.config.generated_dir / name)
        assert record is not None
        return record

    def table(self, view: RunView, *columns: str) -> np.ndarray:
        return runstore.read_result(self.config.store_dir / "runs" / view.run_id, columns)


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    workspace = (tmp_path_factory.mktemp("python-examples") / "workspace").resolve()
    (workspace / "models").mkdir(parents=True)
    for source in SOURCES:
        shutil.copyfile(EXAMPLES / source, workspace / "models" / source)
    shutil.copyfile(LOOP, workspace / "loop.json")
    config = replace(load_config(workspace), timing=TEST_TIMING)
    supervisor = Supervisor(config)
    yield Lab(config, supervisor)
    kill_leftovers(supervisor, workspace)


@pytest.fixture(scope="module")
def ball(lab: Lab) -> RunView:
    return lab.build("models/bouncing_ball.py")


@pytest.fixture(scope="module")
def msd(lab: Lab) -> RunView:
    return lab.build("models/MSD.py")


# --- the bouncing ball --------------------------------------------------------------------------


def test_the_ball_builds_offline_with_the_reference_names_and_validates(
    lab: Lab, ball: RunView
) -> None:
    assert ball.fmu == BALL_FMU
    record = lab.record("bouncing_ball")
    assert record.backend == "pythonfmu" and record.fmu.model_identifier == "BouncingBall"
    assert record.summary.outputs == ["h", "v"]
    assert record.summary.parameters == ["e", "g"]  # tunable parameters come first
    report = validate_fmu(lab.config, BALL_FMU)
    assert report.valid and report.problems == []


def test_the_ball_falls_on_the_exact_parabola_bounces_and_comes_to_rest(
    lab: Lab, ball: RunView
) -> None:
    view = lab.run(BALL_FMU, outputs=["h", "v"])
    table = lab.table(view, "h", "v")
    t, h = table["time"], table["h"]
    falling = t < FIRST_IMPACT
    assert np.allclose(h[falling], H0 - G * t[falling] ** 2 / 2, atol=1e-9)
    assert h.min() >= 0.0
    # The first bounce leaves at e times the impact speed, so its apex is e^2 times the drop.
    second_impact = FIRST_IMPACT + 2 * E * math.sqrt(2 * G * H0) / G
    first_arc = (t > FIRST_IMPACT) & (t < second_impact)
    assert h[first_arc].max() == pytest.approx(E**2 * H0, abs=2e-4)
    # The bounces shorten geometrically and end before 3 s: the ball rests on the floor.
    assert (h[-1], table["v"][-1]) == (0.0, 0.0)


def test_a_lower_restitution_gives_a_lower_bounce(lab: Lab, ball: RunView) -> None:
    view = lab.run(BALL_FMU, start_values={"e": 0.5}, outputs=["h"], stop_time=1.0)
    table = lab.table(view, "h")
    after = table["time"] > FIRST_IMPACT
    assert table["h"][after].max() == pytest.approx(0.25 * H0, abs=2e-4)


# --- the Python mass-spring-damper --------------------------------------------------------------

M, K, D, FORCE = 2.0, 20.0, 4.0, 2.0
"""As in ``test_build_examples.py``: poles -1 +- 3i, x settles at F/k = 0.1."""


def test_the_python_msd_builds_offline_to_the_modelica_msd_name_and_validates(
    lab: Lab, msd: RunView
) -> None:
    assert msd.fmu == MSD_FMU
    record = lab.record("MSD")
    assert record.backend == "pythonfmu" and record.fmu.model_identifier == "MSD"
    assert record.summary.inputs == ["F"] and record.summary.outputs == ["x", "v"]
    assert record.summary.parameters == ["m", "k", "d"]
    report = validate_fmu(lab.config, MSD_FMU)
    assert report.valid and report.problems == []


def test_the_python_msd_follows_the_analytic_step_response_at_a_coarse_step(
    lab: Lab, msd: RunView
) -> None:
    view = lab.run(
        MSD_FMU,
        start_values={"m": M, "k": K, "d": D},
        inputs={"F": Constant(value=FORCE)},
        outputs=["x"],
        output_interval=0.1,
    )
    table = lab.table(view, "x")
    t = table["time"]
    analytic = FORCE / K * (1 - np.exp(-t) * (np.cos(3 * t) + np.sin(3 * t) / 3))
    assert np.max(np.abs(table["x"] - analytic)) < 1e-9


@pytest.mark.oms
def test_the_example_loop_runs_with_the_python_msd_and_overshoots_as_documented(
    lab: Lab, msd: RunView
) -> None:
    """The system-builder skill quotes 10.4 % at h = 0.01 for this loop (measured with MSD.mo);
    the Python twin gives the same number, so the documented tuning holds for both."""
    lab.build("models/pi_controller.py")
    view = lab.run(None, system="loop.json")
    assert view.summary is not None
    x = next(v for v in view.summary.variables if v.name == "plant.x")
    assert x.max is not None and (x.max - 1.0) * 100 == pytest.approx(10.42, abs=0.01)
