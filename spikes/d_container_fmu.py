"""Spike (d): FMPy container FMU from two Reference FMUs.

Throwaway evidence for docs/spikes.md, not product code. Builds a container FMU with
``fmpy.container_fmu.cli.create_container_fmu`` (FMPy 0.3.32 does not re-export it from
``fmpy.container_fmu``): Stair.counter -> Feedthrough.Int32_input inside the container,
plus container input ``u`` -> Feedthrough.Float64_continuous_input and container outputs
``y`` (Feedthrough.Float64_continuous_output) and ``counter`` (Feedthrough.Int32_output).
The container is validated, simulated with an input signal, and its outputs are compared
with a standalone Stair run to prove the internal connection works. Done once with FMI 3.0
components in an FMI 3.0 container and once with FMI 2.0 components in an FMI 2.0 one.

Collaborators: FMPy (in-process; also loads the container binary), Reference FMUs
v0.0.41 under ``.cache/reference-fmus``. Outputs go to ``.cache/spikes/d``.

Run inside the dev container::

    uv run python spikes/d_container_fmu.py
"""

import contextlib
import hashlib
import io
import json
import platform
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parent.parent
REF = REPO / ".cache" / "reference-fmus" / "0.0.41"
OUT = REPO / ".cache" / "spikes" / "d"
STEP = 0.1
STOP = 10.0
TYPES = {"2.0": {"real": "Real", "int": "Integer"}, "3.0": {"real": "Float64", "int": "Int32"}}


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def variable(model_description: Any, name: str) -> Any:
    """Return the model variable called name."""
    return next(v for v in model_description.modelVariables if v.name == name)


def container_description(version: str, fixed: bool) -> tuple[Any, dict[str, Any]]:
    """Build the container's own modelDescription and return it with its variables by name.

    Both variants declare ``time`` first, so it gets value reference 0, which the container
    binary reserves for time. The fixed variant also lists the outputs as InitialUnknowns.
    """
    from fmpy.model_description import (
        CoSimulation,
        DefaultExperiment,
        ModelDescription,
        ModelVariable,
        Unknown,
    )

    real, integer = TYPES[version]["real"], TYPES[version]["int"]
    variables = {
        "time": ModelVariable(
            name="time", type=real, causality="independent", variability="continuous"
        ),
        "u": ModelVariable(
            name="u", type=real, causality="input", variability="continuous", start="0"
        ),
        "y": ModelVariable(
            name="y", type=real, causality="output", variability="continuous", initial="calculated"
        ),
        "counter": ModelVariable(
            name="counter",
            type=integer,
            causality="output",
            variability="discrete",
            initial="calculated",
        ),
    }
    description = ModelDescription(
        fmiVersion=version,
        modelName=f"StairFeedthroughContainer{version.replace('.', '')}",
        instantiationToken="{" + str(uuid.uuid4()) + "}",
        coSimulation=CoSimulation(
            modelIdentifier="container_fmu",
            fixedInternalStepSize=STEP,
            canHandleVariableCommunicationStepSize=True,
        ),
        defaultExperiment=DefaultExperiment(startTime="0", stopTime=str(STOP), stepSize=str(STEP)),
        modelVariables=list(variables.values()),
    )
    description.outputs = [Unknown(variable=variables["y"]), Unknown(variable=variables["counter"])]
    if fixed:
        description.initialUnknowns = list(description.outputs)
    return description, variables


def connector(model_variable: Any, fixed: bool) -> Any:
    """Return the connector variable; the fixed variant renames FMI 2 types to FMI 3 names.

    ``write_configuration`` maps Real/Integer to Float64/Int32 for container variables but
    copies connection types verbatim, and the container binary rejects ``Integer``.
    """
    import attrs

    renamed = {"Real": "Float64", "Integer": "Int32"}.get(model_variable.type)
    return attrs.evolve(model_variable, type=renamed) if fixed and renamed else model_variable


def configuration(version: str, fixed: bool) -> Any:
    """Assemble the container Configuration for Stair -> Feedthrough."""
    from fmpy import read_model_description
    from fmpy.container_fmu.config import Component, Configuration, Connection

    stair_path, ft_path = REF / version / "Stair.fmu", REF / version / "Feedthrough.fmu"
    stair_md, ft_md = read_model_description(str(stair_path)), read_model_description(str(ft_path))
    stair = Component(name="stair", filename=stair_path, modelDescription=stair_md)
    feedthrough = Component(name="feedthrough", filename=ft_path, modelDescription=ft_md)
    description, own = container_description(version, fixed)
    connection = Connection(
        startElement=stair,
        startConnectors=[connector(variable(stair_md, "counter"), fixed)],
        endElement=feedthrough,
        endConnectors=[variable(ft_md, "Int32_input")],
    )
    # container.json "variables" entry i serves value reference i + 1; the naive variant also
    # maps time (empty), which shifts every entry and is rejected by the container binary
    mappings = {} if fixed else {own["time"]: []}
    mappings |= {
        own["u"]: [(feedthrough, variable(ft_md, "Float64_continuous_input"))],
        own["y"]: [(feedthrough, variable(ft_md, "Float64_continuous_output"))],
        own["counter"]: [(feedthrough, variable(ft_md, "Int32_output"))],
    }
    return Configuration(
        parallelDoStep=False,
        modelDescription=description,
        components=[stair, feedthrough],
        connections=[connection],
        variableMappings=mappings,
    )


def build(version: str, fixed: bool) -> Path:
    """Create the container FMU; returns its path."""
    from fmpy.container_fmu.cli import create_container_fmu

    label = "fmi" + version.replace(".", "") + ("_fixed" if fixed else "_naive")
    unzipdir = OUT / f"{label}_unzipped"
    unzipdir.mkdir(parents=True, exist_ok=True)  # create_container_fmu rmtree()s this first
    target = OUT / f"StairFeedthrough_{label}.fmu"
    target.unlink(missing_ok=True)
    create_container_fmu(configuration(version, fixed), unzipdir, target)
    return target


def validate(container: Path) -> dict[str, Any]:
    """Run FMPy's validation and inspect the archive."""
    from fmpy import read_model_description
    from fmpy.validation import validate_fmu

    problems = validate_fmu(str(container))
    md = read_model_description(str(container), validate=True)
    names = zipfile.ZipFile(container).namelist()
    return {
        "validate_fmu_problems": [str(p) for p in problems],
        "fmiVersion": md.fmiVersion,
        "variables": [(v.name, v.valueReference, v.causality, v.type) for v in md.modelVariables],
        "archive_entries": len(names),
        "archive_contains_Container_zip": "Container.zip" in names,
        "container_json": json.loads(zipfile.ZipFile(container).read("resources/container.json")),
    }


def transitions(times: Any, values: Any) -> list[tuple[float, int]]:
    """Return (time, new value) wherever an integer signal changes."""
    return [
        (round(float(times[i]), 3), int(values[i]))
        for i in range(1, len(values))
        if values[i] != values[i - 1]
    ]


def simulate(container: Path, version: str, stop_time: float = STOP) -> dict[str, Any]:
    """Simulate the container with a ramp on u; compare counter with a standalone Stair."""
    from fmpy import simulate_fmu

    signal = np.array(
        [(0.0, 0.0), (STOP, 2 * STOP)], dtype=[("time", np.float64), ("u", np.float64)]
    )
    printed = io.StringIO()
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(printed):
            result = simulate_fmu(
                str(container),
                input=signal,
                output=["u", "y", "counter"],
                output_interval=STEP,
                stop_time=stop_time,
            )
    except Exception as error:
        log = [line for line in printed.getvalue().splitlines() if line.strip()]
        return {"error": f"{type(error).__name__}: {error}", "container_log": log}
    wall = round(time.perf_counter() - started, 4)
    stair = simulate_fmu(
        str(REF / version / "Stair.fmu"),
        fmi_type="CoSimulation",
        output_interval=STEP,
        stop_time=stop_time,
    )
    return {
        "wall_s": wall,
        "rows": len(result),
        "t_end": float(result["time"][-1]),
        "max_abs_y_minus_u": float(np.max(np.abs(result["y"] - result["u"]))),
        "container_counter_transitions": transitions(result["time"], result["counter"]),
        "standalone_stair_transitions": transitions(stair["time"], stair["counter"]),
        "sample_t_u_y_counter": [
            (round(float(r["time"]), 2), float(r["u"]), float(r["y"]), int(r["counter"]))
            for r in result[::20]
        ],
    }


def guarded(probe: Any) -> Any:
    """Run a probe and record its exception instead of aborting the spike."""
    try:
        return probe()
    except Exception as error:  # record and continue: this is a spike
        return {"error": f"{type(error).__name__}: {error}"}


def run(version: str, fixed: bool) -> dict[str, Any]:
    """Build, validate and simulate one container."""
    built = guarded(lambda: build(version, fixed))
    if isinstance(built, dict):
        return {"build": built}
    report = {
        "build": {"container": str(built), "bytes": built.stat().st_size},
        "validate": guarded(lambda: validate(built)),
        "simulate": guarded(lambda: simulate(built, version)),
    }
    if "error" in report["simulate"]:  # retry short of Stair's own termination at t=9
        report["simulate_to_8_5"] = guarded(lambda: simulate(built, version, stop_time=8.5))
    return report


def main() -> int:
    """Run naive and fixed containers for both versions, check input hashes, write result.json."""
    import fmpy
    import fmpy.container_fmu

    OUT.mkdir(parents=True, exist_ok=True)
    inputs = [REF / v / n for v in ("2.0", "3.0") for n in ("Stair.fmu", "Feedthrough.fmu")]
    before = {str(p): sha256(p) for p in inputs}
    report: dict[str, Any] = {
        "versions": {
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "fmpy": fmpy.__version__,
        },
        "import_paths": {
            "fmpy.container_fmu.create_container_fmu": hasattr(
                fmpy.container_fmu, "create_container_fmu"
            ),
            "fmpy.container_fmu.cli.create_container_fmu": True,
        },
    }
    for version in ("3.0", "2.0"):
        for fixed in (False, True):
            label = "fmi" + version.replace(".", "") + ("_fixed" if fixed else "_naive")
            report[label] = run(version, fixed)
    after = {str(p): sha256(p) for p in inputs}
    report["input_sha256"] = {"before": before, "after": after, "unchanged": before == after}
    (OUT / "result.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
