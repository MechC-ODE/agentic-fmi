"""Loading a system file: size, JSON and schema errors, path containment, FMU resolution.

Collaborators: ``agentic_fmi.system.load_system`` (under test), the ``system_fmus`` helpers and the
fixtures of ``tests/system/conftest.py``. Everything is checked on tiny synthetic FMUs; no FMU
binary is loaded.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from system_fmus import PI_VARIABLES, WriteSystem, loop_system, write_fmu, write_json

from agentic_fmi import system as systems
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.system import load_system

FMI1_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<fmiModelDescription fmiVersion="1.0" modelName="Old" modelIdentifier="Old" guid="{o}"'
    ' numberOfContinuousStates="0" numberOfEventIndicators="0"><ModelVariables/>'
    "</fmiModelDescription>\n"
)


def test_load_reads_hashes_and_resolves(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem, workspace: Path
) -> None:
    write_system()
    loaded = load_system(config, "loop.json")
    data = (workspace / "loop.json").read_bytes()
    assert loaded.ref.sha256 == hashlib.sha256(data).hexdigest()
    assert loaded.ref.size_bytes == len(data) and loaded.ref.path == str(workspace / "loop.json")
    assert loaded.path == workspace / "loop.json" and loaded.name == "loop"
    assert dict(loaded.fmus) == {"PI": fmus["pi"], "MSD": fmus["plant"]}
    assert loaded.descriptions["PI"].modelName == "PI"
    assert loaded.file.components["PI"].parameters == {"Kp": 10}
    first = loaded.file.connections[0]
    assert (first.from_, first.to) == ("PI.u", "MSD.F")


def test_fmu_paths_are_taken_against_the_system_files_directory(
    config: Config, fmus: dict[str, Path], workspace: Path
) -> None:
    document = loop_system()
    document["components"]["PI"]["fmu"] = "../fmus/pi.fmu"
    document["components"]["MSD"]["fmu"] = str(fmus["plant"])  # absolute, inside the workspace
    write_json(workspace / "systems" / "loop.json", document)
    loaded = load_system(config, "systems/loop.json")
    assert dict(loaded.fmus) == {"PI": fmus["pi"], "MSD": fmus["plant"]}


def test_a_file_used_by_two_components_is_parsed_once(
    config: Config,
    fmus: dict[str, Path],
    write_system: WriteSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = loop_system()
    document["components"]["PI2"] = {"fmu": "fmus/pi.fmu"}
    write_system(document)
    calls: list[Path] = []
    real = systems.read_description

    def counting(path: Path) -> Any:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(systems, "read_description", counting)
    loaded = load_system(config, "loop.json")
    assert calls.count(fmus["pi"]) == 1
    assert loaded.descriptions["PI"] is loaded.descriptions["PI2"]


def test_a_byte_order_mark_is_accepted(
    config: Config, fmus: dict[str, Path], workspace: Path
) -> None:
    text = json.dumps(loop_system())
    (workspace / "bom.json").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert load_system(config, "bom.json").name == "bom"


# --- the file itself -------------------------------------------------------------------------


def test_missing_empty_and_misnamed_files_say_what_to_do(config: Config, workspace: Path) -> None:
    with pytest.raises(UserError, match=r"'nope.json' was not found.*Write it first"):
        load_system(config, "nope.json")
    with pytest.raises(UserError, match="system argument is empty"):
        load_system(config, "  ")
    (workspace / "dir.json").mkdir()
    with pytest.raises(UserError, match="is a directory"):
        load_system(config, "dir.json")
    (workspace / "plant.ssp").write_bytes(b"PK")
    with pytest.raises(UserError, match=r"SSP archive.*system_import.*imported/<name>/<name>.json"):
        load_system(config, "plant.ssp")


def test_files_outside_the_workspace_are_refused_before_existence_is_checked(
    config: Config, workspace: Path
) -> None:
    outside = workspace.parent / "outside.json"
    outside.write_text(json.dumps(loop_system()), encoding="utf-8")
    for argument in ("../outside.json", str(outside), "../does-not-exist.json"):
        with pytest.raises(UserError, match="outside the workspace"):
            load_system(config, argument)
    (workspace / "link.json").symlink_to(outside)
    with pytest.raises(UserError, match=r"outside the workspace.*symlink"):
        load_system(config, "link.json")


def test_a_file_over_the_size_limit_is_refused_without_parsing_it(
    config: Config, workspace: Path
) -> None:
    limit = config.limits.system_file_max_bytes
    (workspace / "big.json").write_bytes(b" " * (limit + 1))
    with pytest.raises(UserError, match=r"larger than 256 KiB"):
        load_system(config, "big.json")
    (workspace / "exact.json").write_bytes(b" " * limit)
    with pytest.raises(UserError, match="not a valid version 1 system file"):
        load_system(config, "exact.json")  # at the limit it is read, and is not JSON


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"\xff\xfe\x00", "not UTF-8 text"),
        (b"{not json", "Invalid JSON"),
        (b"[]", "the file"),
        (b"{}", "version: Field required"),
    ],
)
def test_bytes_that_are_not_a_system_file_are_explained(
    config: Config, workspace: Path, data: bytes, match: str
) -> None:
    (workspace / "bad.json").write_bytes(data)
    with pytest.raises(UserError, match=match):
        load_system(config, "bad.json")


def _mutated(**path_to_value: Any) -> dict[str, Any]:
    """The closed loop with the dotted paths in ``path_to_value`` replaced (``__`` for dots)."""
    document = loop_system()
    for dotted, value in path_to_value.items():
        *parents, last = dotted.split("__")
        node: Any = document
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        node[int(last) if isinstance(node, list) else last] = value
    return document


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ({**loop_system(), "outputs": ["PI.u"]}, r"outputs: Extra inputs are not permitted"),
        ({**loop_system(), "version": 2}, r"version: Input should be 1"),
        (_mutated(experiment__step_size=0), r"experiment.step_size: Input should be greater"),
        (
            {**loop_system(), "experiment": {"stop_time": 1}},
            r"experiment.step_size: Field required",
        ),
        (
            _mutated(experiment__stop_time=-1),
            r"stop_time must be greater than start_time",
        ),
        (
            _mutated(components__PI__speed=1),
            r"components.PI.speed: Extra inputs are not permitted",
        ),
        (
            _mutated(connections__0__via="PI"),
            r"connections.0.via: Extra inputs are not permitted",
        ),
        (_mutated(connections__0__from="PIu"), r"must look like '<component>.<variable>'"),
        ({**loop_system(), "components": {}}, r"components"),
        (
            {**loop_system(), "components": {"1PI": {"fmu": "fmus/pi.fmu"}}},
            r"components.1PI is not a valid component name",
        ),
        (
            {**loop_system(), "components": {"a-b": {"fmu": "fmus/pi.fmu"}}},
            r"not a valid component name",
        ),
        (_mutated(components__PI__fmu=""), r"components.PI.fmu"),
        (_mutated(components__PI__parameters=[1]), r"components.PI.parameters"),
    ],
)
def test_schema_violations_name_the_json_path(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem, document: Any, match: str
) -> None:
    write_system(document)
    with pytest.raises(UserError, match=match) as raised:
        load_system(config, "loop.json")
    assert "not a valid version 1 system file" in str(raised.value)


def test_non_finite_numbers_are_refused(
    config: Config, fmus: dict[str, Path], workspace: Path
) -> None:
    text = json.dumps(loop_system()).replace('"Kp": 10', '"Kp": NaN')
    (workspace / "nan.json").write_text(text, encoding="utf-8")
    with pytest.raises(UserError, match="not a valid version 1 system file"):
        load_system(config, "nan.json")


def test_only_the_first_problems_are_quoted_with_their_count(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    document = {**loop_system(), "a": 1, "b": 2, "c": 3, "d": 4}
    write_system(document)
    with pytest.raises(UserError, match=r"\(4 problems in all\)") as raised:
        load_system(config, "loop.json")
    assert str(raised.value).count("Extra inputs are not permitted") == 3


def test_windows_reserved_component_names_are_refused(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    for name in ("CON", "lpt3", "Nul"):
        write_system({**loop_system(), "components": {name: {"fmu": "fmus/pi.fmu"}}})
        with pytest.raises(UserError, match=rf"{name!r} is a reserved file name.*Rename"):
            load_system(config, "loop.json")
    assert systems.is_reserved_name("COM9") and not systems.is_reserved_name("COM10")


def test_component_and_connection_counts_are_limited(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem
) -> None:
    write_system()
    few = replace(config, limits=replace(config.limits, system_max_components=1))
    with pytest.raises(UserError, match=r"2 components; the limit is 1"):
        load_system(few, "loop.json")
    short = replace(config, limits=replace(config.limits, system_max_connections=1))
    with pytest.raises(UserError, match=r"2 connections; the limit is 1"):
        load_system(short, "loop.json")


# --- the FMUs --------------------------------------------------------------------------------


def test_fmu_problems_name_the_component_and_the_fix(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem, workspace: Path
) -> None:
    outside = workspace.parent / "elsewhere.fmu"
    write_fmu(outside, PI_VARIABLES)
    (workspace / "fmus" / "notes.txt").write_text("x", encoding="utf-8")
    (workspace / "fmus" / "dir.fmu").mkdir()
    (workspace / "fmus" / "broken.fmu").write_bytes(b"not a zip")
    with zipfile.ZipFile(workspace / "fmus" / "old.fmu", "w") as archive:
        archive.writestr("modelDescription.xml", FMI1_XML)
    cases = {
        "fmus/missing.fmu": r"components.PI.fmu \('fmus/missing.fmu'\).*was not found",
        str(outside): r"components.PI.fmu.*outside the workspace",
        "../elsewhere.fmu": r"components.PI.fmu.*outside the workspace",
        "fmus/notes.txt": r"components.PI.fmu.*is not a .fmu file",
        "fmus/dir.fmu": r"components.PI.fmu.*is a directory",
        "fmus/broken.fmu": r"components.PI: broken.fmu is not a valid FMU",
        "fmus/old.fmu": r"components.PI: old.fmu: FMI 1.0 is not supported",
    }
    for path, match in cases.items():
        document = loop_system()
        document["components"]["PI"]["fmu"] = path
        write_system(document)
        with pytest.raises(UserError, match=match):
            load_system(config, "loop.json")


def test_an_fmu_symlinked_outside_the_workspace_is_refused(
    config: Config, fmus: dict[str, Path], write_system: WriteSystem, workspace: Path
) -> None:
    outside = workspace.parent / "elsewhere.fmu"
    write_fmu(outside, PI_VARIABLES)
    (workspace / "fmus" / "link.fmu").symlink_to(outside)
    document = loop_system()
    document["components"]["PI"]["fmu"] = "fmus/link.fmu"
    write_system(document)
    with pytest.raises(UserError, match=r"components.PI.fmu.*outside the workspace.*symlink"):
        load_system(config, "loop.json")
