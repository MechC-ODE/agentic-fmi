"""``system_import`` on well-formed SSPs: the system file and FMU copies it writes, SSP 1.0
parameter precedence with ``prefix``, defaults, names, and problems passed on as warnings.

Collaborators: ``agentic_fmi.ssp.import_ssp`` (under test), ``agentic_fmi.system`` (the result is
loaded and checked again), the fixtures of ``tests/ssp/conftest.py`` (``make_ssp`` writes SSPs
holding the synthetic FMUs of ``system_fmus``) and ``ssp_support`` (SSD and SSV text). Nothing is
run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from ssp_support import (
    DEFAULT_CONNECTIONS,
    MakeSsp,
    binding_xml,
    component_xml,
    connection_xml,
    ssv_text,
)
from system_fmus import PI_VARIABLES, write_fmu

from agentic_fmi.config import SYSTEM_DEFAULT_STEP_S, Config
from agentic_fmi.errors import UserError
from agentic_fmi.ssp import import_ssp
from agentic_fmi.system import check_system


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document(workspace: Path, name: str) -> dict[str, Any]:
    path = workspace / "imported" / name / f"{name}.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def test_import_writes_the_system_file_and_private_fmu_copies(
    config: Config, make_ssp: MakeSsp, workspace: Path, fmus: dict[str, Path]
) -> None:
    values = {"PI.Kp": ("Real", "7.5"), "MSD.k": ("Integer", "3")}
    source = make_ssp(
        bindings=binding_xml("resources/parameters.ssv"),
        members={"resources/parameters.ssv": ssv_text(values).encode()},
    )
    before = sha256(source)

    result = import_ssp(config, "loop.ssp")

    assert result.system == "imported/loop/loop.json"
    assert result.components == {"PI": sha256(fmus["pi"]), "MSD": sha256(fmus["plant"])}
    assert (result.connections, result.parameters) == (2, 2)
    assert document(workspace, "loop") == {
        "version": 1,
        "components": {
            "PI": {"fmu": "PI.fmu", "parameters": {"Kp": 7.5}},
            "MSD": {"fmu": "MSD.fmu", "parameters": {"k": 3}},
        },
        "connections": [{"from": "PI.u", "to": "MSD.F"}, {"from": "MSD.x", "to": "PI.y"}],
        "experiment": {"start_time": 0.0, "stop_time": 2.0, "step_size": 0.01},
    }
    directory = workspace / "imported" / "loop"
    assert sorted(p.name for p in directory.iterdir()) == ["MSD.fmu", "PI.fmu", "loop.json"]
    assert sha256(directory / "PI.fmu") == sha256(fmus["pi"])
    assert [p.name for p in (workspace / "imported").iterdir()] == ["loop"]  # no staging left
    assert sha256(source) == before
    (warning,) = result.warnings
    assert warning.startswith("components.PI: not connected: r.")
    assert result.hint is not None and "run_start(system='imported/loop/loop.json')" in result.hint
    assert check_system(config, result.system).ok


def test_precedence_follows_ssp_1_0_with_a_warning_per_override(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    inline = ssv_text({"Kp": ("Real", "1"), "Ki": ("Real", "2")}, inline=True)
    components = component_xml(
        "PI",
        "resources/pi.fmu",
        bindings=binding_xml(inline=inline) + binding_xml("resources/component.ssv"),
    ) + component_xml("MSD", "resources/plant.fmu")
    make_ssp(
        components=components,
        bindings=binding_xml("resources/system.ssv")
        + binding_xml("resources/pi.ssv", prefix="PI."),
        members={
            "resources/component.ssv": ssv_text({"Kp": ("Real", "3")}).encode(),
            "resources/system.ssv": ssv_text(
                {"PI.Kp": ("Real", "4"), "MSD.m": ("Real", "2"), "Nope.x": ("Real", "1")}
            ).encode(),
            "resources/pi.ssv": ssv_text({"Kp": ("Real", "5")}).encode(),
        },
    )

    result = import_ssp(config, "loop.ssp", name="ranked")

    components_found = document(workspace, "ranked")["components"]
    assert components_found == {
        "PI": {"fmu": "PI.fmu", "parameters": {"Kp": 5.0, "Ki": 2.0}},
        "MSD": {"fmu": "MSD.fmu", "parameters": {"m": 2.0}},
    }
    overrides = [w for w in result.warnings if "overrides" in w]
    assert overrides == [
        "PI.Kp: 3.0 from resources/component.ssv (bound to component PI) overrides 1.0 from the "
        "inline values of component PI (SSP 1.0 section 5.2.3: a later binding and a higher "
        "level win).",
        "PI.Kp: 4.0 from resources/system.ssv (bound to the system) overrides 3.0 from "
        "resources/component.ssv (bound to component PI) (SSP 1.0 section 5.2.3: a later binding "
        "and a higher level win).",
        "PI.Kp: 5.0 from resources/pi.ssv (bound to the system) overrides 4.0 from "
        "resources/system.ssv (bound to the system) (SSP 1.0 section 5.2.3: a later binding and "
        "a higher level win).",
    ]
    assert any("'Nope.x'" in w and "name no component" in w for w in result.warnings)
    assert result.parameters == 3
    assert len(result.warnings) == 5  # three overrides, the ignored name, PI.r unconnected


def test_a_unit_that_differs_from_the_fmu_is_kept_with_a_warning(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    make_ssp(
        bindings=binding_xml("resources/p.ssv"),
        members={
            "resources/p.ssv": ssv_text({"PI.r": ("Real", "5")}, units={"PI.r": "mm"}).encode()
        },
    )
    result = import_ssp(config, "loop.ssp")
    assert document(workspace, "loop")["components"]["PI"]["parameters"] == {"r": 5.0}
    assert any(
        w.startswith(
            "PI.r: resources/p.ssv (bound to the system) gives the value in 'mm' but the "
            "FMU declares 'm'"
        )
        for w in result.warnings
    )


def test_a_missing_step_and_stop_time_get_defaults_with_warnings(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    make_ssp(step=None, experiment="")
    result = import_ssp(config, "loop.ssp")
    experiment = document(workspace, "loop")["experiment"]
    assert experiment == {"start_time": 0.0, "stop_time": 1.0, "step_size": SYSTEM_DEFAULT_STEP_S}
    assert result.warnings[0].startswith("The SSP states no stop time; stop_time is 1.")
    assert result.warnings[1].startswith("The SSP states no communication step; step_size is")


def test_the_older_fixed_step_master_annotation_gives_the_step(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    older = (
        '<ssd:Annotations><ssc:Annotation type="org.openmodelica"><oms:Annotations>'
        '<oms:SimulationInformation><oms:FixedStepMaster description="oms-ma" stepSize="0.05"/>'
        "</oms:SimulationInformation></oms:Annotations></ssc:Annotation></ssd:Annotations>"
    )
    make_ssp(step=None, system_tail=older)
    import_ssp(config, "loop.ssp")
    assert document(workspace, "loop")["experiment"]["step_size"] == 0.05


def test_one_member_used_by_two_components_is_copied_for_each(
    config: Config, make_ssp: MakeSsp, workspace: Path, fmus: dict[str, Path]
) -> None:
    components = component_xml("A", "resources/plant.fmu") + component_xml(
        "B", "resources/plant.fmu"
    )
    make_ssp(components=components, connections=connection_xml("A", "x", "B", "F"))
    result = import_ssp(config, "loop.ssp", name="pair")
    assert result.components == {"A": sha256(fmus["plant"]), "B": sha256(fmus["plant"])}
    directory = workspace / "imported" / "pair"
    assert sha256(directory / "A.fmu") == sha256(directory / "B.fmu") == sha256(fmus["plant"])


def test_problems_of_the_imported_system_are_passed_on_as_warnings(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    make_ssp(connections=DEFAULT_CONNECTIONS + connection_xml("PI", "nope", "MSD", "F"))
    result = import_ssp(config, "loop.ssp")
    assert result.warnings[0].startswith("error: connections[2].from 'PI.nope': component PI")
    assert result.hint is not None and "has 1 error(s)" in result.hint
    assert "system_check(system='imported/loop/loop.json')" in result.hint
    assert (workspace / "imported" / "loop" / "loop.json").is_file()


def test_other_ssd_variants_are_ignored_with_a_warning(config: Config, make_ssp: MakeSsp) -> None:
    make_ssp(members={"Variant.ssd": b"<not read/>"})
    result = import_ssp(config, "loop.ssp")
    assert any("other variants were ignored: Variant.ssd" in w for w in result.warnings)


def test_warnings_are_capped_at_five(config: Config, make_ssp: MakeSsp) -> None:
    rows = {"PI.Kp": ("Real", "1")}
    bindings = "".join(binding_xml(f"resources/p{i}.ssv") for i in range(8))
    members = {f"resources/p{i}.ssv": ssv_text(rows).encode() for i in range(8)}
    result = import_ssp(config, make_ssp(bindings=bindings, members=members).name)
    assert len(result.warnings) == 5
    assert (
        result.warnings[-1] == "... and 4 more (system_check lists the problems)."
    )  # 7 overrides + PI.r


def test_names_default_to_the_stem_and_are_checked(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    make_ssp(name="my-loop.ssp")
    with pytest.raises(UserError, match=r"The SSP's file name 'my-loop' can't name imported"):
        import_ssp(config, "my-loop.ssp")
    with pytest.raises(UserError, match="reserved file name on Windows"):
        import_ssp(config, "my-loop.ssp", name="CON")
    assert import_ssp(config, "my-loop.ssp", name="first").system == "imported/first/first.json"
    with pytest.raises(UserError, match=r"imported/first already exists.*never replaces"):
        import_ssp(config, "my-loop.ssp", name="First")
    assert [p.name for p in (workspace / "imported").iterdir()] == ["first"]


def test_an_fmi3_component_is_imported(config: Config, make_ssp: MakeSsp, workspace: Path) -> None:
    fmi3 = write_fmu(workspace / "src" / "pi3.fmu", PI_VARIABLES, model="PI", version="3.0")
    make_ssp(members={"resources/pi.fmu": fmi3.read_bytes()})
    result = import_ssp(config, "loop.ssp")
    assert result.components["PI"] == sha256(fmi3)
