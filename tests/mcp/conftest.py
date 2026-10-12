"""Fixtures for the MCP tool tests: the server configuration, an in-memory MCP session and a
synthetic FMU builder.

Collaborators: the shared ``workspace`` and ``fmu`` fixtures (``tests/conftest.py``) and
``mcp_support`` (the session helpers, importable because pytest puts this directory on
``sys.path``). The synthetic FMUs are zip archives holding only a ``modelDescription.xml``; they
carry no binaries, so nothing here can load native code.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from mcp_support import FAST_TIMING, Session, open_session

from agentic_fmi.config import Config, load_config

SynthFmu = Callable[..., Path]


@pytest.fixture
def mcp_config(workspace: Path) -> Config:
    """The server configuration for the test workspace, with :data:`FAST_TIMING`."""
    return replace(load_config(workspace), timing=FAST_TIMING)


@pytest.fixture
def session(mcp_config: Config) -> Iterator[Session]:
    """A connected in-memory MCP session (BouncingBall and the other FMUs come from ``fmu``)."""
    with open_session(mcp_config) as opened:
        yield opened


def _variable_xml(index: int, description: str) -> str:
    kind = "input" if index % 3 == 0 else "parameter"
    attributes = (
        'causality="input" variability="continuous"'
        if kind == "input"
        else 'causality="parameter" variability="fixed"'
    )
    return (
        f'<ScalarVariable name="signal_{index:04d}" valueReference="{index}" {attributes} '
        f'description="{description}"><Real start="0" min="-1000" max="1000"/></ScalarVariable>'
    )


@pytest.fixture
def synth_fmu(workspace: Path) -> SynthFmu:
    """``synth_fmu(name, variables=300, description="...")``: a schema-valid FMI 2.0 FMU with no
    binaries (listed and inspected, never run), for output-budget tests."""

    def build(name: str, variables: int = 300, description: str = "x" * 110) -> Path:
        rows = "".join(_variable_xml(i, description) for i in range(variables))
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<fmiModelDescription fmiVersion="2.0" modelName="Synth" guid="{synth}">\n'
            '<CoSimulation modelIdentifier="Synth"/>\n'
            '<DefaultExperiment startTime="0" stopTime="1"/>\n'
            f"<ModelVariables>{rows}</ModelVariables>\n"
            "<ModelStructure/>\n"
            "</fmiModelDescription>\n"
        )
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("modelDescription.xml", xml)
        return path

    return build
