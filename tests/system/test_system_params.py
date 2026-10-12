"""Parameter layering for system runs: the recorded precedence, type conversion, and what may be
set.

Collaborators: ``agentic_fmi.system.resolve_parameters`` (under test), ``Paramset`` (built by hand,
standing in for what ``ssp.read_paramset`` returns), the ``system_fmus`` helpers and the fixtures
of ``tests/system/conftest.py``. Precedence, lowest first: the FMU's own start values (implicit),
the system file, the ``.ssv`` parameter set, ``start_values``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from system_fmus import PLANT_VARIABLES, Var, WriteSystem, loop_system, par, write_fmu

from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store.protocol import FileRef, ResolvedValue, StartValue
from agentic_fmi.system import Paramset, load_system, resolve_parameters


def paramset(values: Mapping[str, StartValue], units: Mapping[str, str] | None = None) -> Paramset:
    ref = FileRef(path="/workspace/p.ssv", sha256="d" * 64, size_bytes=1)
    return Paramset(ref=ref, values=values, units=units or {})


def resolve(
    config: Config,
    write_system: WriteSystem,
    *,
    document: Mapping[str, Any] | None = None,
    layer: Paramset | None = None,
    start_values: Mapping[str, StartValue] | None = None,
) -> dict[str, ResolvedValue]:
    write_system(document)
    return resolve_parameters(load_system(config, "loop.json"), layer, start_values)


def document_with(**parameters: dict[str, Any]) -> dict[str, Any]:
    """The closed loop whose components have exactly the given parameters (none by default)."""
    document = loop_system()
    for name, component in document["components"].items():
        component["parameters"] = parameters.get(name, {})
    return document


def test_each_layer_overrides_the_one_below_and_records_who_won(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = document_with(PI={"Kp": 10, "Ki": 1}, MSD={"m": 2})
    resolved = resolve(
        config,
        write_system,
        document=document,
        layer=paramset({"PI.Kp": 11, "PI.Ki": 2.5, "MSD.k": 5}),
        start_values={"PI.Kp": 12},
    )
    assert {k: (v.value, v.source) for k, v in resolved.items()} == {
        "MSD.k": (5.0, "paramset"),
        "MSD.m": (2.0, "system"),
        "PI.Ki": (2.5, "paramset"),
        "PI.Kp": (12.0, "start_values"),
    }
    assert list(resolved) == sorted(resolved)


def test_values_nobody_sets_are_left_to_the_fmus_own_start_values(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    assert resolve(config, write_system, document=document_with()) == {}
    resolved = resolve(config, write_system)  # the loop sets PI.Kp only
    assert list(resolved) == ["PI.Kp"] and resolved["PI.Kp"].source == "system"


def test_start_values_alone_reach_a_system_without_parameters(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    resolved = resolve(
        config, write_system, document=document_with(), start_values={"MSD.m": 4, "PI.r": 1}
    )
    assert {k: (v.value, v.source) for k, v in resolved.items()} == {
        "MSD.m": (4.0, "start_values"),
        "PI.r": (1.0, "start_values"),
    }


@pytest.fixture
def typed_plant(workspace: Path, fmus: dict[str, Path]) -> None:
    write_fmu(
        workspace / "fmus" / "plant.fmu",
        [
            *PLANT_VARIABLES,
            par("n", type="Integer", start="1"),
            par("on", type="Boolean", start="false"),
            par("label", type="String", start="a"),
            Var("x0", "local", "Real", "continuous", "exact", "1"),
            Var("x1", "local", "Real", "continuous", "approx", "1"),
            Var("tmp", "local", "Real", "continuous", "calculated"),
            Var("fixed_out", "output", "Real", "constant", "exact", "2"),
        ],
        model="MSD",
    )


def test_values_are_converted_to_the_variables_type(
    config: Config, typed_plant: None, write_system: WriteSystem
) -> None:
    """OMSimulator's ``setValue`` picks the setter from the Python type, so a Real set from the
    JSON number ``10`` must be recorded as ``10.0``."""
    resolved = resolve(
        config,
        write_system,
        document=document_with(MSD={"m": 3, "n": 4.0, "on": True, "label": "text", "x0": 1}),
        start_values={"MSD.k": 7, "MSD.x1": 0},
    )
    values = {name: item.value for name, item in resolved.items()}
    assert values == {
        "MSD.k": 7.0,
        "MSD.label": "text",
        "MSD.m": 3.0,
        "MSD.n": 4,
        "MSD.on": True,
        "MSD.x0": 1.0,
        "MSD.x1": 0.0,
    }
    assert {name: type(value) for name, value in values.items()} == {
        "MSD.k": float,
        "MSD.label": str,
        "MSD.m": float,
        "MSD.n": int,
        "MSD.on": bool,
        "MSD.x0": float,
        "MSD.x1": float,
    }


@pytest.mark.parametrize(
    ("layer", "name", "value", "match"),
    [
        (
            "start_values",
            "PI.Kpp",
            1,
            r"start_values entry 'PI.Kpp': .*no variable 'Kpp'.*Close matches: Kp",
        ),
        (
            "start_values",
            "PII.Kp",
            1,
            r"start_values entry 'PII.Kp': unknown component 'PII'.*Close matches: PI",
        ),
        ("start_values", "Kp", 1, r"start_values entry 'Kp': 'Kp' is not '<component>.<variable>'"),
        ("start_values", "MSD.x", 1, r"MSD.x is an output, which the FMU computes"),
        ("start_values", "MSD.fixed_out", 1, r"MSD.fixed_out is an output"),
        ("start_values", "MSD.F", 1, r"MSD.F is an input driven by PI.u"),
        ("start_values", "MSD.tmp", 1, r"MSD.tmp is a local variable the FMU computes"),
        ("start_values", "MSD.m", "heavy", r"MSD.m is a Real variable and needs a number"),
        ("start_values", "MSD.m", True, r"MSD.m is a Real variable and needs a number"),
        ("start_values", "MSD.n", 1.5, r"MSD.n is a Integer variable and needs a whole number"),
        ("start_values", "MSD.on", 1, r"MSD.on is a Boolean variable and needs true or false"),
        ("start_values", "MSD.m", float("inf"), r"needs a number"),
        ("start_values", "MSD.m", 10**400, r"needs a number"),
        ("paramset", "MSD.x", 1, r"paramset value 'MSD.x': MSD.x is an output"),
        ("paramset", "PX.Kp", 1, r"paramset value 'PX.Kp': unknown component"),
    ],
)
def test_bad_entries_are_refused_naming_the_layer_and_the_fix(
    config: Config,
    typed_plant: None,
    write_system: WriteSystem,
    layer: str,
    name: str,
    value: StartValue,
    match: str,
) -> None:
    kwargs: dict[str, Any] = (
        {"start_values": {name: value}}
        if layer == "start_values"
        else {"layer": paramset({name: value})}
    )
    with pytest.raises(UserError, match=match):
        resolve(config, write_system, document=document_with(), **kwargs)


def test_an_entry_in_the_system_file_is_refused_with_its_layer_too(
    config: Config, typed_plant: None, write_system: WriteSystem
) -> None:
    document = document_with(MSD={"x": 1.0})
    with pytest.raises(UserError, match=r"system file parameter 'MSD.x': MSD.x is an output"):
        resolve(config, write_system, document=document)


def test_the_first_bad_entry_in_layer_order_is_reported(
    config: Config, typed_plant: None, write_system: WriteSystem
) -> None:
    with pytest.raises(UserError, match=r"paramset value 'PI.nope'"):
        resolve(
            config,
            write_system,
            document=document_with(),
            layer=paramset({"PI.nope": 1}),
            start_values={"MSD.x": 1},
        )


def test_a_paramset_unit_that_differs_from_the_declared_unit_is_refused(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    # PI.r is an unconnected input declared in 'm'.
    with pytest.raises(UserError, match=r"gives PI.r in 'mm' but the FMU declares 'm'.*no units"):
        resolve(
            config,
            write_system,
            document=document_with(),
            layer=paramset({"PI.r": 1.0}, {"PI.r": "mm"}),
        )
    same = resolve(
        config,
        write_system,
        document=document_with(),
        layer=paramset({"PI.r": 1.0}, {"PI.r": "m"}),
    )
    assert same["PI.r"].value == 1.0
    # A unit on a variable that declares none is not a conflict, nor is a unit on no value.
    other = resolve(
        config,
        write_system,
        document=document_with(),
        layer=paramset({"MSD.m": 1.0}, {"MSD.m": "kg", "PI.r": "mm"}),
    )
    assert list(other) == ["MSD.m"]


def test_start_values_are_not_checked_against_paramset_units(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    resolved = resolve(
        config,
        write_system,
        document=document_with(),
        layer=paramset({"PI.r": 1.0}, {"PI.r": "m"}),
        start_values={"PI.r": 2.0},
    )
    assert resolved["PI.r"].value == 2.0 and resolved["PI.r"].source == "start_values"
