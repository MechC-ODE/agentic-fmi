"""``system_check``: every error and warning class, on tiny synthetic FMUs.

Collaborators: ``agentic_fmi.system.check_system`` and ``problems`` (under test), the
``system_fmus`` helpers and the fixtures of ``tests/system/conftest.py``. The FMUs are zips with a
``modelDescription.xml`` and, where a platform binary is wanted, an empty placeholder file; the
checks read only those, so no binary is ever loaded.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from system_fmus import (
    PI_VARIABLES,
    PLANT_VARIABLES,
    Var,
    WriteSystem,
    inp,
    loop_system,
    out,
    par,
    write_fmu,
)

from agentic_fmi.budget import json_size
from agentic_fmi.config import Config
from agentic_fmi.system import SystemCheck, check_system


def check(config: Config, write_system: WriteSystem, document: dict[str, Any]) -> SystemCheck:
    write_system(document)
    return check_system(config, "loop.json")


def with_connections(*pairs: tuple[str, str]) -> dict[str, Any]:
    return loop_system(connections=[{"from": a, "to": b} for a, b in pairs])


def only_error(result: SystemCheck) -> str:
    assert not result.ok and len(result.errors) == 1, result.errors
    return result.errors[0]


def test_a_good_loop_passes_and_warns_about_the_unconnected_setpoint(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    result = check(config, write_system, loop_system())
    assert result.ok and result.errors == [] and not result.truncated
    assert result.system == "loop.json" and result.connections == 2
    assert result.components == {"PI": "fmus/pi.fmu", "MSD": "fmus/plant.fmu"}
    assert result.experiment is not None and result.experiment.step_size == 0.01
    assert len(result.warnings) == 1
    assert "components.PI: not connected: r" in result.warnings[0]
    assert "connect it, or set a value" in result.warnings[0]


def test_a_setpoint_set_in_the_parameters_is_not_reported_unconnected(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = loop_system()
    document["components"]["PI"]["parameters"]["r"] = 1.0
    result = check(config, write_system, document)
    assert result.ok and result.warnings == []


def test_unconnected_inputs_are_listed_up_to_five_names(
    config: Config, workspace: Path, write_system: WriteSystem
) -> None:
    write_fmu(workspace / "many.fmu", [inp(f"in{i}") for i in range(8)] + [out("o")])
    document = {
        "version": 1,
        "components": {"M": {"fmu": "many.fmu"}},
        "experiment": {"stop_time": 1, "step_size": 0.1},
    }
    (warning,) = check(config, write_system, document).warnings
    assert "in0, in1, in2, in3, in4, and 3 more" in warning


# --- names -----------------------------------------------------------------------------------


def test_unknown_component_offers_close_matches_and_the_component_list(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = with_connections(("PIX.u", "MSD.F"), ("MSD.x", "PI.y"))
    error = only_error(check(config, write_system, document))
    assert error.startswith("connections[0].from 'PIX.u': unknown component 'PIX'")
    assert "Close matches: PI." in error and "Components: PI, MSD." in error


def test_unknown_variable_offers_close_matches_and_points_to_fmu_variables(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = with_connections(("PI.uu", "MSD.F"), ("MSD.x", "PI.y"))
    error = only_error(check(config, write_system, document))
    assert error.startswith("connections[0].from 'PI.uu': component PI (pi.fmu) has no variable")
    assert "'uu'. Close matches: u." in error and "fmu_variables" in error


def test_both_ends_are_checked_and_listed_separately(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = with_connections(("PIX.u", "MSD.Q"), ("MSD.x", "PI.y"))
    result = check(config, write_system, document)
    assert [e.split(":")[0] for e in result.errors] == [
        "connections[0].from 'PIX.u'",
        "connections[0].to 'MSD.Q'",
    ]


def test_a_variable_name_may_contain_dots_and_splits_at_the_first_dot(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    write_fmu(workspace / "fmus" / "dotted.fmu", [out("body.v[1]"), inp("der(x).in")])
    document = loop_system()
    document["components"]["D"] = {"fmu": "fmus/dotted.fmu"}
    document["connections"] = [
        {"from": "D.body.v[1]", "to": "PI.y"},
        {"from": "PI.u", "to": "D.der(x).in"},
    ]
    assert check(config, write_system, document).ok


# --- direction, drivers ----------------------------------------------------------------------


def test_swapped_ends_are_reported_with_the_corrected_connection(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = with_connections(("MSD.F", "PI.u"), ("MSD.x", "PI.y"))
    error = only_error(check(config, write_system, document))
    assert error.startswith("connections[0]: from and to are swapped")
    assert '{"from": "PI.u", "to": "MSD.F"}' in error


def test_from_must_be_an_output_and_to_must_be_an_input(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = with_connections(("PI.Kp", "MSD.x"), ("MSD.x", "PI.y"))
    result = check(config, write_system, document)
    assert len(result.errors) == 2
    assert "connections[0].from: PI.Kp has causality 'parameter'" in result.errors[0]
    assert "starts at an output" in result.errors[0] and "causality='output'" in result.errors[0]
    assert "connections[0].to: MSD.x has causality 'output'" in result.errors[1]
    assert "ends at an input" in result.errors[1]


def test_an_input_with_two_drivers_is_an_error_listing_both_connections(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = with_connections(("PI.u", "MSD.F"), ("MSD.x", "PI.y"), ("PI.u", "MSD.F"))
    error = only_error(check(config, write_system, document))
    assert error.startswith("MSD.F has 2 drivers: connections[0] (PI.u), connections[2] (PI.u)")
    assert "Keep one connection" in error


# --- types and units -------------------------------------------------------------------------


def test_types_must_match_by_family_and_float_widths_mix(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    write_fmu(
        workspace / "fmus" / "flags.fmu", [out("flag", type="Boolean"), out("n", type="Integer")]
    )
    write_fmu(
        workspace / "fmus" / "wide.fmu",
        [out("y", type="Float64"), inp("n", type="Int64"), inp("k", type="Int64")],
        version="3.0",
    )
    document = loop_system()
    document["components"]["FL"] = {"fmu": "fmus/flags.fmu"}
    document["components"]["W"] = {"fmu": "fmus/wide.fmu"}
    document["connections"] = [
        {"from": "FL.flag", "to": "MSD.F"},  # Boolean to Real: no
        {"from": "W.y", "to": "PI.y"},  # Float64 to Real: fine
        {"from": "FL.n", "to": "W.n"},  # Integer to Int64: fine
        {"from": "PI.u", "to": "W.k"},  # Real to Int64: no
    ]
    result = check(config, write_system, document)
    assert [e.split(":")[0] for e in result.errors] == ["connections[0]", "connections[3]"]
    assert "FL.flag is Boolean but MSD.F is Real" in result.errors[0]
    assert "converts no types" in result.errors[0]
    assert "PI.u is Real but W.k is Int64" in result.errors[1]


def test_string_binary_and_clock_variables_cannot_be_connected(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    write_fmu(workspace / "fmus" / "text.fmu", [out("s", type="String"), inp("t", type="String")])
    write_fmu(
        workspace / "fmus" / "exotic.fmu",
        [out("b", type="Binary"), Var("c", "input", "Clock"), out("f", type="Float64")],
        version="3.0",
    )
    document = loop_system()
    document["components"]["T"] = {"fmu": "fmus/text.fmu"}
    document["components"]["E"] = {"fmu": "fmus/exotic.fmu"}
    document["connections"] = [
        {"from": "T.s", "to": "T.t"},
        {"from": "E.b", "to": "PI.y"},
        {"from": "E.f", "to": "E.c"},
    ]
    result = check(config, write_system, document)
    assert [e.split(" is a ")[1].split(" ")[0] for e in result.errors] == [
        "String",
        "Binary",
        "Clock",
    ]
    assert all("Real, Integer, Boolean and Enumeration" in e for e in result.errors)


def write_plant(workspace: Path, x_unit: str | None) -> None:
    variables = [out("x", unit=x_unit) if v.name == "x" else v for v in PLANT_VARIABLES]
    write_fmu(workspace / "fmus" / "plant.fmu", variables, model="MSD")


def test_declared_units_that_differ_are_an_error(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    write_plant(workspace, "mm")
    error = only_error(check(config, write_system, loop_system()))
    assert error.startswith("connections[1]: unit mismatch: MSD.x is in 'mm' but PI.y expects 'm'")
    assert "converts no units" in error


def test_a_unit_on_one_side_only_is_a_warning(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    write_plant(workspace, None)
    result = check(config, write_system, loop_system())
    assert result.ok
    (warning,) = [w for w in result.warnings if w.startswith("connections[1]")]
    assert "PI.y declares unit 'm' but MSD.x declares none" in warning
    write_plant(workspace, "m")
    no_unit = [inp("y"), *[v for v in PI_VARIABLES if v.name != "y"]]
    write_fmu(workspace / "fmus" / "pi.fmu", no_unit, model="PI")
    (warning,) = [w for w in check_system(config, "loop.json").warnings if "declares" in w]
    assert "MSD.x declares unit 'm' but PI.y declares none" in warning


# --- components ------------------------------------------------------------------------------


def replace_pi(workspace: Path, **options: Any) -> None:
    write_fmu(workspace / "fmus" / "pi.fmu", PI_VARIABLES, model="PI", **options)


def test_a_modelexchange_only_fmu_cannot_be_a_component(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    replace_pi(workspace, interfaces=("ModelExchange",))
    error = only_error(check(config, write_system, loop_system()))
    assert error.startswith("components.PI: pi.fmu can't run here: it has no Co-Simulation")
    assert "ModelExchange only" in error and "export the model with a Co-Simulation" in error


def test_a_scheduledexecution_only_fmu_cannot_be_a_component(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    replace_pi(workspace, version="3.0", interfaces=("ScheduledExecution",))
    error = only_error(check(config, write_system, loop_system()))
    assert "components.PI: pi.fmu can't run here: it declares ScheduledExecution only" in error


def test_an_fmu_without_a_binary_for_this_platform_is_an_error(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    replace_pi(workspace, platform="win64")
    error = only_error(check(config, write_system, loop_system()))
    assert "components.PI: pi.fmu can't run here: no binary for linux64" in error
    assert "ships win64" in error
    replace_pi(workspace, platform=None)
    error = only_error(check_system(config, "loop.json"))
    assert "no binaries for any platform" in error


def test_two_instances_of_an_fmu_that_allows_only_one_per_process_are_refused(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    replace_pi(workspace, once=True)
    document = loop_system()
    document["components"]["PI2"] = {"fmu": "fmus/pi.fmu"}
    result = check(config, write_system, document)
    (error,) = [e for e in result.errors if "canBeInstantiatedOnlyOncePerProcess" in e]
    assert error.startswith("components PI, PI2: pi.fmu declares")
    assert "2 components instantiate it" in error
    # A byte-identical copy under another name is the same FMU.
    (workspace / "fmus" / "copy.fmu").write_bytes((workspace / "fmus" / "pi.fmu").read_bytes())
    document["components"]["PI2"] = {"fmu": "fmus/copy.fmu"}
    result = check(config, write_system, document)
    assert any("components PI, PI2" in e for e in result.errors)
    # One instance, or an FMU without the flag, is fine.
    del document["components"]["PI2"]
    assert check(config, write_system, document).ok
    replace_pi(workspace)
    document["components"]["PI2"] = {"fmu": "fmus/pi.fmu"}
    assert check(config, write_system, document).ok


def slave(workspace: Path, name: str, module: str, code: str) -> None:
    write_fmu(
        workspace / "fmus" / name,
        PI_VARIABLES,
        members={"resources/slavemodule.txt": module, f"resources/{module}.py": code},
    )


def slave_errors(config: Config) -> list[str]:
    return [e for e in check_system(config, "loop.json").errors if "slave module" in e]


def test_pythonfmu_fmus_with_one_slave_module_name_must_be_the_same_fmu(
    config: Config, workspace: Path, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    slave(workspace, "a.fmu", "gain", "class Gain: pass  # one")
    slave(workspace, "b.fmu", "gain", "class Gain: pass  # two")
    document = loop_system()
    document["components"]["A"] = {"fmu": "fmus/a.fmu"}
    document["components"]["B"] = {"fmu": "fmus/b.fmu"}
    write_system(document)
    (error,) = slave_errors(config)
    assert "slave module 'gain' but different content: A (a.fmu); B (b.fmu)" in error
    assert "Rename one source file" in error and "fmu_build" in error
    # The same bytes under two names are one module.
    (workspace / "fmus" / "b.fmu").write_bytes((workspace / "fmus" / "a.fmu").read_bytes())
    assert slave_errors(config) == []
    # Different module names don't clash.
    slave(workspace, "b.fmu", "other", "class Other: pass")
    assert slave_errors(config) == []


# --- the system file's own parameters --------------------------------------------------------


@pytest.fixture
def special_plant(workspace: Path, fmus: dict[str, Path]) -> None:
    """A plant with a constant, a calculated parameter, a state and a computed local."""
    write_fmu(
        workspace / "fmus" / "plant.fmu",
        [
            *PLANT_VARIABLES,
            Var("c0", "local", "Real", "constant", None, "3"),
            Var("kc", "calculatedParameter", "Real", "fixed", None, None),
            Var("x0", "local", "Real", "continuous", "exact", "1"),
            Var("tmp", "local", "Real", "continuous", "calculated", None),
            Var("fixed_out", "output", "Real", "constant", "exact", "2"),
            par("n", type="Integer", start="1"),
            par("on", type="Boolean", start="false"),
            par("label", type="String", start="a"),
        ],
        model="MSD",
    )


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        ({"mm": 1.0}, "no variable 'mm'. Close matches: m."),
        ({"m": "heavy"}, "MSD.m is a Real variable and needs a number, but the value is 'heavy'"),
        ({"m": True}, "needs a number, but the value is True"),
        ({"n": 2.5}, "MSD.n is a Integer variable and needs a whole number"),
        ({"on": 1}, "MSD.on is a Boolean variable and needs true or false"),
        ({"label": 3}, "MSD.label is a String variable and needs text"),
        ({"c0": 1.0}, "MSD.c0 is constant and can't be changed"),
        ({"kc": 1.0}, "MSD.kc is a calculated parameter"),
        ({"x": 1.0}, "MSD.x is an output, which the FMU computes"),
        ({"fixed_out": 1.0}, "MSD.fixed_out is an output, which the FMU computes"),  # initial=exact
        ({"tmp": 1.0}, "MSD.tmp is a local variable the FMU computes (initial=calculated)"),
        ({"F": 1.0}, "MSD.F is an input driven by PI.u, so the connection supplies its value"),
    ],
)
def test_system_file_parameters_are_checked_with_a_fix(
    config: Config,
    special_plant: None,
    write_system: WriteSystem,
    parameters: dict[str, Any],
    expected: str,
) -> None:
    document = loop_system()
    document["components"]["MSD"]["parameters"] = parameters
    error = only_error(check(config, write_system, document))
    assert error.startswith(f"components.MSD.parameters.{next(iter(parameters))}: ")
    assert expected in error


def test_parameters_inputs_not_driven_and_states_with_a_start_can_be_set(
    config: Config, special_plant: None, write_system: WriteSystem
) -> None:
    document = loop_system()
    document["components"]["MSD"]["parameters"] = {
        "m": 2,
        "n": 3.0,
        "on": True,
        "label": "text",
        "x0": 0.5,
    }
    document["components"]["PI"]["parameters"]["r"] = 1
    assert check(config, write_system, document).ok


def test_a_parameter_of_a_variable_the_fmu_lacks_is_an_error_not_a_crash(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = loop_system()
    document["components"]["PI"]["parameters"] = {"Kq": 1}
    error = only_error(check(config, write_system, document))
    assert error.startswith("components.PI.parameters.Kq: component PI (pi.fmu) has no variable")


def experiment_error(config: Config, write_system: WriteSystem, **experiment: float) -> str | None:
    """The one error of the closed loop with this experiment, or ``None`` if it checks out."""
    document = loop_system(experiment={"stop_time": 10, "step_size": 0.01, **experiment})
    result = check(config, write_system, document)
    assert result.ok == (not result.errors) and len(result.errors) <= 1, result.errors
    return result.errors[0] if result.errors else None


def test_a_step_longer_than_the_horizon_is_an_error(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    error = experiment_error(config, write_system, stop_time=0.5, step_size=2)
    assert error is not None
    assert error.startswith("experiment: the step 2 s exceeds the horizon 0.5 s")
    assert "so the run would take no step" in error


def test_a_start_time_other_than_zero_is_an_error_because_omsimulator_starts_at_zero(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    error = experiment_error(config, write_system, start_time=1)
    assert error is not None
    assert error.startswith("experiment: start_time 1 can't be used: OMSimulator always starts")
    assert "move stop_time instead" in error


def test_a_horizon_that_is_not_a_whole_number_of_steps_names_the_nearest_step(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    error = experiment_error(config, write_system, stop_time=1, step_size=0.3)
    assert error is not None
    assert error.startswith("experiment: the horizon 1 s is 3.33333 steps of 0.3 s, not a whole")
    assert "such as 0.333333 s (3 steps" in error and "stop_time = 0.9." in error
    # The nearest divisor can be the larger step.
    error = experiment_error(config, write_system, stop_time=1, step_size=0.45)
    assert error is not None and "such as 0.5 s (2 steps" in error
    # Floating-point noise in the division is not a remainder.
    for stop, step in ((10, 0.01), (0.3, 0.1), (3, 0.01), (1, 0.1), (0.7, 0.1), (1e6, 1)):
        assert experiment_error(config, write_system, stop_time=stop, step_size=step) is None


# --- budget ----------------------------------------------------------------------------------


def test_many_problems_are_cut_to_the_budget_and_say_so(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    pairs = [(f"PI.nope{i}", "MSD.F") for i in range(200)]
    result = check(config, write_system, with_connections(*pairs))
    assert not result.ok and result.truncated
    assert 1 <= len(result.errors) < 200
    assert json_size(result) <= config.limits.json_budget_bytes
    assert result.errors[0].startswith("connections[0].from 'PI.nope0'")


def test_warnings_are_cut_before_errors(
    config: Config, workspace: Path, write_system: WriteSystem
) -> None:
    count = 120
    write_fmu(workspace / "w.fmu", [inp(f"in_{'x' * 40}{i}") for i in range(count)] + [out("o")])
    document = {
        "version": 1,
        "components": {f"C{i}": {"fmu": "w.fmu"} for i in range(count)},
        "connections": [{"from": "C0.nope", "to": "C1.nope"}],
        "experiment": {"stop_time": 1, "step_size": 0.1},
    }
    limits = replace(config.limits, system_max_components=200)
    result = check(replace(config, limits=limits), write_system, document)
    assert result.truncated and len(result.errors) == 2  # both ends of the bad connection
    assert json_size(result) <= limits.json_budget_bytes


def test_long_messages_are_clipped(
    config: Config, workspace: Path, write_system: WriteSystem
) -> None:
    long_name = "v" * 2000
    write_fmu(workspace / "fmus" / "long.fmu", [out(long_name)])
    document = {
        "version": 1,
        "components": {"L": {"fmu": "fmus/long.fmu", "parameters": {long_name: "abc"}}},
        "experiment": {"stop_time": 1, "step_size": 0.1},
    }
    (error,) = check(config, write_system, document).errors
    assert len(error) <= config.limits.message_max_chars and error.endswith("...")
