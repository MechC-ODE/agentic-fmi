"""Comparing and plotting system runs: spec differences on parameter values, legend labels such
as ``PI.Kp=12``, results through the shared result tools, and a guard that every
``SystemSpec`` field is either compared or ignored on purpose.

Collaborators: ``agentic_fmi.analysis.compare`` (``diff_specs``, ``compare_runs`` on
``SystemSpec``; under test), ``agentic_fmi.runs`` (``compare_runs``, ``plot_runs``,
``get_results`` on runs fabricated by the ``add_system_run`` fixture) and ``system_fmus``
(``system_spec``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel
from system_fmus import AddSystemRun, system_spec

from agentic_fmi import runs
from agentic_fmi.analysis.compare import compare_runs, diff_specs
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store.protocol import (
    Experiment,
    FmuRef,
    RunSpec,
    RunState,
    SystemSpec,
    Versions,
    utc_now,
)

PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


def differences(*specs: SystemSpec) -> dict[str, dict[str, object]]:
    return {d.field: dict(d.values) for d in diff_specs(specs)}


# --- diff_specs ------------------------------------------------------------------------------


def test_identical_system_specs_have_no_differences() -> None:
    a = system_spec("r1", {"PI.Kp": 10.0})
    assert diff_specs([a, system_spec("r2", {"PI.Kp": 10.0})]) == []


def test_parameter_values_are_listed_first_as_parameters_component_variable() -> None:
    a = system_spec("r1", {"PI.Kp": 10.0, "PI.Ki": 1.0}, step=0.01)
    b = system_spec("r2", {"PI.Kp": 12.0, "PI.Ki": 1.0}, step=0.02, stop_time=2.0)
    fields = [d.field for d in diff_specs([a, b])]
    assert fields == ["parameters.PI.Kp", "master.step_size", "experiment.stop_time"]
    assert differences(a, b)["parameters.PI.Kp"] == {"r1": 10.0, "r2": 12.0}


def test_a_parameter_set_in_one_run_only_is_unset_in_the_other() -> None:
    a = system_spec("r1", {"PI.Kp": 10.0})
    b = system_spec("r2", {"PI.Kp": 10.0, "MSD.m": 2.0})
    assert differences(a, b) == {"parameters.MSD.m": {"r1": None, "r2": 2.0}}


def test_the_layer_that_set_a_value_is_not_a_difference() -> None:
    a = system_spec("r1", {"PI.Kp": 10.0}, source="system")
    b = system_spec("r2", {"PI.Kp": 10.0}, source="start_values")
    assert diff_specs([a, b]) == []


def test_the_step_is_one_difference_not_two() -> None:
    a, b = system_spec("r1", step=0.01), system_spec("r2", step=0.1)
    assert list(differences(a, b)) == ["master.step_size"]


def test_connections_are_compared_as_a_sorted_set() -> None:
    a = system_spec("r1", connections=(("PI.u", "MSD.F"), ("MSD.x", "PI.y")))
    reordered = system_spec("r2", connections=(("MSD.x", "PI.y"), ("PI.u", "MSD.F")))
    rewired = system_spec("r3", connections=(("PI.u", "MSD.F"), ("MSD.v", "PI.y")))
    assert diff_specs([a, reordered]) == []
    (difference,) = diff_specs([a, rewired])
    assert difference.field == "connections"
    assert difference.values["r3"] == '["MSD.v->PI.y","PI.u->MSD.F"]'


def test_fmus_files_and_installs_are_compared_by_hash() -> None:
    a = system_spec("r1")
    b = system_spec("r2", fmu_sha="d", system_sha="e", oms_sha="f", paramset_sha="1")
    assert set(differences(a, b)) == {
        "components.PI.sha256",
        "components.MSD.sha256",
        "system.sha256",
        "paramset.sha256",
        "omsimulator.sha256",
    }
    assert "system.path" not in differences(a, b)  # same name, same place


COMPARED = frozenset(
    {
        "kind",
        "name",
        "system.sha256",
        "paramset.sha256",
        "components.PI.fmu.sha256",
        "components.MSD.fmu.sha256",
        "connections",
        "parameters.PI.Kp.value",
        "master.step_size",
        "experiment.start_time",
        "experiment.stop_time",
        "timeout_s",
        "force",
        "live",  # a live run's result also depends on its commands (ADR 0010)
        *(f"versions.{field}" for field in Versions.model_fields),
        *(f"omsimulator.{field}" for field in ("version", "sha256")),
    }
)
"""Every leaf of a system spec that ``diff_specs`` compares."""

IGNORED = frozenset(
    {
        "protocol",  # the same constant in every spec
        "run_id",
        "created_at",
        "estimated_steps",  # derived from the experiment and the step
        "idle_limit_s",  # can only end a live run, never change its trajectory
        "outputs",  # the recorded variables; a missing one is a note instead
        "system.path",  # the file's content (sha256) decides, not where it was
        "system.size_bytes",
        "paramset.path",
        "paramset.size_bytes",
        "parameters.PI.Kp.source",  # the layer that set a value doesn't change the result
        "master.method",  # single-valued Literals today: compare them once they can differ
        "master.coupling",
        "experiment.output_interval",  # equal to master.step_size
        "experiment.relative_tolerance",  # null for systems
        "experiment.step_size",
        *(
            f"components.{name}.fmu.{field}"  # the FMU's sha256 decides, not where it was
            for name in ("PI", "MSD")
            for field in ("path", "size_bytes", "fmi_version", "model_name")
        ),
    }
)
"""Every leaf of a system spec that ``diff_specs`` deliberately leaves out."""


def leaves(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Every leaf of a spec as (key path, value); a list is one leaf. Dictionary keys such as
    ``PI.Kp`` stay one path element."""
    if isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from leaves(getattr(node, name), (*path, name))
    elif isinstance(node, dict):
        for key, item in node.items():
            yield from leaves(item, (*path, key))
    else:
        yield path, node


def with_leaf(node: Any, path: tuple[str, ...], value: Any) -> Any:
    """A copy of ``node`` with the leaf at ``path`` replaced (nothing is validated)."""
    if not path:
        return value
    head, *rest = path
    if isinstance(node, dict):
        return {**node, head: with_leaf(node[head], tuple(rest), value)}
    return node.model_copy(update={head: with_leaf(getattr(node, head), tuple(rest), value)})


def other(value: Any) -> Any:
    """A different value of the same kind."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, str):
        return value[:-1] + ("0" if value[-1] != "0" else "1")
    if isinstance(value, datetime):
        return value + timedelta(seconds=1)
    if isinstance(value, list):
        return [*value, value[0]]
    return 1.0  # a null number


def test_every_system_spec_field_is_compared_or_explicitly_ignored() -> None:
    """A new ``SystemSpec`` field fails here until it is added to ``COMPARED`` (and to
    ``compare._flatten_system``) or to ``IGNORED``; each entry is also checked to behave so."""
    base = system_spec("r1", {"PI.Kp": 10.0}, paramset_sha="d").model_copy(
        update={"estimated_steps": 100.0}
    )
    found = {".".join(path): (path, value) for path, value in leaves(base)}
    assert COMPARED.isdisjoint(IGNORED)
    unclassified = set(found) - COMPARED - IGNORED
    assert not unclassified, (
        f"classify these SystemSpec fields in compare.py and here: {unclassified}"
    )
    assert set(found) == COMPARED | IGNORED, "a listed field is no longer in SystemSpec"
    for name, (path, value) in found.items():
        changed = with_leaf(base, path, other(value))
        assert bool(diff_specs([base, changed])) is (name in COMPARED), name


def test_a_single_fmu_run_and_a_system_run_can_be_compared() -> None:
    single = RunSpec(
        run_id="r1",
        created_at=utc_now(),
        fmu=FmuRef(
            path="/w/a.fmu", sha256="a" * 64, size_bytes=1, fmi_version="2.0", model_name="A"
        ),
        fmi_type="CoSimulation",
        experiment=Experiment(start_time=0, stop_time=1, output_interval=0.01),
        outputs=["x"],
        timeout_s=60,
        versions=Versions.current(),
    )
    system = system_spec("r2", {"PI.Kp": 1.0})
    found = {d.field for d in diff_specs([single, system])}
    assert {"kind", "fmu.sha256", "parameters.PI.Kp", "components.PI.sha256"} <= found
    time = np.linspace(0, 1, 11)
    table = np.empty(11, dtype=[("time", float), ("x", float)])
    table["time"], table["x"] = time, time
    comparison = compare_runs([single, system], {"r1": table, "r2": table}, ["x"])
    assert any("different FMUs" in note for note in comparison.notes)


# --- run_compare, run_results, run_plot -------------------------------------------------------


def test_run_compare_reports_the_tuned_parameter(
    config: Config, add_system_run: AddSystemRun
) -> None:
    a = add_system_run({"PI.Kp": 10.0, "PI.Ki": 1.0})
    b = add_system_run({"PI.Kp": 12.0, "PI.Ki": 1.0}, columns={"MSD.x": lambda t: 2.0 - np.exp(-t)})
    comparison = runs.compare_runs(config, [a, b])
    assert [(d.field, d.values) for d in comparison.spec_differences] == [
        ("parameters.PI.Kp", {a: 10.0, b: 12.0})
    ]
    (variable,) = comparison.variables
    assert variable.name == "MSD.x" and variable.unit == "m"
    base, other = variable.runs
    assert base.max_abs_diff is None
    assert other.max_abs_diff == pytest.approx(1.0)  # the offset between the two curves
    assert comparison.notes == []


def test_runs_of_different_fmus_are_noted(config: Config, add_system_run: AddSystemRun) -> None:
    a, b = add_system_run(sha="a"), add_system_run(sha="b")
    comparison = runs.compare_runs(config, [a, b])
    assert {d.field for d in comparison.spec_differences} == {
        "components.PI.sha256",
        "components.MSD.sha256",
    }
    assert any("different FMUs (see spec_differences components)" in n for n in comparison.notes)


def test_legend_labels_name_the_parameters_that_differ(
    config: Config, add_system_run: AddSystemRun
) -> None:
    a = add_system_run({"PI.Kp": 10.0, "PI.Ki": 1.0})
    b = add_system_run({"PI.Kp": 12.0, "PI.Ki": 2.5})
    c = add_system_run({"PI.Kp": 12.0, "PI.Ki": 2.5, "MSD.m": 2.0})
    specs = [runs._load_ready(config, run_id).spec for run_id in (a, b, c)]
    assert runs._legend_labels(specs[:2]) == {
        a: f"{a} (PI.Ki=1, PI.Kp=10)",
        b: f"{b} (PI.Ki=2.5, PI.Kp=12)",
    }
    labels = runs._legend_labels(specs)
    assert labels[c] == f"{c} (PI.Ki=2.5, PI.Kp=12, MSD.m=2)"
    assert labels[a] == f"{a} (PI.Ki=1, PI.Kp=10, MSD.m=unset)"


def test_legend_labels_leave_out_hashes_versions_and_timeouts(
    config: Config, add_system_run: AddSystemRun
) -> None:
    a = add_system_run({"PI.Kp": 10.0}, system_sha="c")
    b = add_system_run({"PI.Kp": 10.0}, system_sha="d")
    specs = [runs._load_ready(config, run_id).spec for run_id in (a, b)]
    assert {d.field for d in diff_specs(specs)} == {"system.sha256"}
    assert runs._legend_labels(specs) == {a: a, b: b}


def test_run_plot_draws_system_runs_with_parameter_labels(
    config: Config, add_system_run: AddSystemRun
) -> None:
    a = add_system_run({"PI.Kp": 10.0})
    b = add_system_run({"PI.Kp": 12.0})
    plot = runs.plot_runs(config, [a, b], variables=["MSD.x"])
    assert plot.png.startswith(PNG_SIGNATURE) and len(plot.png) <= config.limits.plot_max_bytes
    assert plot.path.parent == config.store_dir / "runs" / a / "plots"
    assert plot.path.name == f"MSD.x__{a}+{b}.png"
    assert plot.caption.startswith(f"MSD.x for {a}, {b}; saved to ")


def test_run_results_reads_a_system_runs_columns(
    config: Config, add_system_run: AddSystemRun
) -> None:
    run_id = add_system_run({"PI.Kp": 10.0}, columns={"MSD.x": lambda t: t, "PI.u": lambda t: -t})
    results = runs.get_results(config, run_id, variables=["PI.u"], max_points=10)
    assert [s.name for s in results.series] == ["PI.u"] and results.n_points_total == 101
    assert results.series[0].unit == "m"
    default = runs.get_results(config, run_id)
    assert [s.name for s in default.series] == ["MSD.x", "PI.u"]


def test_an_unfinished_system_run_has_no_results_yet(
    config: Config, add_system_run: AddSystemRun
) -> None:
    running = add_system_run(state=RunState.RUNNING)
    with pytest.raises(UserError, match=rf"Run {running} is running; only finished or stopped"):
        runs.get_results(config, running)
    stopped = add_system_run(state=RunState.STOPPED)
    assert runs.get_results(config, stopped).run_id == stopped
