"""``fmu_build`` through the in-memory MCP client: what the model sees for a build that succeeds,
one that fails, and an argument the published schema refuses.

Collaborators: ``mcp_support.Session`` (the ``session`` fixture, over the real server in this
process), the ``workspace`` fixture, and real workers that run real ``python -I -B -m pythonfmu
build`` subprocesses (about a second per build; the test process never imports ``pythonfmu``).
``tests/builds/`` covers the core function; this module covers the tool adapter, the error
text and the structured content the client validates.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mcp_support import Session, text_of

GAIN = '''"""A gain: y = k * u."""

from pythonfmu import Fmi2Causality, Fmi2Slave, Real


class AfmiGain(Fmi2Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.u = 1.0
        self.y = 0.0
        self.register_variable(Real("u", causality=Fmi2Causality.input))
        self.register_variable(Real("y", causality=Fmi2Causality.output))

    def do_step(self, current_time, step_size):
        self.y = 2.0 * self.u
        return True
'''
BROKEN = """from pythonfmu import Fmi2Slave


class AfmiBroken(Fmi2Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k = (2.0 +
"""


def _write(workspace: Path, rel: str, text: str) -> None:
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_successful_build_returns_the_job_view_with_the_fmu_facts(
    session: Session, workspace: Path
) -> None:
    _write(workspace, "models/afmi_gain.py", GAIN)
    result = session.call("fmu_build", source="models/afmi_gain.py", wait_s=60)

    assert not result.is_error, text_of(result)
    view = result.structured_content
    assert view is not None
    assert json.loads(text_of(result)) == view  # the text and the structured content agree
    assert view["kind"] == "build" and view["state"] == "finished", view
    assert view["fmu"] == "generated/afmi_gain/afmi_gain.fmu"
    assert re.fullmatch(r"[0-9a-f]{64}", view["build"]["sha256"]), view["build"]
    assert view["build"]["interfaces"] == ["CoSimulation"]
    assert len(text_of(result).encode()) <= session.config.limits.build_result_max_bytes
    assert (workspace / view["fmu"]).is_file()
    # The new FMU is an ordinary workspace FMU for the other tools.
    paths = [item["path"] for item in session.ok("fmu_list")["items"]]
    assert paths == ["generated/afmi_gain/afmi_gain.fmu"]
    assert session.ok("run_status", run_id=view["run_id"])["state"] == "finished"


def test_a_failed_build_is_an_error_naming_the_line_to_fix(
    session: Session, workspace: Path
) -> None:
    _write(workspace, "models/afmi_broken.py", BROKEN)
    message = session.error("fmu_build", source="models/afmi_broken.py", wait_s=60)

    assert message.startswith("Error executing tool fmu_build: Build failed (pythonfmu)")
    assert "models/afmi_broken.py:7:" in message and "SyntaxError" in message
    assert "Fix the source and call fmu_build again." in message
    assert not (workspace / "generated").exists()  # a failure writes nothing
    (item,) = session.ok("run_list")["items"]
    assert item["kind"] == "build" and item["state"] == "failed"


def test_a_build_name_that_breaks_the_pattern_is_refused_before_any_job(
    session: Session, workspace: Path
) -> None:
    _write(workspace, "models/afmi_gain.py", GAIN)
    message = session.error("fmu_build", source="models/afmi_gain.py", name="1-bad name")

    assert message.startswith("Error executing tool fmu_build")
    assert "name" in message and "pattern" in message
    assert session.ok("run_list")["total"] == 0
    assert not (workspace / "generated").exists()
