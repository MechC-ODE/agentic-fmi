"""The two ``fmu-authoring`` skill examples, built exactly as shipped and run: the PI controller
(``pi_controller.py``, pythonfmu) and the mass-spring-damper plant (``MSD.mo``, omc).

Collaborators: ``skills/fmu-authoring/examples/`` (copied, never modified, into a temporary
workspace), ``agentic_fmi.runs`` (``start_build``, ``start_run``), ``agentic_fmi.fmu.validate``
(``fmu_validate``), ``agentic_fmi.store.runstore`` (build records, run specs and result tables)
and ``tests/builds/build_support.py``. The workspace, supervisor and builds are module-scoped, so
each example is built once (about 1 s and 7 s). The Modelica tests are marked ``omc`` and
skipped without ``omc``; the PI tests need only pythonfmu.

Finding pinned here: FMPy's validator rejects a pythonfmu FMU whose output has no ``initial``
attribute (``ModelStructure/InitialUnknowns`` is missing, because pythonfmu never writes one).
The shipped ``pi_controller.py`` declares ``initial=Fmi2Initial.exact`` on its output and
validates; a regression test builds it without that argument to show the problem.
"""

from __future__ import annotations

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
from agentic_fmi.store.protocol import BuildRecord, Constant, RunState, Step
from agentic_fmi.supervisor import Supervisor

EXAMPLES = Path(__file__).resolve().parents[2] / "skills" / "fmu-authoring" / "examples"
PI_FMU = "generated/pi_controller/pi_controller.fmu"
MSD_FMU = "generated/MSD/MSD.fmu"

NO_INITIAL_UNKNOWNS = (
    "ModelStructure/InitialUnknowns does not contain the expected set of variables. "
    "Expected {u} but was {}."
)
"""What FMPy says about a pythonfmu output without ``initial=exact``: no InitialUnknowns."""

omc_missing = pytest.mark.skipif(
    shutil.which("omc") is None, reason="omc (OpenModelica) is not installed"
)


@dataclass(frozen=True)
class Lab:
    """A workspace holding the two examples, and the server objects that serve it."""

    config: Config
    supervisor: Supervisor

    def build(self, source: str) -> RunView:
        """``fmu_build``, failing the test unless the build finished."""
        view = runs.start_build(self.config, self.supervisor, source, wait_s=60)
        assert view.state is RunState.FINISHED, view
        return view

    def run(self, fmu: str, **kwargs: Any) -> RunView:
        """``run_start`` with a generous wait, failing the test unless the run finished."""
        view = runs.start_run(self.config, self.supervisor, fmu, wait_s=60, **kwargs)
        assert view.state is RunState.FINISHED, view
        return view

    def record(self, name: str) -> BuildRecord:
        record = runstore.read_build_record(self.config.generated_dir / name)
        assert record is not None
        return record

    def spec_sha256(self, view: RunView) -> str:
        """The FMU hash the run's ``spec.json`` pinned."""
        return runstore.read_spec(self.config.store_dir / "runs" / view.run_id).fmu.sha256

    def table(self, view: RunView, *columns: str) -> np.ndarray:
        """The run's recorded ``columns`` (and ``time``) as a structured array."""
        return runstore.read_result(self.config.store_dir / "runs" / view.run_id, columns)


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Lab]:
    """A workspace with ``models/MSD.mo`` and ``models/pi_controller.py`` copied from the skill."""
    workspace = (tmp_path_factory.mktemp("examples") / "workspace").resolve()
    (workspace / "models").mkdir(parents=True)
    for example in ("MSD.mo", "pi_controller.py"):
        shutil.copyfile(EXAMPLES / example, workspace / "models" / example)
    config = replace(load_config(workspace), timing=TEST_TIMING)
    supervisor = Supervisor(config)
    yield Lab(config, supervisor)
    kill_leftovers(supervisor, workspace)


@pytest.fixture(scope="module")
def pi_built(lab: Lab) -> RunView:
    return lab.build("models/pi_controller.py")


@pytest.fixture(scope="module")
def msd_built(lab: Lab) -> RunView:
    if shutil.which("omc") is None:
        pytest.skip("omc (OpenModelica) is not installed")
    return lab.build("models/MSD.mo")


def test_the_examples_are_copied_unchanged(lab: Lab) -> None:
    for example in ("MSD.mo", "pi_controller.py"):
        copy = lab.config.workspace / "models" / example
        assert copy.read_bytes() == (EXAMPLES / example).read_bytes()


# --- the PI controller (pythonfmu) --------------------------------------------------------------


def test_the_pi_controller_builds_to_its_name(lab: Lab, pi_built: RunView) -> None:
    assert pi_built.fmu == PI_FMU and (lab.config.workspace / PI_FMU).is_file()
    record = lab.record("pi_controller")
    assert record.backend == "pythonfmu" and record.fmu.interfaces == ["CoSimulation"]
    assert record.fmu.model_identifier == "PIController"
    assert (record.summary.inputs, record.summary.outputs) == (["y"], ["u"])
    assert record.summary.parameters == ["Kp", "Ki", "r"]


def test_the_shipped_pi_controller_validates_with_no_problems(lab: Lab, pi_built: RunView) -> None:
    report = validate_fmu(lab.config, PI_FMU)
    assert report.valid and report.problems == []


def test_a_pythonfmu_output_without_initial_exact_fails_validation(lab: Lab) -> None:
    text = (EXAMPLES / "pi_controller.py").read_text(encoding="utf-8")
    naive = text.replace(', "initial": Fmi2Initial.exact', "")
    assert naive != text and '"initial"' not in naive
    (lab.config.workspace / "models" / "pi_naive.py").write_text(naive, encoding="utf-8")
    view = lab.build("models/pi_naive.py")
    report = validate_fmu(lab.config, view.fmu)
    assert report.problems == [NO_INITIAL_UNKNOWNS] and not report.valid


def test_the_pi_controller_with_ki_zero_is_a_proportional_controller_one_step_late(
    lab: Lab, pi_built: RunView
) -> None:
    kp, r = 10.0, 1.0
    view = lab.run(
        PI_FMU,
        start_values={"Kp": kp, "Ki": 0.0, "r": r},
        inputs={"y": Step(time=0.5, before=0.0, after=0.25)},
        outputs=["y", "u"],
        stop_time=1.0,
        output_interval=0.01,
    )
    assert lab.spec_sha256(view) == lab.record("pi_controller").fmu.sha256
    table = lab.table(view, "y", "u")
    time, y, u = table["time"], table["y"], table["u"]
    assert u[0] == 0.0  # the output is computed in do_step: nothing before the first step
    # Afterwards u = Kp*(r - y), with y either as recorded or one sample earlier (the lag).
    now, late = kp * (r - y[1:]), kp * (r - y[:-1])
    assert np.all(np.isclose(u[1:], now) | np.isclose(u[1:], late))
    assert np.allclose(u[(time > 0) & (time <= 0.5)], kp * r)
    assert np.allclose(u[time >= 0.52], kp * (r - 0.25))


# --- the mass-spring-damper plant (omc) ---------------------------------------------------------

M, K, D, FORCE = 2.0, 20.0, 4.0, 2.0
"""Poles of 2 s^2 + 4 s + 20 = 0 are -1 +- 3i: the step response settles with e^-t, and x
ends at F/k = 0.1 well inside the default experiment (10 s)."""


@pytest.mark.omc
@omc_missing
def test_the_msd_builds_to_its_name_and_validates(lab: Lab, msd_built: RunView) -> None:
    assert msd_built.fmu == MSD_FMU and (lab.config.workspace / MSD_FMU).is_file()
    record = lab.record("MSD")
    assert record.backend == "omc" and record.fmu.interfaces == ["CoSimulation", "ModelExchange"]
    assert record.fmu.model_identifier == "MSD"
    assert record.summary.inputs == ["F"] and sorted(record.summary.outputs) == ["v", "x"]
    assert sorted(record.summary.parameters) == ["d", "k", "m"]
    assert record.warnings == []
    report = validate_fmu(lab.config, MSD_FMU)
    assert report.valid, report.problems


@pytest.mark.omc
@omc_missing
@pytest.mark.parametrize("fmi_type", ["ModelExchange", "CoSimulation"])
def test_the_msd_settles_at_force_over_stiffness_in_both_interfaces(
    lab: Lab, msd_built: RunView, fmi_type: str
) -> None:
    view = lab.run(
        MSD_FMU,
        fmi_type=fmi_type,
        start_values={"m": M, "k": K, "d": D},
        inputs={"F": Step(time=1.0, before=0.0, after=FORCE)},
        outputs=["x", "v"],
    )
    assert view.fmi_type == fmi_type
    assert lab.spec_sha256(view) == lab.record("MSD").fmu.sha256
    table = lab.table(view, "x", "v")
    assert table["x"][-1] == pytest.approx(FORCE / K, rel=0.01)
    assert abs(table["v"][-1]) < 1e-3
    assert table["x"][0] == 0.0 and np.all(table["x"][table["time"] <= 1.0] == 0.0)


@pytest.mark.omc
@omc_missing
@pytest.mark.parametrize(
    ("fmi_type", "interval"),
    [("ModelExchange", 0.01), ("CoSimulation", 0.1)],
    ids=["ME", "CS-coarse"],
)
def test_the_msd_follows_the_analytic_step_response_even_at_a_coarse_communication_step(
    lab: Lab, msd_built: RunView, fmi_type: str, interval: float
) -> None:
    """A forward-Euler CS FMU is 3e-2 off at a 0.1 s step (spike (f)); CVode is well under
    1e-3, so this fails if the build lost ``--fmiFlags=s:cvode``."""
    view = lab.run(
        MSD_FMU,
        fmi_type=fmi_type,
        start_values={"m": M, "k": K, "d": D},
        inputs={"F": Constant(value=FORCE)},
        outputs=["x"],
        output_interval=interval,
    )
    table = lab.table(view, "x")
    t = table["time"]
    analytic = FORCE / K * (1 - np.exp(-t) * (np.cos(3 * t) + np.sin(3 * t) / 3))
    assert np.max(np.abs(table["x"] - analytic)) < 1e-3
