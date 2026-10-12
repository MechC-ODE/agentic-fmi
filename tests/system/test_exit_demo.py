"""The closed-loop demo as a test: the closed loop of the two ``fmu-authoring`` examples (PI
controller and mass-spring-damper) is built, wired by the ``system-builder`` example
``loop.json``, checked, run, tuned to an overshoot under 5 %, exported as an SSP and imported
again, with real workers and the real OMSimulator.

Collaborators: ``skills/fmu-authoring/examples/`` and ``skills/system-builder/examples/loop.json``
(copied, never modified, into a temporary workspace), ``agentic_fmi.runs`` (``start_build``,
``start_run``, ``compare_runs``, ``plot_runs``), ``agentic_fmi.system`` (``check_system``),
``agentic_fmi.ssp`` (``export_ssp``, ``import_ssp``) and ``agentic_fmi.store.runstore`` (the
result tables). The overshoot is read from each run's summary (the maximum of ``plant.x``), as
Claude reads it; the series are read only to compare runs bit for bit. Needs ``omc`` for the
plant (6 to 20 s to build) and OMSimulator for the runs (a few seconds each).

The numbers (measured in spikes (c) and (g) and pinned here): with Kp = 10 and Ki = 15 the
loop overshoots by 10.42 % at the communication step h = 0.01, by 7.88 % at h = 0.001 (7.84 % is
the offline model's value) and by 7.56 % in continuous time, because OMSimulator couples
Jacobi-style and the loop gains up to a step of delay.

The tuned pair, Kp = 8 and Ki = 12, was picked offline with :func:`sampled_loop`, the exact
sampled-data model of this loop (the PI recurrence of ``pi_controller.py``, the plant's
zero-order-hold discretization, one step of delay), searching Kp in 1..15 and Ki in 8..30 at
h = 0.01. Why this pair:

- Kp alone can't do it: with Ki = 15, Kp = 8 gives 8.9 % and Kp = 5 gives 10.5 %, and a higher Kp
  is worse (15 gives 17.7 %).
- Ki is the lever for the overshoot (the integrator keeps charging while the mass is still
  moving): with Kp = 10, Ki = 13 gives 6.0 % and Ki = 12 gives 3.7 %.
- Trimming Kp as well gives the best margin: (8, 12) overshoots 2.0 % at h = 0.01 and 0.43 % at
  h = 0.001, reaches the 2 % band around the setpoint after 5.05 s (6.07 s at (10, 15) and
  (10, 12)), ends within 0.1 % of the setpoint at t = 10 s, and every neighbour with Kp or Ki
  changed by one stays under 5 % (the worst, (7, 13), gives 4.7 %), so the verdict doesn't hang
  on a rounding error.
"""

from __future__ import annotations

import os
import shutil
import signal
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from agentic_fmi import runs
from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.runs import RunView
from agentic_fmi.ssp import NAMESPACES, export_ssp, import_ssp
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState
from agentic_fmi.supervisor import Supervisor
from agentic_fmi.system import check_system

pytestmark = [
    pytest.mark.omc,
    pytest.mark.oms,
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("omc") is None, reason="omc (OpenModelica) is not installed"),
]

SKILLS = Path(__file__).resolve().parents[2] / "skills"
EXAMPLES = SKILLS / "fmu-authoring" / "examples"
LOOP = SKILLS / "system-builder" / "examples" / "loop.json"

SETPOINT = 1.0
BASE_GAINS = (10.0, 15.0)
TUNED_GAINS = (8.0, 12.0)
"""``(Kp, Ki)`` of the example file and of the tuned loop (see the module docstring)."""
OVERSHOOT_LIMIT = 5.0
"""The goal, in percent of the setpoint."""
M, K, D = 1.0, 10.0, 2.0
"""``MSD.mo``'s mass, stiffness and damping."""


# --- the offline model --------------------------------------------------------------------------


def expm(matrix: np.ndarray) -> np.ndarray:
    """Matrix exponential by scaling and squaring with a Taylor series (no SciPy needed)."""
    squarings = max(0, int(np.ceil(np.log2(max(np.linalg.norm(matrix, 1), 1e-16)))) + 1)
    scaled = matrix / 2**squarings
    term = result = np.eye(len(matrix))
    for n in range(1, 25):
        term = term @ scaled / n
        result = result + term
    for _ in range(squarings):
        result = result @ result
    return result


def plant_step(h: float) -> tuple[np.ndarray, np.ndarray]:
    """``(Ad, Bd)`` of the mass-spring-damper for a force held over one step of ``h``."""
    augmented = np.array([[0.0, 1.0, 0.0], [-K / M, -D / M, 1.0 / M], [0.0, 0.0, 0.0]])
    exact = expm(augmented * h)
    return exact[:2, :2], exact[:2, 2]


def sampled_loop(kp: float, ki: float, h: float, stop: float = 10.0) -> np.ndarray:
    """The plant position ``x`` at the communication points of the Jacobi-coupled loop.

    Both components step from point k with the inputs exchanged at k: the PI computes
    ``u[k+1]`` from ``x[k]`` (``pi_controller.py``: the integral advances before the output is
    computed), the plant advances with the force ``u[k]`` held. Row k is the sampled state after
    the exchange, as the engine records it.
    """
    steps = round(stop / h)
    a, b = plant_step(h)
    x, u, integral = np.zeros((steps + 1, 2)), np.zeros(steps + 1), 0.0
    for k in range(steps):
        error = SETPOINT - x[k, 0]
        integral += error * h
        u[k + 1] = kp * error + ki * integral
        x[k + 1] = a @ x[k] + b * u[k]
    return x[:, 0]


def model_overshoot(kp: float, ki: float, h: float) -> float:
    """The offline model's overshoot, in percent of the setpoint."""
    return float((sampled_loop(kp, ki, h).max() - SETPOINT) / SETPOINT * 100)


# --- the workspace ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Demo:
    """A workspace with the built examples and ``loop.json``, and the objects that serve it."""

    config: Config
    supervisor: Supervisor

    def finish(self, view: RunView) -> RunView:
        assert view.state is RunState.FINISHED, view
        return view

    def build(self, source: str) -> None:
        self.finish(runs.start_build(self.config, self.supervisor, source, wait_s=60))

    def run(self, system: str = "loop.json", **kwargs: Any) -> RunView:
        """``run_start(system=...)`` with a generous wait; the run must finish."""
        view = runs.start_run(
            self.config, self.supervisor, None, system=system, wait_s=120, **kwargs
        )
        return self.finish(view)

    def table(self, view: RunView) -> np.ndarray:
        """The run's whole ``result.csv`` as a structured array."""
        return runstore.read_result(runstore.run_dir(self.config.store_dir, view.run_id))

    def spec(self, view: RunView) -> Any:
        return runstore.read_job_spec(runstore.run_dir(self.config.store_dir, view.run_id))


def overshoot_percent(view: RunView) -> float:
    """The overshoot of ``plant.x`` as Claude reads it: from the summary's maximum."""
    assert view.summary is not None
    x = next(v for v in view.summary.variables if v.name == "plant.x")
    assert x.max is not None
    return (x.max - SETPOINT) / SETPOINT * 100


UNIT_WARNINGS = ["connections[0]: plant.F", "connections[1]: plant.x"]
"""What ``system_check`` warns about in the example: the plant declares units (N, m) and the
controller declares none, so a value crosses unchanged. Not a problem here."""


def unit_warnings(warnings: list[str]) -> list[str]:
    """The ends of the unit warnings, asserting that they are all there is."""
    assert all(" declares unit " in warning for warning in warnings), warnings
    return [warning.split(" declares unit")[0] for warning in warnings]


def final_value(view: RunView) -> float:
    assert view.summary is not None
    final = next(v.final for v in view.summary.variables if v.name == "plant.x")
    assert final is not None
    return final


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    """A workspace with both examples built by ``fmu_build`` and the example ``loop.json``."""
    workspace = (tmp_path_factory.mktemp("exit-demo") / "workspace").resolve()
    (workspace / "models").mkdir(parents=True)
    for example in ("MSD.mo", "pi_controller.py"):
        shutil.copyfile(EXAMPLES / example, workspace / "models" / example)
    shutil.copyfile(LOOP, workspace / "loop.json")
    config = replace(load_config(workspace), timing=Timing(poll_interval_s=0.05))
    supervisor = Supervisor(config)
    lab = Demo(config, supervisor)
    lab.build("models/pi_controller.py")
    lab.build("models/MSD.mo")
    yield lab
    for child in list(supervisor._children.values()):
        if child.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGKILL)
        child.process.wait(timeout=10)


@pytest.fixture(scope="module")
def baseline(demo: Demo) -> RunView:
    """The example file as it is: Kp = 10, Ki = 15, h = 0.01."""
    return demo.run()


@pytest.fixture(scope="module")
def fine(demo: Demo) -> RunView:
    """The same loop with a tenth of the step, h = 0.001."""
    return demo.run(output_interval=0.001)


@pytest.fixture(scope="module")
def tuned(demo: Demo) -> RunView:
    """The tuned gains through ``start_values``, h = 0.01."""
    kp, ki = TUNED_GAINS
    return demo.run(start_values={"PI.Kp": kp, "PI.Ki": ki})


# --- the demo -----------------------------------------------------------------------------------


def test_the_example_file_is_copied_unchanged_and_the_check_passes(demo: Demo) -> None:
    assert (demo.config.workspace / "loop.json").read_bytes() == LOOP.read_bytes()
    check = check_system(demo.config, "loop.json")
    assert check.ok and check.errors == []
    assert unit_warnings(check.warnings) == UNIT_WARNINGS
    assert check.components == {
        "PI": "generated/pi_controller/pi_controller.fmu",
        "plant": "generated/MSD/MSD.fmu",
    }
    assert check.connections == 2


def test_the_loop_at_h_0_01_overshoots_10_percent(demo: Demo, baseline: RunView) -> None:
    assert (baseline.kind, baseline.fmu) == ("system", "loop.json")
    assert overshoot_percent(baseline) == pytest.approx(10.42, abs=0.1)
    assert final_value(baseline) == pytest.approx(SETPOINT, abs=0.01)  # the integrator tracks r
    spec = demo.spec(baseline)
    assert spec.master.step_size == 0.01 and spec.experiment.stop_time == 10
    assert {ref: p.source for ref, p in spec.parameters.items()} == {
        "PI.Kp": "system",
        "PI.Ki": "system",
        "PI.r": "system",
    }


def test_a_smaller_step_changes_the_loop_to_7_88_percent(fine: RunView) -> None:
    assert fine.summary is not None and fine.summary.n_points == 10001
    assert overshoot_percent(fine) == pytest.approx(7.88, abs=0.02)


def test_the_offline_model_predicts_the_real_runs(
    baseline: RunView, fine: RunView, tuned: RunView
) -> None:
    """The model that picked the gains reproduces the engine's loop, so its pick can be trusted.
    The plant FMU integrates with CVode at a finite tolerance, so the model is a little off:
    0.01 points at h = 0.01, 0.04 at h = 0.001 (7.88 % measured against 7.84 % modelled)."""
    kp, ki = BASE_GAINS
    assert overshoot_percent(baseline) == pytest.approx(model_overshoot(kp, ki, 0.01), abs=0.05)
    assert overshoot_percent(fine) == pytest.approx(model_overshoot(kp, ki, 0.001), abs=0.1)
    kp, ki = TUNED_GAINS
    assert overshoot_percent(tuned) == pytest.approx(model_overshoot(kp, ki, 0.01), abs=0.05)


def test_the_tuned_pair_is_not_a_knife_edge_in_the_model() -> None:
    kp, ki = TUNED_GAINS
    neighbours = [
        model_overshoot(kp + dkp, ki + dki, 0.01) for dkp in (-1, 0, 1) for dki in (-1, 0, 1)
    ]
    assert max(neighbours) < OVERSHOOT_LIMIT
    assert model_overshoot(kp, ki, 0.001) < OVERSHOOT_LIMIT  # nor does it need this step
    # Kp alone doesn't do it: with Ki = 15 no Kp in 1..15 gets near the limit.
    assert min(model_overshoot(gain, BASE_GAINS[1], 0.01) for gain in range(1, 16)) > 8.0


def test_tuning_kp_and_ki_brings_the_overshoot_under_5_percent(demo: Demo, tuned: RunView) -> None:
    assert overshoot_percent(tuned) < OVERSHOOT_LIMIT
    assert final_value(tuned) == pytest.approx(SETPOINT, abs=0.005)
    spec = demo.spec(tuned)
    assert {ref: (p.value, p.source) for ref, p in spec.parameters.items()} == {
        "PI.Kp": (8.0, "start_values"),
        "PI.Ki": (12.0, "start_values"),
        "PI.r": (1.0, "system"),
    }


def test_run_compare_reports_the_parameter_differences(
    demo: Demo, baseline: RunView, fine: RunView, tuned: RunView
) -> None:
    ids = [baseline.run_id, fine.run_id, tuned.run_id]
    comparison = runs.compare_runs(demo.config, ids, ["plant.x"])
    differences = {d.field: d.values for d in comparison.spec_differences}
    assert differences["parameters.PI.Kp"] == {ids[0]: 10.0, ids[1]: 10.0, ids[2]: 8.0}
    assert differences["parameters.PI.Ki"] == {ids[0]: 15.0, ids[1]: 15.0, ids[2]: 12.0}
    assert differences["master.step_size"] == {ids[0]: 0.01, ids[1]: 0.001, ids[2]: 0.01}
    (variable,) = comparison.variables
    assert variable.name == "plant.x"
    maxima = {metrics.run_id: metrics.max for metrics in variable.runs}
    assert maxima[ids[0]] == pytest.approx(1.1042, abs=1e-3)
    assert maxima[ids[2]] < 1 + OVERSHOOT_LIMIT / 100


def test_run_plot_returns_a_png(demo: Demo, baseline: RunView, tuned: RunView) -> None:
    plot = runs.plot_runs(demo.config, [baseline.run_id, tuned.run_id], ["plant.x"])
    assert plot.png.startswith(bytes.fromhex("89504e470d0a1a0a"))
    assert plot.path.is_file() and plot.path.read_bytes() == plot.png


TUNED_SSV = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<ssv:ParameterSet xmlns:ssv="{NAMESPACES["ssv"]}" version="1.0" name="tuned">'
    "<ssv:Parameters>"
    '<ssv:Parameter name="PI.Kp"><ssv:Real value="8"/></ssv:Parameter>'
    '<ssv:Parameter name="PI.Ki"><ssv:Real value="12"/></ssv:Parameter>'
    "</ssv:Parameters></ssv:ParameterSet>\n"
)


def test_the_tuned_system_exported_and_imported_runs_identically(
    demo: Demo, tuned: RunView
) -> None:
    workspace = demo.config.workspace
    (workspace / "tuned.ssv").write_text(TUNED_SSV, encoding="utf-8")

    by_paramset = demo.run(paramset="tuned.ssv")
    paramset_spec = demo.spec(by_paramset)
    assert {ref: p.source for ref, p in paramset_spec.parameters.items()} == {
        "PI.Kp": "paramset",
        "PI.Ki": "paramset",
        "PI.r": "system",
    }
    assert np.array_equal(demo.table(by_paramset), demo.table(tuned))

    exported = export_ssp(demo.config, "loop.json", "exports/tuned.ssp", paramset="tuned.ssv")
    assert (workspace / exported.path).is_file() and set(exported.fmus) == {"PI", "plant"}
    imported = import_ssp(demo.config, exported.path, "tuned")
    assert imported.system == "imported/tuned/tuned.json"
    assert unit_warnings(imported.warnings) == UNIT_WARNINGS
    again = demo.run(system=imported.system)

    original, copy = demo.table(tuned), demo.table(again)
    assert original.dtype.names == copy.dtype.names
    assert set(original.dtype.names or ()) == {"time", "PI.u", "plant.x", "plant.v"}
    for column in original.dtype.names or ():
        assert np.array_equal(original[column], copy[column]), column
    assert overshoot_percent(again) == overshoot_percent(tuned) < OVERSHOOT_LIMIT
    shas = {name: c.fmu.sha256 for name, c in demo.spec(tuned).components.items()}
    assert {name: c.fmu.sha256 for name, c in demo.spec(again).components.items()} == shas
