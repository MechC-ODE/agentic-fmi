"""Helpers for the system-run worker tests: pythonfmu slave sources, the build, a ``SystemSpec``
builder and the exact Jacobi recurrence of the closed loop.

Collaborators: ``tests/worker/conftest.py`` (the fixtures that use them) and the test modules,
which import this module by name because pytest puts this directory on ``sys.path``.
``agentic_fmi.oms_pin`` gives the installed OMSimulator identity; ``agentic_fmi.fmu.inspect``
the FMU facts. The slave sources are text: the test process never imports ``pythonfmu``, and
only the build subprocess and OMSimulator in a worker run them.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

from agentic_fmi import oms_pin
from agentic_fmi.fmu.inspect import read_description, sha256_file
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

REPO_ROOT = Path(__file__).resolve().parents[2]
PI_SOURCE = REPO_ROOT / "skills" / "fmu-authoring" / "examples" / "pi_controller.py"
"""The shipped PI example: ``u = Kp*(r - y) + Ki*integral(r - y)``, computed in ``do_step``."""

PI_DEFAULTS = {"Kp": 10.0, "Ki": 15.0, "r": 1.0}
PLANT_DEFAULTS = {"K": 2.0, "tau": 0.5}

PLANT = '''"""First-order lag with exact zero-order-hold discretization: x <- a*x + K*(1-a)*F."""

import math

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Fmi2Variability, Real

TUNABLE = {"causality": Fmi2Causality.parameter, "variability": Fmi2Variability.tunable}


class Plant(Fmi2Slave):
    author = "tests"
    description = "x' = (K*F - x)/tau, exact over a step with F held"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.K, self.tau = 2.0, 0.5
        self.F, self.x = 0.0, 0.0
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

STALL = '''"""y = t until t = 0.5; every later step is discarded (do_step returns False)."""

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Real


class Stall(Fmi2Slave):
    author = "tests"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.y = 0.0
        self.register_variable(
            Real("y", causality=Fmi2Causality.output, initial=Fmi2Initial.exact)
        )

    def do_step(self, current_time, step_size):
        if current_time + step_size > 0.5 + 1e-9:
            return False
        self.y = current_time + step_size
        return True
'''

SOURCES = {"PIController": None, "Plant": PLANT, "Stall": STALL}
"""Built FMU name -> slave source (``None``: the shipped PI example)."""


def build_fmus(directory: Path) -> dict[str, Path]:
    """Build every :data:`SOURCES` slave with ``python -I -B -m pythonfmu build`` into
    ``directory``; returns FMU name -> path."""
    sources, out = directory / "src", directory / "fmus"
    sources.mkdir(parents=True)
    out.mkdir()
    built: dict[str, Path] = {}
    for name, text in SOURCES.items():
        source = sources / (PI_SOURCE.name if text is None else f"{name.lower()}.py")
        source.write_text(PI_SOURCE.read_text() if text is None else text)
        command = [sys.executable, "-I", "-B", "-m", "pythonfmu", "build", "-f", str(source)]
        subprocess.run([*command, "-d", str(out)], check=True, capture_output=True, timeout=120)
        built[name] = out / f"{name}.fmu"
        assert built[name].is_file(), f"pythonfmu built no {name}.fmu"
    return built


def fmu_ref(path: Path) -> FmuRef:
    md = read_description(path)
    return FmuRef(
        path=str(path),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        fmi_version=md.fmiVersion,
        model_name=md.modelName,
    )


def default_outputs(components: Mapping[str, Path]) -> list[str]:
    """Every component's outputs as ``<component>.<variable>``."""
    return [
        f"{name}.{variable.name}"
        for name, path in components.items()
        for variable in read_description(path).modelVariables
        if variable.causality == "output"
    ]


def system_spec(
    run_id: str,
    system_file: Path,
    components: Mapping[str, Path],
    *,
    connections: Sequence[tuple[str, str]] = (),
    parameters: Mapping[str, tuple[StartValue, ParameterLayer]] | None = None,
    outputs: Sequence[str] | None = None,
    step_size: float = 0.01,
    stop_time: float = 1.0,
    start_time: float = 0.0,
    timeout_s: float = 60.0,
    omsimulator: OmsVersion | None = None,
) -> SystemSpec:
    """The ``SystemSpec`` the server would write; ``system_file`` gets a JSON stand-in for the
    system file it pins. ``omsimulator`` defaults to the installed identity."""
    system_file.write_text(json.dumps({"version": 1, "components": sorted(components)}))
    return SystemSpec(
        run_id=run_id,
        created_at=utc_now(),
        name=system_file.stem,
        system=FileRef(
            path=str(system_file),
            sha256=sha256_file(system_file),
            size_bytes=system_file.stat().st_size,
        ),
        components={name: ComponentSpec(fmu=fmu_ref(path)) for name, path in components.items()},
        connections=[SystemConnection(from_=a, to=b) for a, b in connections],
        parameters={
            ref: ResolvedValue(value=value, source=layer)
            for ref, (value, layer) in (parameters or {}).items()
        },
        master=MasterAlgorithm(step_size=step_size),
        experiment=Experiment(
            start_time=start_time, stop_time=stop_time, output_interval=step_size
        ),
        outputs=list(outputs) if outputs is not None else default_outputs(components),
        timeout_s=timeout_s,
        estimated_steps=(stop_time - start_time) / step_size,
        versions=Versions.current(),
        omsimulator=omsimulator or oms_pin.installed() or oms_pin.pinned(),
    )


def jacobi_loop(
    steps: int, h: float, pi: Mapping[str, float], plant: Mapping[str, float]
) -> dict[str, np.ndarray]:
    """The PI and the exact-ZOH plant coupled Jacobi-style and sampled after each exchange:
    both step from point k with the inputs exchanged at k (``y_k = x_k``, ``F_k = u_k``)."""
    a = math.exp(-h / plant["tau"])
    u, x, integral = np.zeros(steps + 1), np.zeros(steps + 1), 0.0
    for k in range(steps):
        error = pi["r"] - x[k]
        integral += error * h
        u[k + 1] = pi["Kp"] * error + pi["Ki"] * integral
        x[k + 1] = a * x[k] + plant["K"] * (1.0 - a) * u[k]
    return {"PI.u": u, "PI.y": x, "P.x": x, "P.F": u}
