"""A real ``afmi mcp --workspace`` subprocess over stdio: it must survive FMUs that print and log.

Collaborators: ``mcp_support.open_stdio_session`` (the MCP SDK's stdio client on
``python -m agentic_fmi mcp``), the ``fmu`` fixture, and the pythonfmu command line for an FMU
whose slave ``print``s from inside the worker. Everything an FMU writes lands in the run's
``log.txt``; if any of it reached the server's stdout, the JSON-RPC stream would break and the
next call here would fail.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from mcp_support import Session, open_stdio_session

from agentic_fmi.config import Config, load_config

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]

CHATTY = """\
from pythonfmu import DefaultExperiment, Fmi2Causality, Fmi2Slave, Real


class Chatty(Fmi2Slave):
    author = "Agentic FMI tests"
    description = "prints from every step"
    default_experiment = DefaultExperiment(start_time=0.0, stop_time=1.0, step_size=0.1)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.y = 0.0
        self.register_variable(Real("y", causality=Fmi2Causality.output))

    def do_step(self, current_time, step_size):
        print(f"chatty step at {current_time:.1f}", flush=True)
        self.y = current_time
        return True
"""


def _log_with(path: Path, text: str, timeout_s: float = 10.0) -> str:
    """``log.txt`` once it holds ``text``. The worker prints a failure's traceback just after it
    records ``failed`` (so the status's log tail has none), and writes it line by line: wait for
    the traceback's last line (the exception message), not its first, or the log can be read
    half written."""
    deadline = time.monotonic() + timeout_s
    while text not in (log := path.read_text()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return log


@pytest.fixture
def stdio(workspace: Path) -> Iterator[Session]:
    with open_stdio_session(load_config(workspace)) as opened:
        yield opened


def test_the_session_survives_odd_and_failing_runs(stdio: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    assert len(stdio.tools()) == 18
    # e = 2 is outside the model's 0..1 but FMPy and the FMU accept it: the ball gains energy.
    odd = stdio.ok("run_start", fmu=BALL, start_values={"e": 2.0}, stop_time=1.0, wait_s=30)
    assert odd["state"] == "finished", odd
    # A start value FMPy can't convert fails inside the worker; its traceback goes to log.txt.
    bad = stdio.ok("run_start", fmu=BALL, start_values={"e": "abc"}, stop_time=1.0, wait_s=30)
    assert bad["state"] == "failed" and bad["error"]["code"] == "simulation_error", bad
    assert "Traceback" not in json.dumps(bad)
    log_path = stdio.config.store_dir / "runs" / bad["run_id"] / "log.txt"
    log = _log_with(log_path, "could not convert string to float")
    assert "Traceback" in log and "could not convert string to float" in log
    assert stdio.ok("workspace_info")["active_runs"] == []
    after = stdio.ok("run_start", fmu=BALL, start_values={"e": 0.5}, stop_time=1.0, wait_s=30)
    assert after["state"] == "finished"
    assert stdio.ok("run_list")["total"] == 3
    assert stdio.error("run_status", run_id="r9").startswith("Error executing tool run_status")


def test_the_session_survives_an_fmu_that_prints(stdio: Session, workspace: Path) -> None:
    cli = Path(sys.executable).parent / "pythonfmu"
    if not cli.is_file():
        pytest.skip("the pythonfmu command is not installed next to the interpreter")
    source = workspace.parent / "chatty_src" / "chatty.py"
    source.parent.mkdir()
    source.write_text(CHATTY)
    built = workspace.parent / "chatty_out"
    subprocess.run(
        [str(cli), "build", "-f", str(source), "-d", str(built)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    shutil.copy2(next(built.glob("*.fmu")), workspace / "Chatty.fmu")
    view = stdio.ok("run_start", fmu="Chatty.fmu", wait_s=30)
    assert view["state"] == "finished", view
    log = (stdio.config.store_dir / "runs" / "r1" / "log.txt").read_text()
    assert "chatty step at 0.0" in log
    assert stdio.ok("workspace_info")["fmu_count"] == 1
    assert stdio.ok("run_results", run_id="r1", variables=["y"])["series"][0]["y"][-1] > 0


def test_stdio_config_is_the_workspace_default_store(stdio: Session, workspace: Path) -> None:
    info = stdio.ok("workspace_info")
    assert info["workspace"] == str(workspace)
    assert info["store_dir"] == str(workspace / ".agentic-fmi")
    assert isinstance(stdio.config, Config)
