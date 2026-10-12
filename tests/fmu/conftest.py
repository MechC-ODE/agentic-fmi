"""Fixtures for the ``fmu/`` tests: a ``Config`` for the temporary workspace and synthetic FMUs.

Collaborators: ``tests/conftest.py`` (``workspace`` and ``fmu`` fixtures), ``agentic_fmi.config``
(``load_config``). The synthetic FMUs are tiny zip archives that hold only a
``modelDescription.xml``; they carry no binaries, so nothing here can load native code.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fmi.config import Config, load_config

FMI1_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="1.0" modelName="Old" modelIdentifier="Old" guid="{old}"
    numberOfContinuousStates="0" numberOfEventIndicators="0">
  <ModelVariables/>
</fmiModelDescription>
"""

WriteFmu = Callable[[str, dict[str, str]], Path]
Fmi2Xml = Callable[..., str]


@pytest.fixture
def fmi1_xml() -> str:
    """A minimal ``modelDescription.xml`` that declares ``fmiVersion="1.0"``."""
    return FMI1_XML


@pytest.fixture
def config(workspace: Path) -> Config:
    """The ``Config`` whose only allowed root is the temporary ``workspace``."""
    return load_config(workspace)


@pytest.fixture
def write_fmu(workspace: Path) -> WriteFmu:
    """Return ``write_fmu(name, members)``: a zip at ``<workspace>/<name>`` with those members."""

    def write(name: str, members: dict[str, str]) -> Path:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for member, text in members.items():
                archive.writestr(member, text)
        return path

    return write


@pytest.fixture
def fmi2_xml() -> Fmi2Xml:
    """Return ``fmi2_xml(...)``: a schema-valid FMI 2.0 Co-Simulation ``modelDescription.xml``.

    Keyword arguments: ``inputs``, ``outputs`` and ``parameters`` (variable counts),
    ``description`` (model description text), ``same_name`` (give every variable one name, to
    provoke validation problems) and ``variable_description`` (text for each variable).
    """

    def build(
        *,
        inputs: int = 0,
        outputs: int = 0,
        parameters: int = 0,
        description: str = "",
        same_name: bool = False,
        variable_description: str = "",
    ) -> str:
        rows: list[str] = []
        output_indexes: list[int] = []
        specs = [("input", inputs), ("output", outputs), ("parameter", parameters)]
        for causality, count in specs:
            for i in range(count):
                index = len(rows) + 1
                name = "v" if same_name else f"{causality[0]}{i}"
                if causality == "output":
                    output_indexes.append(index)
                rows.append(_scalar(name, index - 1, causality, variable_description))
        unknowns = "".join(f'<Unknown index="{i}"/>' for i in output_indexes)
        outputs_xml = f"<Outputs>{unknowns}</Outputs>" if unknowns else ""
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<fmiModelDescription fmiVersion="2.0" modelName="Synth" guid="{{synth}}" '
            f'description="{description}" generationTool="tests">\n'
            '<CoSimulation modelIdentifier="Synth"/>\n'
            '<DefaultExperiment startTime="0" stopTime="1"/>\n'
            f"<ModelVariables>{''.join(rows)}</ModelVariables>\n"
            f"<ModelStructure>{outputs_xml}</ModelStructure>\n"
            "</fmiModelDescription>\n"
        )

    return build


def _scalar(name: str, reference: int, causality: str, description: str) -> str:
    """One FMI 2.0 ``ScalarVariable`` element for the synthetic model."""
    attributes = {
        "input": 'causality="input" variability="continuous"',
        "output": 'causality="output" variability="continuous" initial="calculated"',
        "parameter": 'causality="parameter" variability="fixed"',
    }[causality]
    start = '<Real start="0"/>' if causality != "output" else "<Real/>"
    text = f' description="{description}"' if description else ""
    return (
        f'<ScalarVariable name="{name}" valueReference="{reference}" {attributes}{text}>'
        f"{start}</ScalarVariable>"
    )
