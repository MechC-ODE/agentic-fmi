"""``system_export`` and ``system_import`` through the in-memory MCP client: the structured
results the client validates against the published schemas, and the refusal texts the model
sees.

Collaborators: ``mcp_support.Session`` (the ``session`` fixture, over the real server in this
process), the ``workspace`` fixture and ``system_fmus`` (synthetic FMUs: zips with a
``modelDescription.xml`` and a placeholder binary; nothing here runs one). ``tests/ssp/`` covers
the core functions; this module covers the adapters.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from mcp_support import Session, text_of
from system_fmus import loop_system, write_json, write_loop_fmus


@pytest.fixture
def loop(workspace: Path) -> dict[str, Path]:
    """``loop.json`` and its two synthetic FMUs in the workspace."""
    fmus = write_loop_fmus(workspace)
    write_json(workspace / "loop.json", loop_system())
    return fmus


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_then_import_returns_the_structured_results(
    session: Session, workspace: Path, loop: dict[str, Path]
) -> None:
    result = session.call("system_export", system="loop.json", dest="exports/loop.ssp")
    assert not result.is_error, text_of(result)
    exported = result.structured_content
    assert exported is not None and json.loads(text_of(result)) == exported
    path = workspace / "exports" / "loop.ssp"
    assert exported == {
        "path": "exports/loop.ssp",
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
        "fmus": {"PI": sha256(loop["pi"]), "MSD": sha256(loop["plant"])},
        "parameters": 1,
    }

    imported = session.ok("system_import", source="exports/loop.ssp")
    assert imported["system"] == "imported/loop/loop.json"
    assert imported["components"] == exported["fmus"]
    assert (imported["connections"], imported["parameters"]) == (2, 1)
    assert imported["warnings"][0].startswith("components.PI: not connected: r.")
    assert "run_start(system='imported/loop/loop.json')" in imported["hint"]
    assert "null" not in text_of(session.call("system_import", source="exports/loop.ssp", name="b"))


def test_refusals_are_errors_with_the_fix(
    session: Session, workspace: Path, loop: dict[str, Path]
) -> None:
    session.ok("system_export", system="loop.json", dest="loop.ssp")
    text = session.error("system_export", system="loop.json", dest="loop.ssp")
    assert "loop.ssp already exists, and system_export never overwrites" in text
    assert "is inside the run store" in session.error(
        "system_export", system="loop.json", dest=".agentic-fmi/x.ssp"
    )
    session.ok("system_import", source="loop.ssp")
    assert "imported/loop already exists" in session.error("system_import", source="loop.ssp")

    with zipfile.ZipFile(workspace / "loop.ssp") as exported:
        ssd = exported.read("SystemStructure.ssd")
    with zipfile.ZipFile(workspace / "slip.ssp", "w") as archive:
        archive.writestr("SystemStructure.ssd", ssd.replace(b"resources/PI.fmu", b"../PI.fmu"))
    text = session.error("system_import", source="slip.ssp")
    assert "component PI: the reference '../PI.fmu' has a '..', '.' or empty path segment" in text
    assert list((workspace / "imported").iterdir()) == [workspace / "imported" / "loop"]
    assert "was not found in the workspace" in session.error("system_import", source="no.ssp")
