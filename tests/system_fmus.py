"""Synthetic FMUs and system files for the system tests (``tests/system/``, ``tests/mcp``).

Collaborators: ``tests/conftest.py`` (the ``workspace`` fixture; pytest puts this directory on
``sys.path``, so any test module can ``import system_fmus``), ``agentic_fmi.system`` (the code
under test). An FMU here is a zip with a ``modelDescription.xml`` and, if asked, an empty file
where a binary would be (``binaries/linux64/Synth.so``). The placeholder is never loaded: the
system checks and the server read the zip directory and the XML only, and tests never run these
FMUs.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_fmi.store.protocol import (
    ComponentSpec,
    Experiment,
    FileRef,
    FmuRef,
    MasterAlgorithm,
    OmsVersion,
    ParameterLayer,
    ResolvedValue,
    StartValue,
    SystemConnection,
    SystemSpec,
    Versions,
    utc_now,
)

WriteSystem = Callable[..., str]
"""A fixture factory: write a system file into the workspace, return its relative path."""
AddSystemRun = Callable[..., str]
"""A fixture factory: fabricate a finished system run in the store, return its run ID."""

BINARIES = {
    ("2.0", "linux64"): ("binaries/linux64", ".so"),
    ("2.0", "win64"): ("binaries/win64", ".dll"),
    ("3.0", "linux64"): ("binaries/x86_64-linux", ".so"),
    ("3.0", "win64"): ("binaries/x86_64-windows", ".dll"),
}
"""(FMI version, platform) -> the placeholder binary's directory and suffix."""


@dataclass(frozen=True)
class Var:
    """One scalar variable of a synthetic FMU."""

    name: str
    causality: str = "local"
    type: str = "Real"
    variability: str | None = None
    initial: str | None = None
    start: str | None = None
    unit: str | None = None


def inp(name: str, *, unit: str | None = None, type: str = "Real", start: str = "0") -> Var:
    return Var(name, "input", type, None, None, start, unit)


def out(name: str, *, unit: str | None = None, type: str = "Real") -> Var:
    return Var(name, "output", type, None, "calculated", None, unit)


def par(
    name: str,
    *,
    start: str = "1",
    variability: str = "fixed",
    type: str = "Real",
    unit: str | None = None,
) -> Var:
    return Var(name, "parameter", type, variability, None, start, unit)


PI_VARIABLES = (
    inp("y", unit="m"),
    inp("r", unit="m"),
    out("u", unit="N"),
    par("Kp"),
    par("Ki", start="0", variability="tunable"),
)
"""A controller: measurement ``y`` and setpoint ``r`` in, force ``u`` out, two gains."""

PLANT_VARIABLES = (
    inp("F", unit="N"),
    out("x", unit="m"),
    out("v", unit="m/s"),
    par("m"),
    par("k"),
)
"""A plant: force ``F`` in, position ``x`` and speed ``v`` out, mass and stiffness."""


def _attributes(variable: Var, version: str) -> str:
    variability = variable.variability
    if variability is None:
        variability = "discrete" if variable.type not in ("Real", "Float64") else "continuous"
    parts = [f'causality="{variable.causality}"', f'variability="{variability}"']
    if variable.initial:
        parts.append(f'initial="{variable.initial}"')
    if variable.type == "Clock":
        parts.append('intervalVariability="triggered"')
    if version == "3.0":
        if variable.start is not None and variable.type not in ("Clock", "Binary"):
            parts.append(f'start="{variable.start}"')
        if variable.unit:
            parts.append(f'unit="{variable.unit}"')
    return " ".join(parts)


def _variable_xml(index: int, variable: Var, version: str) -> str:
    head = f'name="{variable.name}" valueReference="{index}" {_attributes(variable, version)}'
    if version == "3.0":
        tag = {"Real": "Float64", "Integer": "Int32"}.get(variable.type, variable.type)
        return f"<{tag} {head}/>"
    value = []
    if variable.start is not None:
        value.append(f'start="{variable.start}"')
    if variable.unit:
        value.append(f'unit="{variable.unit}"')
    return (
        f'<ScalarVariable name="{variable.name}" valueReference="{index}" '
        f"{_attributes(variable, version)}><{variable.type} {' '.join(value)}/></ScalarVariable>"
    )


def description_xml(
    variables: Sequence[Var],
    *,
    model: str = "Synth",
    version: str = "2.0",
    interfaces: Sequence[str] = ("CoSimulation",),
    once: bool = False,
) -> str:
    """A schema-valid ``modelDescription.xml`` with these variables and interfaces."""
    flag = ' canBeInstantiatedOnlyOncePerProcess="true"' if once else ""
    tags = "".join(f'<{name} modelIdentifier="{model}"{flag}/>' for name in interfaces)
    units = sorted({v.unit for v in variables if v.unit})
    if units:
        tags += (
            "<UnitDefinitions>"
            + "".join(f'<Unit name="{u}"/>' for u in units)
            + "</UnitDefinitions>"
        )
    rows = "".join(_variable_xml(i, v, version) for i, v in enumerate(variables))
    outputs = [i for i, v in enumerate(variables) if v.causality == "output"]
    if version == "3.0":
        rows += '<Float64 name="time" valueReference="1000" causality="independent"/>'
        structure = "".join(f'<Output valueReference="{i}"/>' for i in outputs)
        root = f'fmiVersion="3.0" modelName="{model}" instantiationToken="{{synth}}"'
    else:
        unknowns = "".join(f'<Unknown index="{i + 1}"/>' for i in outputs)
        structure = f"<Outputs>{unknowns}</Outputs>" if unknowns else ""
        root = f'fmiVersion="2.0" modelName="{model}" guid="{{synth}}"'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<fmiModelDescription {root}>\n{tags}\n"
        f"<ModelVariables>{rows}</ModelVariables>\n"
        f"<ModelStructure>{structure}</ModelStructure>\n</fmiModelDescription>\n"
    )


def write_fmu(
    path: Path,
    variables: Sequence[Var],
    *,
    model: str = "Synth",
    version: str = "2.0",
    interfaces: Sequence[str] = ("CoSimulation",),
    once: bool = False,
    platform: str | None = "linux64",
    members: Mapping[str, str] | None = None,
) -> Path:
    """Write a synthetic FMU at ``path``; ``platform`` names the placeholder binary (``None``:
    no binaries at all), ``members`` adds files (``resources/slavemodule.txt``, ...)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = description_xml(variables, model=model, version=version, interfaces=interfaces, once=once)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelDescription.xml", xml)
        if platform is not None:
            directory, suffix = BINARIES[(version, platform)]
            archive.writestr(f"{directory}/{model}{suffix}", b"")
        for name, text in (members or {}).items():
            archive.writestr(name, text)
    return path


def loop_system(**changes: Any) -> dict[str, Any]:
    """A valid closed loop of ``fmus/pi.fmu`` and ``fmus/plant.fmu`` (one warning: the setpoint
    ``PI.r`` is unconnected). Top-level ``changes`` replace keys of the dictionary."""
    system: dict[str, Any] = {
        "version": 1,
        "components": {
            "PI": {"fmu": "fmus/pi.fmu", "parameters": {"Kp": 10}},
            "MSD": {"fmu": "fmus/plant.fmu"},
        },
        "connections": [{"from": "PI.u", "to": "MSD.F"}, {"from": "MSD.x", "to": "PI.y"}],
        "experiment": {"start_time": 0, "stop_time": 10, "step_size": 0.01},
    }
    return {**system, **changes}


def write_loop_fmus(workspace: Path) -> dict[str, Path]:
    """``fmus/pi.fmu`` and ``fmus/plant.fmu`` in ``workspace``."""
    return {
        "pi": write_fmu(workspace / "fmus" / "pi.fmu", PI_VARIABLES, model="PI"),
        "plant": write_fmu(workspace / "fmus" / "plant.fmu", PLANT_VARIABLES, model="MSD"),
    }


def write_json(path: Path, document: Any) -> Path:
    """Write ``document`` as JSON at ``path`` (creating its directory) and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def system_spec(
    run_id: str,
    parameters: Mapping[str, StartValue] | None = None,
    *,
    root: str = "/workspace",
    name: str = "loop",
    source: ParameterLayer = "start_values",
    step: float = 0.01,
    stop_time: float = 1.0,
    fmu_sha: str = "a",
    system_sha: str = "c",
    oms_sha: str = "b",
    paramset_sha: str | None = None,
    connections: Sequence[tuple[str, str]] = (("PI.u", "MSD.F"), ("MSD.x", "PI.y")),
) -> SystemSpec:
    """A ``SystemSpec`` of the closed loop, as the server would write it, for the tests that
    fabricate runs or compare specs. Each one-letter sha argument is repeated to 64 digits."""
    fmu = FmuRef(
        path=f"{root}/fmus/pi.fmu",
        sha256=fmu_sha * 64,
        size_bytes=1,
        fmi_version="2.0",
        model_name="PI",
    )
    return SystemSpec(
        run_id=run_id,
        created_at=utc_now(),
        name=name,
        system=FileRef(path=f"{root}/{name}.json", sha256=system_sha * 64, size_bytes=9),
        paramset=None
        if paramset_sha is None
        else FileRef(path=f"{root}/p.ssv", sha256=paramset_sha * 64, size_bytes=4),
        components={"PI": ComponentSpec(fmu=fmu), "MSD": ComponentSpec(fmu=fmu)},
        connections=[
            SystemConnection.model_validate({"from": origin, "to": destination})
            for origin, destination in connections
        ],
        parameters={
            ref: ResolvedValue(value=value, source=source)
            for ref, value in sorted((parameters or {}).items())
        },
        master=MasterAlgorithm(step_size=step),
        experiment=Experiment(start_time=0.0, stop_time=stop_time, output_interval=step),
        outputs=["MSD.x"],
        timeout_s=60.0,
        versions=Versions.current(),
        omsimulator=OmsVersion(version="3.0.0.post198", sha256=oms_sha * 64),
    )
