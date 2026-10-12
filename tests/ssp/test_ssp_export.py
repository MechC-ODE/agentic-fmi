"""``system_export``: the archive layout, byte-identical repeats (in this process and a fresh
one), validity against OMSimulator's bundled SSD and SSV XSDs, parameter sets, the file-level
round trip through ``system_import``, and the refusals.

Collaborators: ``agentic_fmi.ssp`` (under test), the fixtures of ``tests/ssp/conftest.py``
(synthetic FMUs and ``loop.json``), ``system_fmus`` (FMI 3.0 FMUs and system files) and
``ssp_support`` (the XSD check; OMSimulator's schema directory is located without importing the
package). Nothing is run.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree
from ssp_support import oms_schema_dir, ssv_text, xsd_errors
from system_fmus import PI_VARIABLES, PLANT_VARIABLES, Var, loop_system, write_fmu, write_json

from agentic_fmi import ssp
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.ssp import NAMESPACES, export_ssp, import_ssp

NS = {prefix: NAMESPACES[prefix] for prefix in ("ssd", "ssc", "ssv", "oms", "afmi")}
needs_schemas = pytest.mark.skipif(
    oms_schema_dir() is None, reason="OMSimulator's bundled XSDs not installed"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_the_archive_holds_the_ssd_one_ssv_and_each_fmu(
    config: Config, loop: str, workspace: Path, fmus: dict[str, Path]
) -> None:
    before = {path: sha256(path) for path in [*fmus.values(), workspace / loop]}

    result = export_ssp(config, loop, "exports/loop.ssp")

    path = workspace / "exports" / "loop.ssp"
    assert (result.path, result.sha256, result.size_bytes) == (
        "exports/loop.ssp",
        sha256(path),
        path.stat().st_size,
    )
    assert result.fmus == {"PI": before[fmus["pi"]], "MSD": before[fmus["plant"]]}
    assert result.parameters == 1
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
    assert [i.filename for i in infos] == [
        "SystemStructure.ssd",
        "resources/MSD.fmu",
        "resources/PI.fmu",
        "resources/parameters.ssv",
    ]
    assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {i.external_attr >> 16 for i in infos} == {0o100644}
    assert {i.create_system for i in infos} == {3}
    assert [i.compress_type for i in infos] == [8, 0, 0, 8]
    content = entries(path)
    assert content["resources/PI.fmu"] == fmus["pi"].read_bytes()
    assert {p: sha256(p) for p in before} == before  # inputs untouched
    assert [p.name for p in path.parent.iterdir()] == ["loop.ssp"]  # no temporary file left

    ssd = etree.fromstring(content["SystemStructure.ssd"])
    assert b"generationDateAndTime" not in content["SystemStructure.ssd"]
    assert (ssd.get("version"), ssd.get("name")) == ("1.0", "loop")
    system = ssd.find("ssd:System", NS)
    assert [etree.QName(e).localname for e in system] == [
        "ParameterBindings",
        "Elements",
        "Connections",
        "Annotations",
    ]
    binding = system.find("ssd:ParameterBindings/ssd:ParameterBinding", NS)
    assert binding.get("source") == "resources/parameters.ssv"
    pi = system.find("ssd:Elements/ssd:Component[@name='PI']", NS)
    assert (pi.get("source"), pi.get("implementation")) == ("resources/PI.fmu", "CoSimulation")
    connectors = {
        c.get("name"): (c.get("kind"), etree.QName(c[0]).localname)
        for c in pi.iterfind("ssd:Connectors/ssd:Connector", NS)
    }
    assert connectors == {
        "y": ("input", "Real"),
        "r": ("input", "Real"),
        "u": ("output", "Real"),
        "Kp": ("parameter", "Real"),
        "Ki": ("parameter", "Real"),
    }
    ours = pi.find("ssd:Annotations/ssc:Annotation[@type='org.agentic-fmi']/afmi:Fmu", NS)
    assert ours.get("sha256") == before[fmus["pi"]]
    step = system.find(
        "ssd:Annotations/ssc:Annotation/oms:Annotations/oms:SimulationInformation", NS
    )
    assert dict(step.attrib) == {"name": "ma", "method": "oms_ma", "fixedStepSize": "0.01"}
    experiment = ssd.find("ssd:DefaultExperiment", NS)
    assert dict(experiment.attrib) == {"startTime": "0.0", "stopTime": "10.0"}
    connections = [
        (c.get("startElement"), c.get("startConnector"), c.get("endElement"), c.get("endConnector"))
        for c in system.iterfind("ssd:Connections/ssd:Connection", NS)
    ]
    assert connections == [("PI", "u", "MSD", "F"), ("MSD", "x", "PI", "y")]
    ssv = etree.fromstring(content["resources/parameters.ssv"])
    assert [(p.get("name"), etree.QName(p[0]).localname, p[0].get("value")) for p in ssv[0]] == [
        ("PI.Kp", "Real", "10.0")
    ]


def test_repeated_exports_are_byte_identical_even_from_another_process(
    config: Config, loop: str, workspace: Path
) -> None:
    first = export_ssp(config, loop, "a.ssp")
    second = export_ssp(config, loop, "b.ssp")
    script = (
        "import sys; from pathlib import Path; from agentic_fmi.config import load_config; "
        "from agentic_fmi.ssp import export_ssp; "
        "export_ssp(load_config(Path(sys.argv[1])), 'loop.json', 'c.ssp')"
    )
    subprocess.run([sys.executable, "-c", script, str(workspace)], check=True, timeout=60)
    assert (workspace / "a.ssp").read_bytes() == (workspace / "b.ssp").read_bytes()
    assert (workspace / "a.ssp").read_bytes() == (workspace / "c.ssp").read_bytes()
    assert first.sha256 == second.sha256 == sha256(workspace / "c.ssp")


def test_a_parameter_set_overrides_the_system_file_values(
    config: Config, loop: str, workspace: Path
) -> None:
    (workspace / "p.ssv").write_text(
        ssv_text({"PI.Kp": ("Real", "3"), "MSD.k": ("Integer", "2"), "PI.r": ("Real", "0.5")}),
        encoding="utf-8",
    )
    result = export_ssp(config, loop, "with.ssp", paramset="p.ssv")
    assert result.parameters == 3
    ssv = etree.fromstring(entries(workspace / "with.ssp")["resources/parameters.ssv"])
    assert [(p.get("name"), p[0].get("value")) for p in ssv[0]] == [
        ("MSD.k", "2.0"),  # converted to the Real variable's type
        ("PI.Kp", "3.0"),
        ("PI.r", "0.5"),
    ]


def test_a_system_without_values_gets_no_parameter_set(config: Config, workspace: Path) -> None:
    document = loop_system()
    document["components"]["PI"]["parameters"] = {}
    write_fmu(workspace / "fmus" / "pi.fmu", PI_VARIABLES, model="PI")
    write_fmu(workspace / "fmus" / "plant.fmu", PLANT_VARIABLES, model="MSD")
    write_json(workspace / "bare.json", document)
    export_ssp(config, "bare.json", "bare.ssp")
    content = entries(workspace / "bare.ssp")
    assert ssp.PARAMETERS_MEMBER not in content
    assert b"ParameterBindings" not in content["SystemStructure.ssd"]


def test_one_fmu_used_twice_is_stored_once(config: Config, workspace: Path) -> None:
    plant = write_fmu(workspace / "fmus" / "plant.fmu", PLANT_VARIABLES, model="MSD")
    copy = workspace / "fmus" / "same.fmu"
    copy.write_bytes(plant.read_bytes())
    document = {
        "version": 1,
        "components": {"A": {"fmu": "fmus/plant.fmu"}, "B": {"fmu": "fmus/same.fmu"}},
        "connections": [],
        "experiment": {"stop_time": 1, "step_size": 0.1},
    }
    write_json(workspace / "pair.json", document)
    result = export_ssp(config, "pair.json", "pair.ssp")
    assert result.fmus == {"A": sha256(plant), "B": sha256(plant)}
    content = entries(workspace / "pair.ssp")
    assert [n for n in content if n.endswith(".fmu")] == ["resources/A.fmu"]
    assert content["SystemStructure.ssd"].count(b'source="resources/A.fmu"') == 2


@needs_schemas
def test_exports_validate_against_omsimulators_bundled_xsds(
    config: Config, loop: str, workspace: Path
) -> None:
    (workspace / "p.ssv").write_text(
        ssv_text({"PI.Ki": ("Real", "1.5"), "PI.r": ("Real", "1")}), encoding="utf-8"
    )
    export_ssp(config, loop, "valid.ssp", paramset="p.ssv")
    content = entries(workspace / "valid.ssp")
    assert xsd_errors(content["SystemStructure.ssd"], "SystemStructureDescription.xsd") == []
    assert xsd_errors(content[ssp.PARAMETERS_MEMBER], "SystemStructureParameterValues.xsd") == []


@needs_schemas
def test_fmi3_components_give_an_ssd_2_0_that_validates(config: Config, workspace: Path) -> None:
    write_fmu(workspace / "fmus" / "pi.fmu", PI_VARIABLES, model="PI", version="3.0")
    write_fmu(workspace / "fmus" / "plant.fmu", PLANT_VARIABLES, model="MSD", version="3.0")
    write_json(workspace / "loop3.json", loop_system())
    export_ssp(config, "loop3.json", "loop3.ssp")
    content = entries(workspace / "loop3.ssp")
    ssd = etree.fromstring(content["SystemStructure.ssd"])
    assert ssd.get("version") == "2.0"
    assert ssd.find(".//ssd:Connector[@name='u']/ssc:Float64", NS) is not None
    assert xsd_errors(content["SystemStructure.ssd"], "SystemStructureDescription.xsd") == []
    assert xsd_errors(content[ssp.PARAMETERS_MEMBER], "SystemStructureParameterValues.xsd") == []


SSP_1_0_KINDS = {"input", "output", "parameter", "calculatedParameter", "inout"}
"""The connector kinds SSP 1.0.1 allows (written out here, not imported from the code)."""


def _kinds(ssd: etree._Element) -> set[str]:
    return {c.get("kind") for c in ssd.iterfind(".//ssd:Connector", NS)}


def test_a_version_1_0_export_has_only_ssp_1_0_connector_kinds(
    config: Config, loop: str, workspace: Path
) -> None:
    export_ssp(config, loop, "plain.ssp")
    ssd = etree.fromstring(entries(workspace / "plain.ssp")["SystemStructure.ssd"])
    assert ssd.get("version") == "1.0"
    assert _kinds(ssd) and _kinds(ssd) <= SSP_1_0_KINDS


def test_a_state_a_value_sets_gives_an_ssd_2_0_with_a_local_connector(
    config: Config, workspace: Path
) -> None:
    """SSP 1.0.1 has no ``local`` connector kind, so the export must not claim version 1.0."""
    state = Var("s", "local", "Real", "continuous", "exact", "0")
    write_fmu(workspace / "fmus" / "pi.fmu", PI_VARIABLES, model="PI")
    write_fmu(workspace / "fmus" / "plant.fmu", (*PLANT_VARIABLES, state), model="MSD")
    document = loop_system()
    document["components"]["MSD"]["parameters"] = {"s": 0.5}
    write_json(workspace / "state.json", document)

    export_ssp(config, "state.json", "state.ssp")

    content = entries(workspace / "state.ssp")
    ssd = etree.fromstring(content["SystemStructure.ssd"])
    assert ssd.get("version") == "2.0"
    assert (
        ssd.find(".//ssd:Component[@name='MSD']//ssd:Connector[@name='s']", NS).get("kind")
        == "local"
    )
    assert _kinds(ssd) - SSP_1_0_KINDS == {"local"}
    if oms_schema_dir() is not None:
        assert xsd_errors(content["SystemStructure.ssd"], "SystemStructureDescription.xsd") == []


def test_an_export_imports_back_to_the_same_system(
    config: Config, loop: str, workspace: Path, fmus: dict[str, Path]
) -> None:
    (workspace / "p.ssv").write_text(ssv_text({"MSD.m": ("Real", "2")}), encoding="utf-8")
    export_ssp(config, loop, "exports/loop.ssp", paramset="p.ssv")
    result = import_ssp(config, "exports/loop.ssp", name="back")
    assert result.components == {"PI": sha256(fmus["pi"]), "MSD": sha256(fmus["plant"])}
    imported = json.loads((workspace / "imported/back/back.json").read_text(encoding="utf-8"))
    assert imported == {
        "version": 1,
        "components": {
            "PI": {"fmu": "PI.fmu", "parameters": {"Kp": 10.0}},
            "MSD": {"fmu": "MSD.fmu", "parameters": {"m": 2.0}},
        },
        "connections": [{"from": "PI.u", "to": "MSD.F"}, {"from": "MSD.x", "to": "PI.y"}],
        "experiment": {"start_time": 0.0, "stop_time": 10.0, "step_size": 0.01},
    }
    assert [w for w in result.warnings if not w.startswith("components.PI: not connected")] == []


@pytest.mark.parametrize(
    ("dest", "match"),
    [
        ("", r"The dest argument is empty"),
        ("out.zip", r"dest 'out\.zip' must end in \.ssp"),
        ("../out.ssp", r"dest '\.\./out\.ssp' is outside the workspace"),
        (".agentic-fmi/out.ssp", r"is inside the run store"),
        ("generated/x/out.ssp", r"is inside generated/"),
        ("imported/out.ssp", r"is inside imported/"),
        ("taken.ssp", r"taken\.ssp already exists, and system_export never overwrites"),
        ("file.txt/out.ssp", r"a parent of it is a file"),
    ],
)
def test_bad_destinations_are_refused(
    config: Config, loop: str, workspace: Path, dest: str, match: str
) -> None:
    (workspace / "taken.ssp").write_bytes(b"keep")
    (workspace / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UserError, match=match):
        export_ssp(config, loop, dest)
    assert (workspace / "taken.ssp").read_bytes() == b"keep"
    assert not (workspace.parent / "out.ssp").exists()


def test_a_system_with_errors_is_refused(config: Config, workspace: Path, fmus: object) -> None:
    write_json(workspace / "bad.json", loop_system(connections=[{"from": "PI.y", "to": "MSD.F"}]))
    with pytest.raises(UserError, match=r"Can't export bad: it has 1 error\(s\)\. \(1\) "):
        export_ssp(config, "bad.json", "bad.ssp")
    assert not (workspace / "bad.ssp").exists()


def test_a_bad_parameter_set_is_refused(config: Config, loop: str, workspace: Path) -> None:
    (workspace / "p.ssv").write_text(ssv_text({"PI.u": ("Real", "1")}), encoding="utf-8")
    with pytest.raises(UserError, match=r"paramset value 'PI\.u': PI\.u is an output"):
        export_ssp(config, loop, "x.ssp", paramset="p.ssv")
    assert not (workspace / "x.ssp").exists()


def test_an_fmu_that_changes_during_the_export_is_refused(
    config: Config,
    loop: str,
    workspace: Path,
    fmus: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ssp.write_deterministic_zip

    def write_then_touch(path: Path, members: object) -> None:
        original(path, members)  # type: ignore[arg-type]
        with fmus["plant"].open("ab") as handle:
            handle.write(b"\0")

    monkeypatch.setattr(ssp, "write_deterministic_zip", write_then_touch)
    with pytest.raises(UserError, match=r"component MSD \(plant\.fmu\) changed during the export"):
        export_ssp(config, loop, "exports/x.ssp")
    assert list((workspace / "exports").iterdir()) == []  # no archive, no temporary file


def test_a_file_that_appears_meanwhile_is_never_replaced(
    config: Config, loop: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ssp.write_deterministic_zip

    def write_then_race(path: Path, members: object) -> None:
        original(path, members)  # type: ignore[arg-type]
        (workspace / "race.ssp").write_bytes(b"theirs")

    monkeypatch.setattr(ssp, "write_deterministic_zip", write_then_race)
    with pytest.raises(UserError, match=r"race\.ssp appeared while the export was written"):
        export_ssp(config, loop, "race.ssp")
    assert (workspace / "race.ssp").read_bytes() == b"theirs"
    assert sorted(p.name for p in workspace.glob("race.ssp*")) == ["race.ssp"]
