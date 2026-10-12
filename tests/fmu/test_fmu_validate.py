"""Tests for ``agentic_fmi.fmu.validate``: FMPy's validation plus the project's unsupported-FMU
findings, reported as data.

Collaborators: the ``fmu`` and ``workspace`` fixtures (``tests/conftest.py``), and ``config``,
``write_fmu``, ``fmi1_xml`` and ``fmi2_xml`` (``tests/fmu/conftest.py``). Reference FMUs are
copied into a temporary workspace first; only ``modelDescription.xml`` is read.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.validate import PROBLEM_MAX_CHARS, validate_fmu

FmuFactory = Callable[[str, str], Path]
WriteFmu = Callable[[str, dict[str, str]], Path]
Fmi2Xml = Callable[..., str]

ALL_FMUS = [
    *(("2.0", name) for name in ("BouncingBall", "Dahlquist", "Feedthrough", "Resource")),
    ("2.0", "Stair"),
    ("2.0", "VanDerPol"),
    *(("3.0", name) for name in ("BouncingBall", "Dahlquist", "Feedthrough", "Resource")),
    *(("3.0", name) for name in ("Roberts", "Stair", "StateSpace", "VanDerPol")),
]
"""Every Reference FMU except Clocks, which is valid but unsupported (tested separately)."""


@pytest.mark.parametrize(("version", "name"), ALL_FMUS, ids=[f"{v}-{n}" for v, n in ALL_FMUS])
def test_validate_reference_fmus_report_no_problems(
    config: Config, fmu: FmuFactory, version: str, name: str
) -> None:
    """The Reference FMUs are valid FMI, so the report is clean."""
    fmu(version, name)
    report = validate_fmu(config, f"{version}/{name}.fmu")
    assert report.fmu == f"{version}/{name}.fmu"
    assert (report.valid, report.n_problems, report.problems, report.truncated) == (
        True,
        0,
        [],
        False,
    )


def test_validate_reports_scheduled_execution_only_as_unsupported(
    config: Config, fmu: FmuFactory
) -> None:
    """Clocks is valid FMI 3.0 but FMPy can't run it, so it is reported, not raised."""
    fmu("3.0", "Clocks")
    report = validate_fmu(config, "3.0/Clocks.fmu")
    assert report.valid is False and report.n_problems == 1
    assert report.problems[0].startswith("Unsupported by Agentic FMI: ScheduledExecution only")


def test_validate_reports_fmi1_as_unsupported_instead_of_raising(
    config: Config, write_fmu: WriteFmu, fmi1_xml: str
) -> None:
    """An FMI 1.0 archive yields one problem that says FMI 1.0 is not supported."""
    write_fmu("old.fmu", {"modelDescription.xml": fmi1_xml})
    report = validate_fmu(config, "old.fmu")
    assert report.valid is False
    assert report.problems == [
        "Unsupported by Agentic FMI: FMI 1.0 is not supported; export the model as FMI 2.0 or 3.0."
    ]


def test_validate_reports_broken_archives_as_problems(
    config: Config, write_fmu: WriteFmu, workspace: Path
) -> None:
    """Non-zip files and archives without XML are data, not errors."""
    (workspace / "junk.fmu").write_bytes(b"junk")
    write_fmu("empty.fmu", {"readme.txt": "x"})
    for name in ("junk.fmu", "empty.fmu"):
        report = validate_fmu(config, name)
        assert report.valid is False and report.n_problems == len(report.problems) >= 1


def test_validate_reports_schema_violations(config: Config, write_fmu: WriteFmu) -> None:
    """A model description that breaks the schema is listed with FMPy's line information."""
    xml = '<fmiModelDescription fmiVersion="2.0" modelName="x"/>'
    write_fmu("bad.fmu", {"modelDescription.xml": xml})
    report = validate_fmu(config, "bad.fmu")
    assert report.valid is False
    assert any("line" in problem for problem in report.problems)


def test_validate_caps_and_truncates_long_problem_lists(
    config: Config, write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """Forty variables with one name give 39 problems; 25 are listed, each within 300 chars."""
    write_fmu("dup.fmu", {"modelDescription.xml": fmi2_xml(inputs=40, same_name=True)})
    report = validate_fmu(config, "dup.fmu")
    assert report.valid is False
    assert report.n_problems >= 39 and report.truncated is True
    assert len(report.problems) == config.limits.inspect_max_items
    assert all(len(problem) <= PROBLEM_MAX_CHARS for problem in report.problems)


def test_validate_truncates_each_problem_to_300_characters(
    config: Config, write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """A 2000-character variable name makes a problem text longer than 300; it is cut."""
    xml = fmi2_xml(inputs=2, same_name=True).replace('name="v"', f'name="{"n" * 2000}"')
    write_fmu("long.fmu", {"modelDescription.xml": xml})
    report = validate_fmu(config, "long.fmu")
    assert report.problems and max(len(p) for p in report.problems) == PROBLEM_MAX_CHARS
    assert any(problem.endswith("...") for problem in report.problems)


def test_validate_unknown_and_outside_fmus_raise_user_error(
    config: Config, fmu: FmuFactory, tmp_path: Path
) -> None:
    """Only path problems raise: a missing FMU and a path outside the workspace."""
    with pytest.raises(UserError, match="not found"):
        validate_fmu(config, "missing.fmu")
    outside = tmp_path / "outside.fmu"
    outside.write_bytes(fmu("3.0", "BouncingBall").read_bytes())
    with pytest.raises(UserError, match="outside the workspace"):
        validate_fmu(config, str(outside))


def test_validate_does_not_modify_the_fmu(config: Config, fmu: FmuFactory) -> None:
    """Validation leaves the archive byte-identical."""
    path = fmu("2.0", "Feedthrough")
    before = path.read_bytes()
    validate_fmu(config, "2.0/Feedthrough.fmu")
    assert path.read_bytes() == before
