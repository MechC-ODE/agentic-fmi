"""Validate an FMU's ``modelDescription.xml`` and archive layout with FMPy, without loading
binaries.

Wraps ``fmpy.validation.validate_fmu`` (XML schema, variable names, model structure, start
values, units, backslashes in archive paths) and adds the project's own checks: FMI 1.0 and
ScheduledExecution-only are reported as unsupported.

Collaborators: ``fmu/catalog.py`` (path resolution), ``fmu/inspect.py`` (parsing),
``mcp/server.py`` (``fmu_validate``).
"""

from __future__ import annotations

from pathlib import Path

from fmpy.validation import validate_fmu as fmpy_validate_fmu
from pydantic import BaseModel

from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.catalog import relative_path, resolve_fmu
from agentic_fmi.fmu.inspect import (
    SCHEDULED_EXPLAINED,
    declared_fmi_version,
    read_description,
    truncate,
)

PROBLEM_MAX_CHARS = 300
"""Longest problem text in a report."""

UNSUPPORTED = "Unsupported by Agentic FMI: "

FMI1_PROBLEM = UNSUPPORTED + "FMI 1.0 is not supported; export the model as FMI 2.0 or 3.0."


class ValidationReport(BaseModel):
    """The ``fmu_validate`` result."""

    fmu: str
    valid: bool
    n_problems: int
    problems: list[str]
    """At most ``Limits.inspect_max_items`` problems, each truncated to 300 characters."""
    truncated: bool = False


def _scheduled_only_problems(path: Path) -> list[str]:
    """The ScheduledExecution-only finding; empty if the XML can't be read or has CS or ME."""
    try:
        md = read_description(path)
    except UserError:
        return []
    if md.coSimulation is None and md.modelExchange is None and md.scheduledExecution is not None:
        return [UNSUPPORTED + SCHEDULED_EXPLAINED]
    return []


def _find_problems(path: Path) -> list[str]:
    """All problems: an unreadable archive or FMI 1.0 stops here, otherwise FMPy's checks run."""
    try:
        version = declared_fmi_version(path)
    except UserError as exc:
        return [str(exc)]
    if version == "1.0":
        return [FMI1_PROBLEM]
    return _scheduled_only_problems(path) + list(fmpy_validate_fmu(str(path)))


def validate_fmu(config: Config, fmu: str) -> ValidationReport:
    """Validate a workspace FMU. Problems are data, not errors.

    Raises:
        UserError: only when the FMU can't be found or resolved (see ``resolve_fmu``).
    """
    path = resolve_fmu(config, fmu)
    problems = _find_problems(path)
    cap = config.limits.inspect_max_items
    return ValidationReport(
        fmu=relative_path(config, path),
        valid=not problems,
        n_problems=len(problems),
        problems=[truncate(str(problem), PROBLEM_MAX_CHARS) for problem in problems[:cap]],
        truncated=len(problems) > cap,
    )
