"""Spike (c): OMSimulator 3 SSP round trip and step-wise control.

Throwaway evidence for docs/spikes.md, not product code. Builds a two-FMU SSP
(Feedthrough A.Float64_continuous_output -> B.Float64_continuous_input) with the
OMSimulator 3 Python API (``SSP``, ``CRef``), exports it, re-imports it, simulates it and
checks the connected signal in the CSV result file. Then it probes step-wise control with
the high-level ``InstantiatedModel`` API and with the low-level ``Capi`` wrapper around
the ``oms_*`` C functions, checks whether two instantiated models can coexist in one
process, and runs a crashing ``Capi`` call in a child process. FMI 2.0 and FMI 3.0
Feedthrough are both exercised; FMI 3.0 runs with and without excluding its Binary
variables from the result file.

Collaborators: OMSimulator (loaded in-process), Reference FMUs v0.0.41 under
``.cache/reference-fmus``. All outputs go to ``.cache/spikes/c``.

Run inside the dev container::

    uv run python spikes/c_omsimulator.py
"""

import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REF = REPO / ".cache" / "reference-fmus" / "0.0.41"
OUT = REPO / ".cache" / "spikes" / "c"
LEAK_DIR = OUT / "process-tmp"
IN_PORT = "Float64_continuous_input"
OUT_PORT = "Float64_continuous_output"
BINARY_SIGNALS = ".*Binary.*"
SSD_VALUE = 3.0
BATCH_VALUE = 4.0
STEP_VALUE_BEFORE = 4.0
STEP_VALUE_AFTER = 7.0
STEPS = 10
SEGFAULT_CHILD = """
import sys
from OMSimulator import SSP, Capi
inst = SSP(sys.argv[1], temp_dir=sys.argv[2]).instantiate()
inst.setResultFile(sys.argv[3])
print("calling Capi.getResultFileSignals('model')", flush=True)
print(Capi.getResultFileSignals("model"), flush=True)
"""


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def crefs(system: str) -> dict[str, Any]:
    """Return the CRefs used throughout the spike for a root system name."""
    from OMSimulator import CRef

    return {
        "a_in": CRef(system, "A", IN_PORT),
        "a_out": CRef(system, "A", OUT_PORT),
        "b_out": CRef(system, "B", OUT_PORT),
        "b_in": CRef(system, "B", IN_PORT),
    }


def plain(value: Any) -> Any:
    """Unwrap OMSimulator value objects (Float64 etc.) into JSON-friendly values."""
    return getattr(value, "value", value)


def build_ssp(fmu: Path, target: Path) -> None:
    """Build A -> B from one Feedthrough FMU, bind A's input to SSD_VALUE, export to target."""
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=str(OUT / "ssp-tmp"))
    model.addResource(str(fmu), new_name="resources/Feedthrough.fmu")
    model.addComponent(CRef("default", "A"), "resources/Feedthrough.fmu")
    model.addComponent(CRef("default", "B"), "resources/Feedthrough.fmu")
    refs = crefs("default")
    model.addConnection(refs["a_out"], refs["b_in"])
    model.setValue(refs["a_in"], SSD_VALUE)
    model.export(str(target))


def reimport(ssp_path: Path) -> Any:
    """Load an exported SSP back from disk."""
    from OMSimulator import SSP

    return SSP(str(ssp_path), temp_dir=str(OUT / "ssp-tmp"))


def instantiate(ssp_path: Path, result: Path, exclude_binary: bool) -> Any:
    """Re-import and instantiate an SSP with a CSV result file."""
    inst = reimport(ssp_path).instantiate()
    inst.setResultFile(str(result))
    if exclude_binary:
        inst.removeSignalsFromResults(BINARY_SIGNALS)
    return inst


def read_csv(path: Path) -> tuple[list[str], list[list[float]]]:
    """Read an OMSimulator CSV result file into a header and float rows."""
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        header = [name.strip() for name in next(reader)]
        rows = [[float(cell) for cell in row] for row in reader if row]
    return header, rows


def column(header: list[str], suffix: str) -> int:
    """Return the index of the single column whose name ends with suffix."""
    matches = [i for i, name in enumerate(header) if name.endswith(suffix)]
    if len(matches) != 1:
        raise KeyError(f"expected one column ending in {suffix!r}, got {matches}")
    return matches[0]


def describe_reimport(ssp_path: Path) -> dict[str, Any]:
    """Show that the connection and the inline binding survive export and re-import."""
    model = reimport(ssp_path)
    return {
        "ssp": str(ssp_path),
        "ssp_bytes": ssp_path.stat().st_size,
        "reimported_connections": [
            f"{c.startElement}.{c.startConnector} -> {c.endElement}.{c.endConnector}"
            for c in model.activeVariant.system.connections
        ],
        "reimported_binding_A_input": repr(plain(model.getValue(crefs("default")["a_in"]))),
    }


def batch(ssp_path: Path, label: str, exclude_binary: bool) -> dict[str, Any]:
    """Instantiate, override A's input, simulate to the end and check B's output column."""
    refs = crefs("default")
    result = OUT / f"batch_{label}.csv"
    inst = instantiate(ssp_path, result, exclude_binary)
    inst.setValue(refs["a_in"], BATCH_VALUE)
    started = time.perf_counter()
    inst.initialize()
    inst.simulate()
    wall = round(time.perf_counter() - started, 4)
    end_time = inst.getTime()
    inst.terminate()
    inst.delete()
    header, rows = read_csv(result)
    values = sorted({row[column(header, f".B.{OUT_PORT}")] for row in rows})
    return {
        "exclude_binary_from_results": exclude_binary,
        "simulate_wall_s": wall,
        "end_time": end_time,
        "result_file": str(result),
        "result_rows": len(rows),
        "B_out_values": values,
        "connection_ok": values == [BATCH_VALUE],
    }


def step_high_level(ssp_path: Path, label: str, exclude_binary: bool) -> dict[str, Any]:
    """One doStep, then STEPS stepUntil increments; A's input changes after the 5th."""
    refs = crefs("default")
    result = OUT / f"stepped_{label}.csv"
    inst = instantiate(ssp_path, result, exclude_binary)
    inst.setValue(refs["a_in"], STEP_VALUE_BEFORE)
    inst.initialize()
    inst.doStep()
    do_step_time = inst.getTime()
    stop = inst.getStopTime()
    trace = []
    for k in range(1, STEPS + 1):
        if k == STEPS // 2 + 1:
            inst.setValue(refs["a_in"], STEP_VALUE_AFTER)
        started = time.perf_counter()
        inst.stepUntil(stop * k / STEPS)
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        trace.append(
            {
                "t": round(inst.getTime(), 9),
                "A_in": plain(inst.getValue(refs["a_in"])),
                "B_out": plain(inst.getValue(refs["b_out"])),
                "wall_ms": wall_ms,
            }
        )
    inst.terminate()
    inst.delete()
    header, rows = read_csv(result)
    b_col = column(header, f".B.{OUT_PORT}")
    switch = next((row[0] for row in rows if row[b_col] == STEP_VALUE_AFTER), None)
    return {
        "time_after_one_doStep": round(do_step_time, 9),
        "trace": trace,
        "result_rows": len(rows),
        "first_result_time_with_B_out_after_value": switch,
        "ok": trace[STEPS // 2 - 1]["B_out"] == STEP_VALUE_BEFORE
        and trace[STEPS // 2]["B_out"] == STEP_VALUE_AFTER,
    }


def step_low_level(ssp_path: Path) -> dict[str, Any]:
    """Drive the model through Capi (oms_initialize/stepUntil/setReal/getReal/getTime)."""
    from OMSimulator import Capi

    instantiate(ssp_path, OUT / "stepped_capi.csv", exclude_binary=False)
    a_in, b_out = f"model.root.A.{IN_PORT}", f"model.root.B.{OUT_PORT}"
    statuses = [Capi.setReal(a_in, STEP_VALUE_BEFORE), Capi.initialize("model")]
    trace = []
    for k in range(1, STEPS + 1):
        if k == STEPS // 2 + 1:
            statuses.append(Capi.setReal(a_in, STEP_VALUE_AFTER))
        statuses.append(Capi.stepUntil("model", k / STEPS))
        now, _ = Capi.getTime("model")
        value, status = Capi.getReal(b_out)
        statuses.append(status)
        trace.append({"t": round(now, 9), "B_out": value})
    statuses += [Capi.terminate("model"), Capi.delete("model")]
    return {
        "calls": "Capi.setReal / initialize / stepUntil / getTime / getReal / terminate / delete",
        "trace": trace,
        "distinct_statuses": sorted({str(s) for s in statuses}),
        "ok": trace[STEPS // 2 - 1]["B_out"] == STEP_VALUE_BEFORE
        and trace[STEPS // 2]["B_out"] == STEP_VALUE_AFTER,
    }


def two_instances(ssp_path: Path) -> dict[str, Any]:
    """Check whether a second InstantiatedModel can exist while the first is alive."""
    first = reimport(ssp_path).instantiate()
    outcome: dict[str, Any] = {"first_model_name": first.modelName}
    try:
        second = reimport(ssp_path).instantiate()
        outcome["second"] = f"created, modelName={second.modelName}"
        second.delete()
    except Exception as error:  # the spike records whatever the library raises
        outcome["second"] = f"{type(error).__name__}: {error}"
    first.delete()
    return outcome


def segfault_probe(ssp_path: Path) -> dict[str, Any]:
    """Run Capi.getResultFileSignals in a child process and record how it exits."""
    args = [str(ssp_path), str(OUT / "ssp-tmp"), str(OUT / "segfault_probe.csv")]
    child = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", SEGFAULT_CHILD, *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TMPDIR": str(LEAK_DIR)},
    )
    fault = [line for line in child.stderr.splitlines() if "Fatal Python error" in line]
    return {
        "returncode": child.returncode,
        "stdout_tail": child.stdout.splitlines()[-1:],
        "fault": fault,
    }


def legacy_api_surface() -> dict[str, Any]:
    """Record which legacy entry points exist in the installed package and native library."""
    import OMSimulator
    from OMSimulator import Capi

    native = ["oms_importFile", "oms_export", "oms_getState", "oms_setState", "oms_doStep"]
    return {
        "has_OMSimulator_class": hasattr(OMSimulator, "OMSimulator"),
        "public_names": sorted(n for n in dir(OMSimulator) if n[0].isupper()),
        "capi_python_methods": sorted(n for n in dir(Capi) if not n.startswith("_")),
        "native_symbols_present": {name: hasattr(Capi.obj, name) for name in native},
    }


def guarded(probe: Any) -> Any:
    """Run a probe and record its exception instead of aborting the spike."""
    try:
        return probe()
    except Exception as error:  # record and continue: this is a spike
        return {"error": f"{type(error).__name__}: {error}"}


def version_section(fmu: Path, label: str, exclude_binary: bool) -> dict[str, Any]:
    """Run reimport, batch and stepping probes for one Feedthrough FMU."""
    ssp_path = OUT / f"feedthrough_chain_{label.split('_')[0]}.ssp"
    if not ssp_path.exists():
        build_ssp(fmu, ssp_path)
    return {
        "reimport": guarded(lambda: describe_reimport(ssp_path)),
        "batch": guarded(lambda: batch(ssp_path, label, exclude_binary)),
        "step_high_level": guarded(lambda: step_high_level(ssp_path, label, exclude_binary)),
    }


def main() -> int:
    """Run every probe, write result.json and print it."""
    OUT.mkdir(parents=True, exist_ok=True)
    LEAK_DIR.mkdir(exist_ok=True)
    tempfile.tempdir = str(LEAK_DIR)  # InstantiatedModel calls mkdtemp() and never removes it
    import OMSimulator

    fmu2, fmu3 = REF / "2.0" / "Feedthrough.fmu", REF / "3.0" / "Feedthrough.fmu"
    before = {str(p): sha256(p) for p in (fmu2, fmu3)}
    chain2 = OUT / "feedthrough_chain_fmi20.ssp"
    report: dict[str, Any] = {
        "versions": {
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "OMSimulator": OMSimulator.__version__,
            "native": OMSimulator.version,
        },
        "legacy_api": legacy_api_surface(),
        "fmi20": version_section(fmu2, "fmi20", exclude_binary=False),
        "fmi30_as_is": version_section(fmu3, "fmi30_as-is", exclude_binary=False),
        "fmi30_binary_excluded": version_section(fmu3, "fmi30_no-binary", exclude_binary=True),
        "step_low_level_capi": guarded(lambda: step_low_level(chain2)),
        "two_instances": guarded(lambda: two_instances(chain2)),
        "segfault_probe": guarded(lambda: segfault_probe(chain2)),
    }
    report["leaked_process_tmp_dirs"] = len(list(LEAK_DIR.iterdir()))
    after = {str(p): sha256(p) for p in (fmu2, fmu3)}
    report["input_sha256"] = {"before": before, "after": after, "unchanged": before == after}
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
