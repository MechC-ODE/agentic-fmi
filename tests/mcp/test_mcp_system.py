"""System tools through the in-memory MCP client: ``system_check``'s result and failures, and the
texts ``run_start`` returns when a system request is wrong or accepted.

Collaborators: ``mcp_support.Session`` (the ``session`` fixture, over the real server in this
process), the ``workspace`` fixture, ``system_fmus`` (synthetic FMUs: zips with a
``modelDescription.xml`` and an empty placeholder where a binary would be; nothing here runs
one). ``oms_pin`` is monkeypatched (no OMSimulator install needed) and ``Supervisor.spawn`` is
faked, so an accepted ``run_start`` leaves a ``queued`` run and starts no worker; the system
run's worker is covered by ``tests/worker``. ``tests/system/`` covers the core functions; this
module covers the tool adapters, the error text and the structured content the client validates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ImageContent
from mcp_support import Session, text_of
from system_fmus import loop_system, write_json, write_loop_fmus

from agentic_fmi import oms_pin
from agentic_fmi.store.protocol import OmsVersion
from agentic_fmi.supervisor import Supervisor


@pytest.fixture
def loop(workspace: Path) -> Path:
    """``loop.json`` with its two FMUs (``fmus/pi.fmu``, ``fmus/plant.fmu``) in the workspace."""
    write_loop_fmus(workspace)
    return write_json(workspace / "loop.json", loop_system())


@pytest.fixture
def no_workers(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """A matching OMSimulator install, and a ``spawn`` that starts nothing."""
    found = OmsVersion(version="3.0.0.post198", sha256="b" * 64)
    monkeypatch.setattr(oms_pin, "installed", lambda: found)
    monkeypatch.setattr(oms_pin, "pin_problem", lambda got, expected: None)
    started: list[Path] = []

    def spawn(self: Supervisor, run_dir: Path) -> int:
        started.append(Path(run_dir))
        return 12345

    monkeypatch.setattr(Supervisor, "spawn", spawn)
    return started


def test_system_check_returns_the_structured_result(session: Session, loop: Path) -> None:
    result = session.call("system_check", system="loop.json")
    assert not result.is_error, text_of(result)
    check = result.structured_content
    assert check is not None and json.loads(text_of(result)) == check
    assert check["ok"] is True and check["system"] == "loop.json" and check["connections"] == 2
    assert check["components"] == {"PI": "fmus/pi.fmu", "MSD": "fmus/plant.fmu"}
    assert check["experiment"] == {"start_time": 0, "stop_time": 10, "step_size": 0.01}
    assert check["errors"] == [] and check["truncated"] is False
    (warning,) = check["warnings"]
    assert warning.startswith("components.PI: not connected: r.")
    assert "null" not in text_of(result)


def test_system_check_lists_problems_instead_of_failing(
    session: Session, workspace: Path, loop: Path
) -> None:
    document = loop_system(
        connections=[
            {"from": "PIX.u", "to": "MSD.F"},
            {"from": "MSD.F", "to": "PI.u"},
            {"from": "PI.u", "to": "MSD.F"},
            {"from": "PI.u", "to": "MSD.F"},
        ]
    )
    document["components"]["PI"]["parameters"] = {"u": 1}
    write_json(workspace / "loop.json", document)
    check = session.ok("system_check", system="loop.json")
    assert check["ok"] is False and check["truncated"] is False
    errors = check["errors"]
    assert errors[0].startswith("connections[0].from 'PIX.u': unknown component 'PIX'")
    assert "Close matches: PI." in errors[0]
    assert any(e.startswith("connections[1]: from and to are swapped") for e in errors)
    assert any(e.startswith("MSD.F has 2 drivers: connections[2]") for e in errors)
    assert any(e.startswith("components.PI.parameters.u: ") and "output" in e for e in errors)


def test_system_check_failures_are_errors_with_the_fix(
    session: Session, workspace: Path, loop: Path
) -> None:
    assert "'nope.json' was not found" in session.error("system_check", system="nope.json")
    assert "outside the workspace" in session.error("system_check", system="../loop.json")
    assert "is a directory" in session.error("system_check", system="fmus")
    (workspace / "a.ssp").write_bytes(b"PK")
    assert "system_import" in session.error("system_check", system="a.ssp")
    (workspace / "bad.json").write_text('{"version": 1, "extra": true}', encoding="utf-8")
    text = session.error("system_check", system="bad.json")
    assert "not a valid version 1 system file" in text
    assert "extra: Extra inputs are not permitted" in text and "experiment: Field required" in text
    write_json(workspace / "missing.json", loop_system(components={"PI": {"fmu": "no.fmu"}}))
    assert "components.PI.fmu ('no.fmu')" in session.error("system_check", system="missing.json")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "Pass exactly one of fmu"),
        ({"fmu": "fmus/pi.fmu", "system": "loop.json"}, "not both"),
        ({"fmu": "fmus/pi.fmu", "paramset": "p.ssv"}, "paramset applies to system runs only"),
        (
            {"system": "loop.json", "inputs": {"PI.r": {"kind": "constant", "value": 1}}},
            "Systems have no input signals",
        ),
        ({"system": "loop.json", "fmi_type": "ModelExchange"}, "remove fmi_type"),
        ({"system": "loop.json", "solver": "Euler"}, "remove solver"),
        ({"system": "loop.json", "relative_tolerance": 1e-6}, "remove relative_tolerance"),
        ({"system": "loop.json", "start_values": {"PI.u": 1}}, "PI.u is an output"),
        ({"system": "loop.json", "outputs": ["MSD.xx"]}, "Close matches: x"),
        ({"system": "loop.json", "output_interval": 1e-9}, "communication steps"),
    ],
)
def test_run_start_refusals_name_the_fix(
    session: Session,
    loop: Path,
    no_workers: list[Path],
    arguments: dict[str, Any],
    expected: str,
) -> None:
    assert expected in session.error("run_start", **arguments)
    assert session.ok("run_list")["total"] == 0 and no_workers == []


def test_run_start_refuses_a_changed_omsimulator_with_both_digests(
    session: Session, loop: Path, no_workers: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = "OMSimulator tree 1111 does not match the pinned 2222. Run uv sync --locked."
    monkeypatch.setattr(oms_pin, "pin_problem", lambda got, expected: problem)
    assert problem in session.error("run_start", system="loop.json")
    assert no_workers == []


def test_an_accepted_system_run_is_a_queued_run_of_kind_system(
    session: Session, loop: Path, no_workers: list[Path]
) -> None:
    view = session.ok(
        "run_start", system="loop.json", start_values={"PI.Kp": 12}, output_interval=0.05, wait_s=0
    )
    assert (view["run_id"], view["kind"], view["state"]) == ("r1", "system", "queued")
    assert view["fmu"] == "loop.json" and view["fmi_type"] == "CoSimulation"
    assert view["t_stop"] == 10 and "run_status('r1')" in view["hint"]
    assert len(no_workers) == 1
    spec = json.loads((no_workers[0] / "spec.json").read_text(encoding="utf-8"))
    assert spec["kind"] == "system" and spec["master"]["step_size"] == 0.05
    assert spec["parameters"]["PI.Kp"] == {"value": 12.0, "source": "start_values"}
    assert spec["outputs"] == ["PI.u", "MSD.x", "MSD.v"]
    status = session.ok("run_status", run_id="r1")
    assert (status["kind"], status["state"], status["fmu"]) == ("system", "queued", "loop.json")
    (item,) = session.ok("run_list")["items"]
    assert (item["run_id"], item["kind"], item["fmu_name"]) == ("r1", "system", "loop")
    # The compare and plot tools meet it as an unfinished run, with the usual advice.
    assert "only finished or stopped runs have results" in session.error("run_results", run_id="r1")


PI_EXAMPLE = Path(__file__).resolve().parents[2] / "skills/fmu-authoring/examples/pi_controller.py"
PLANT = '''"""First-order lag, exact for a held input: x <- a*x + K*(1 - a)*F, a = exp(-h/tau)."""

import math

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Fmi2Variability, Real

TUNABLE = {"causality": Fmi2Causality.parameter, "variability": Fmi2Variability.tunable}


class Lag(Fmi2Slave):
    author = "tests"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.K, self.tau, self.F, self.x = 2.0, 0.5, 0.0, 0.0
        self.register_variable(Real("K", **TUNABLE))
        self.register_variable(Real("tau", **TUNABLE))
        self.register_variable(Real("F", causality=Fmi2Causality.input))
        self.register_variable(
            Real("x", causality=Fmi2Causality.output, initial=Fmi2Initial.exact)
        )

    def do_step(self, current_time, step_size):
        a = math.exp(-step_size / self.tau)
        self.x = a * self.x + self.K * (1.0 - a) * self.F
        return True
'''


@pytest.mark.oms
def test_a_built_loop_is_checked_run_compared_and_plotted(
    session: Session, workspace: Path
) -> None:
    """The whole path with a real worker and the real OMSimulator: two pythonfmu FMUs built by
    ``fmu_build``, a system file, ``system_check``, two runs that differ in ``PI.Kp``,
    ``run_compare`` and ``run_plot``."""
    models = workspace / "models"
    models.mkdir()
    shutil.copy(PI_EXAMPLE, models / "pi_controller.py")
    (models / "plant.py").write_text(PLANT, encoding="utf-8")
    for source in ("pi_controller", "plant"):
        built = session.ok("fmu_build", source=f"models/{source}.py", wait_s=60)
        assert built["state"] == "finished", built
    write_json(
        workspace / "loop.json",
        {
            "version": 1,
            "components": {
                "PI": {
                    "fmu": "generated/pi_controller/pi_controller.fmu",
                    "parameters": {"Kp": 10, "Ki": 15},
                },
                "P": {"fmu": "generated/plant/plant.fmu"},
            },
            "connections": [{"from": "PI.u", "to": "P.F"}, {"from": "P.x", "to": "PI.y"}],
            "experiment": {"stop_time": 3, "step_size": 0.01},
        },
    )
    check = session.ok("system_check", system="loop.json")
    assert check["ok"] is True and check["errors"] == [] and check["warnings"] == []

    first = session.ok("run_start", system="loop.json", wait_s=60)
    assert (first["kind"], first["state"], first["fmu"]) == ("system", "finished", "loop.json")
    by_name = {v["name"]: v for v in first["summary"]["variables"]}
    assert set(by_name) == {"PI.u", "P.x"} and first["summary"]["n_points"] == 301
    assert by_name["P.x"]["final"] == pytest.approx(1.0, abs=0.01)  # the loop tracks r = 1
    assert by_name["P.x"]["max"] > 1.05  # with the overshoot this step size gives
    second = session.ok("run_start", system="loop.json", start_values={"PI.Kp": 12}, wait_s=60)
    assert second["state"] == "finished", second

    spec = json.loads(
        (session.config.store_dir / "runs" / second["run_id"] / "spec.json").read_text(
            encoding="utf-8"
        )
    )
    pinned = hashlib.sha256((workspace / "generated/plant/plant.fmu").read_bytes()).hexdigest()
    assert spec["components"]["P"]["fmu"]["sha256"] == pinned
    assert spec["parameters"]["PI.Kp"] == {"value": 12.0, "source": "start_values"}
    assert spec["parameters"]["PI.Ki"] == {"value": 15.0, "source": "system"}

    ids = [first["run_id"], second["run_id"]]  # fmu_build jobs took r1 and r2
    comparison = session.ok("run_compare", run_ids=ids)
    assert [(d["field"], d["values"]) for d in comparison["spec_differences"]] == [
        ("parameters.PI.Kp", {ids[0]: 10.0, ids[1]: 12.0})
    ]
    plant_x = next(v for v in comparison["variables"] if v["name"] == "P.x")
    assert plant_x["runs"][1]["max_abs_diff"] > 0.01
    plot = session.call("run_plot", run_ids=ids, variables=["P.x"])
    assert not plot.is_error, text_of(plot)
    (image,) = [c for c in plot.content if isinstance(c, ImageContent)]
    assert base64.b64decode(image.data).startswith(bytes.fromhex("89504e470d0a1a0a"))
