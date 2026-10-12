"""Spike (g): closed loop of a pythonfmu PI controller and an omc MSD plant in OMSimulator.

Throwaway evidence for docs/spikes.md, not product code. It builds the two skill examples
(``skills/fmu-authoring/examples/pi_controller.py`` with pythonfmu, ``MSD.mo`` with omc and
``--fmiFlags=s:cvode``) into ``.cache/spikes/g/fmus``, wires them in OMSimulator 3
(PI.u -> MSD.F, MSD.x -> PI.y, r = 1) and runs the loop for 10 s at communication steps
0.01 and 0.1 in child Python processes, the way a worker would. It compares the result with
an FMPy co-simulation master (Jacobi and Gauss-Seidel), an exact sampled-data reference
and the continuous-time loop. It then steps the system with ``stepUntil``/``doStep``,
changes ``Kp`` mid-run, exports and re-imports an SSP, checks byte reproducibility, applies
``Kp`` from SSV files (written by the oms3 API and by hand), records where
``libOMSimulator.so`` comes from and re-checks the FMI 3.0 Binary result-file issue.

Collaborators: pythonfmu (build CLI), omc (``/usr/bin/omc``), OMSimulator and FMPy (only in
child processes), numpy and lxml (parent). All outputs go to ``.cache/spikes/g``.

Run inside the dev container::

    uv run --no-sync python spikes/g_closed_loop.py
"""

import base64
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from itertools import pairwise
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / ".cache" / "spikes" / "g"
EXAMPLES = REPO / "skills" / "fmu-authoring" / "examples"
PI_SRC = EXAMPLES / "pi_controller.py"
MSD_SRC = EXAMPLES / "MSD.mo"
FEEDTHROUGH3 = REPO / ".cache" / "reference-fmus" / "0.0.41" / "3.0" / "Feedthrough.fmu"
FMUS = OUT / "fmus"
PI_FMU = FMUS / "PIController.fmu"
MSD_FMU = FMUS / "MSD.fmu"

STOP = 10.0
R = 1.0
KP, KI = 10.0, 15.0
MASS, SPRING, DAMPING = 1.0, 10.0, 2.0
STEPS = (0.01, 0.1)
KP_SWITCH_T, KP_NEW = 1.0, 40.0
SSV_API_KP, SSV_FILE_KP = 40.0, 2.5
CHUNK = 0.1
TIMEOUT = 300

MSD_MOS = """setCommandLineOptions("--fmiFlags=s:cvode"); getErrorString();
loadFile("{mo}"); getErrorString();
buildModelFMU(MSD, version="2.0", fmuType="me_cs"); getErrorString();
"""

HANDWRITTEN_SSV = """<?xml version="1.0" encoding="UTF-8"?>
<ssv:ParameterSet xmlns:ssc="http://ssp-standard.org/SSP1/SystemStructureCommon"
                  xmlns:ssv="http://ssp-standard.org/SSP1/SystemStructureParameterValues"
                  version="1.0" name="pi_gains">
  <ssv:Parameters>
    <ssv:Parameter name="Kp">
      <ssv:Real value="{kp}"/>
    </ssv:Parameter>
  </ssv:Parameters>
</ssv:ParameterSet>
"""


# ---------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> dict[str, list[float]]:
    """Read an OMSimulator CSV result file into columns keyed by header name."""
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        header = [name.strip() for name in next(reader)]
        rows = [[float(cell) for cell in row] for row in reader if row]
    return {name: [row[i] for row in rows] for i, name in enumerate(header)}


def pick(columns: dict[str, list[float]], suffix: str) -> list[float]:
    """Return the single column whose name ends with suffix."""
    names = [name for name in columns if name.endswith(suffix)]
    if len(names) != 1:
        raise KeyError(f"expected one column ending in {suffix!r}, got {names}")
    return columns[names[0]]


def signals(columns: dict[str, list[float]]) -> dict[str, Any]:
    """Extract t, x, u, F, y (and Kp if present) from an OMSimulator result.

    OMSimulator writes two rows per communication point (after doStep, then after the input
    exchange) and more at the stop time. The series keep the first row per printed time
    (the post-step values); the raw row structure is reported separately.
    """
    times = columns["time"]
    first = [i for i, t in enumerate(times) if i == 0 or t != times[i - 1]]
    raw: dict[str, list[float]] = {"t": times}
    for key, suffix in {"x": ".MSD.x", "u": ".PI.u", "F": ".MSD.F", "y": ".PI.y"}.items():
        raw[key] = pick(columns, suffix)
    kp = [name for name in columns if name.endswith(".PI.Kp")]
    if kp:
        raw["Kp"] = columns[kp[0]]
    out: dict[str, Any] = {key: [values[i] for i in first] for key, values in raw.items()}
    counts = [b - a for a, b in zip(first, [*first[1:], len(times)], strict=True)]
    last = times[-1]
    out["rows"] = {
        "raw": len(times),
        "unique_times": len(first),
        "rows_per_time": {str(n): counts.count(n) for n in sorted(set(counts))},
        "rows_at_stop_t_x_u_F_y": [
            [raw[k][i] for k in ("t", "x", "u", "F", "y")] for i, t in enumerate(times) if t == last
        ],
    }
    out["header"] = list(columns)
    return out


# ---------------------------------------------------------------------------------------------
# Child processes (OMSimulator and FMPy load native code only here)
# ---------------------------------------------------------------------------------------------


def native_threads() -> int:
    """Return the number of OS threads of this process."""
    return len(os.listdir("/proc/self/task"))


def lib_mapped() -> bool:
    """Return whether libOMSimulator.so is mapped into this process."""
    return "libOMSimulator.so" in Path("/proc/self/maps").read_text()


def build_system(cfg: dict[str, Any]) -> Any:
    """Build the PI + MSD SSP in memory with the oms3 API."""
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=cfg["tmp"])
    model.addResource(cfg["pi_fmu"], new_name="resources/PIController.fmu")
    model.addResource(cfg["msd_fmu"], new_name="resources/MSD.fmu")
    model.addComponent(CRef("default", "PI"), "resources/PIController.fmu")
    model.addComponent(CRef("default", "MSD"), "resources/MSD.fmu")
    model.addConnection(CRef("default", "PI", "u"), CRef("default", "MSD", "F"))
    model.addConnection(CRef("default", "MSD", "x"), CRef("default", "PI", "y"))
    if cfg.get("bind_r", True):
        model.setValue(CRef("default", "PI", "r"), R)
    solver: dict[str, Any] = {"name": "ma", "method": "oms_ma", "fixedStepSize": cfg["h"]}
    if cfg.get("rtol") is not None:
        solver["relativeTolerance"] = cfg["rtol"]
    model.newSolver(solver)
    model.setSolver(CRef("default", "PI"), "ma")
    model.setSolver(CRef("default", "MSD"), "ma")
    model.activeVariant.stopTime = STOP
    return model


def load_system(cfg: dict[str, Any]) -> Any:
    """Build the system or import it from an SSP file, then apply an optional SSV."""
    from OMSimulator import SSP, SSV, CRef

    model = SSP(cfg["ssp_in"], temp_dir=cfg["tmp"]) if cfg.get("ssp_in") else build_system(cfg)
    ssv = cfg.get("ssv")
    if ssv:
        path = ssv["path"]
        if ssv["mode"] == "api":  # the oms3 SSV writer; "hand" files are written by the parent
            values = SSV()
            values.setValue(ssv["name"], ssv["kp"])
            values.export(path)
        model.addResource(path, new_name=ssv["resource"])
        model.addSSVReference(CRef(*ssv["target"]), ssv["resource"])
    for path in cfg.get("exports", []):
        model.export(path)
    return model


def with_heartbeat(work: Any) -> tuple[float, dict[str, Any]]:
    """Run work() on a second thread while this thread ticks every ms; report the gaps."""
    beats: list[float] = []
    errors: list[str] = []
    done = threading.Event()

    def target() -> None:
        try:
            work()
        except BaseException as error:  # record and re-raise in the caller
            errors.append(f"{type(error).__name__}: {error}")
        finally:
            done.set()

    worker = threading.Thread(target=target, name="oms-simulate")
    started = time.perf_counter()
    worker.start()
    peak_threads = native_threads()
    while not done.is_set():
        beats.append(time.perf_counter())
        peak_threads = max(peak_threads, native_threads())
        time.sleep(0.001)
    worker.join()
    wall = time.perf_counter() - started
    if errors:
        raise RuntimeError(errors[0])
    gaps = [b - a for a, b in pairwise(beats)] or [0.0]
    return wall, {
        "heartbeats": len(beats),
        "max_gap_ms": round(max(gaps) * 1000, 3),
        "median_gap_ms": round(statistics.median(gaps) * 1000, 3),
        "peak_os_threads": peak_threads,
    }


def python_side_effects(path_before: list[str]) -> dict[str, Any]:
    """Report which pythonfmu and slave modules the FMU loaded into this interpreter."""
    modules = {
        name: getattr(module, "__file__", None)
        for name, module in sys.modules.items()
        if name.split(".")[0] in ("pythonfmu", "pi_controller")
    }
    return {
        "pythonfmu_file": modules.get("pythonfmu"),
        "pi_controller_file": modules.get("pi_controller"),
        "loaded_modules": sorted(modules),
        "sys_path_added": [p for p in sys.path if p not in path_before],
        "LD_PRELOAD": os.environ.get("LD_PRELOAD"),
    }


def time_add_submodel(timings: list[list[Any]]) -> None:
    """Wrap Capi.addSubModel so each call's wall time (FMU extraction + load) is recorded."""
    from OMSimulator import Capi

    original = Capi.addSubModel

    def timed(cref: str, path: str) -> Any:
        started = time.perf_counter()
        status = original(cref, path)
        timings.append([cref, round(time.perf_counter() - started, 3)])
        return status

    Capi.addSubModel = timed


def child_oms_batch(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build or import the loop, simulate it on a worker thread and read the result file."""
    import warnings

    path_before = list(sys.path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import OMSimulator

        report: dict[str, Any] = {
            "version": OMSimulator.version,
            "lib_mapped_after_import": lib_mapped(),
            "threads_after_import": native_threads(),
            "addSubModel_s": [],
        }
        time_add_submodel(report["addSubModel_s"])
        model = load_system(cfg)
        started = time.perf_counter()
        inst = model.instantiate()
        report["instantiate_s"] = round(time.perf_counter() - started, 4)
        inst.setResultFile(cfg["result"])
        if cfg.get("exclude_binary"):
            inst.removeSignalsFromResults(".*Binary.*")
        started = time.perf_counter()
        inst.initialize()
        report["initialize_s"] = round(time.perf_counter() - started, 4)
        report["Kp_after_initialize"] = probe_value(inst, "PI", "Kp")
        if cfg.get("simulate", True):
            wall, beat = with_heartbeat(inst.simulate)
            report |= {"simulate_s": round(wall, 4), "gil_probe": beat}
        report["end_time"] = repr(inst.getTime())
        report["api_calls"] = inst.dumpApiCalls().splitlines()
        inst.terminate()
        started = time.perf_counter()
        inst.delete()
        report["delete_s"] = round(time.perf_counter() - started, 4)
    report["warnings"] = [str(w.message)[:400] for w in caught]
    report["python"] = python_side_effects(path_before)
    if cfg.get("simulate", True) and cfg["result"].endswith(".csv"):
        report["signals"] = signals(read_csv(Path(cfg["result"])))
    return report


def probe_value(inst: Any, component: str, name: str) -> Any:
    """Read one variable from an instantiated model, or the error text if that fails."""
    from OMSimulator import CRef

    try:
        return inst.getValue(CRef("default", component, name))
    except Exception as error:  # record whatever the library raises
        return f"{type(error).__name__}: {error}"


def child_oms_stepped(cfg: dict[str, Any]) -> dict[str, Any]:
    """doStep once, then stepUntil in CHUNK increments; switch Kp at KP_SWITCH_T."""
    from OMSimulator import CRef

    model = load_system(cfg)
    inst = model.instantiate()
    inst.setResultFile(cfg["result"])
    inst.addSignalsToResults(".*Kp")
    inst.initialize()
    inst.doStep()
    first_step_time = inst.getTime()
    trace, walls, missed = [], [], []
    for k in range(1, round(STOP / CHUNK) + 1):
        target = round(k * CHUNK, 9)
        if abs(inst.getTime() - KP_SWITCH_T) < 1e-6:
            status = inst.setValue(CRef("default", "PI", "Kp"), KP_NEW)
            trace.append({"event": "setValue(PI.Kp)", "t": inst.getTime(), "status": str(status)})
        started = time.perf_counter()
        inst.stepUntil(target)
        walls.append(time.perf_counter() - started)
        if abs(inst.getTime() - target) > 1e-9:
            missed.append([target, repr(inst.getTime())])
        if k % 5 == 0 or abs(target - KP_SWITCH_T) < 0.25:
            trace.append(
                {
                    "t": round(inst.getTime(), 9),
                    "Kp": probe_value(inst, "PI", "Kp"),
                    "u": probe_value(inst, "PI", "u"),
                    "x": probe_value(inst, "MSD", "x"),
                }
            )
    inst.terminate()
    inst.delete()
    return {
        "time_after_first_doStep": first_step_time,
        "stepUntil_targets_missed": {"count": len(missed), "first": missed[:4]},
        "stepUntil_ms": {
            "count": len(walls),
            "median": round(statistics.median(walls) * 1000, 3),
            "max": round(max(walls) * 1000, 3),
        },
        "trace": trace,
        "signals": signals(read_csv(Path(cfg["result"]))),
    }


def fmpy_slave(path: str, name: str, unzip: Path) -> tuple[Any, dict[str, int]]:
    """Extract an FMU and create an FMPy FMU2Slave for it."""
    from fmpy import extract, read_model_description
    from fmpy.fmi2 import FMU2Slave

    description = read_model_description(path)
    directory = extract(path, unzipdir=str(unzip))
    slave = FMU2Slave(
        guid=description.guid,
        unzipDirectory=directory,
        modelIdentifier=description.coSimulation.modelIdentifier,
        instanceName=name,
    )
    return slave, {v.name: v.valueReference for v in description.modelVariables}


def time_grid(h: float, accumulated: bool) -> list[float]:
    """Communication points: k*h, or t += min(h, STOP - t) the way OMSimulator advances."""
    if not accumulated:
        return [k * h for k in range(round(STOP / h) + 1)]
    grid = [0.0]
    while grid[-1] < STOP:
        grid.append(grid[-1] + min(h, STOP - grid[-1]))
    return grid


def child_fmpy_master(cfg: dict[str, Any]) -> dict[str, Any]:
    """Co-simulate PI and MSD with FMPy at step h, Jacobi or Gauss-Seidel (PI first)."""
    h, scheme = cfg["h"], cfg["scheme"]
    pi, pvr = fmpy_slave(cfg["pi_fmu"], "PI", Path(cfg["tmp"]) / "PI")
    msd, mvr = fmpy_slave(cfg["msd_fmu"], "MSD", Path(cfg["tmp"]) / "MSD")
    started = time.perf_counter()
    for slave in (pi, msd):
        slave.instantiate()
        slave.setupExperiment(tolerance=cfg.get("tolerance"), startTime=0.0, stopTime=STOP)
    pi.setReal([pvr["r"], pvr["Kp"]], [R, cfg.get("kp_start", KP)])
    for slave in (pi, msd):
        slave.enterInitializationMode()
        slave.exitInitializationMode()
    t, x, u = [0.0], [msd.getReal([mvr["x"]])[0]], [pi.getReal([pvr["u"]])[0]]
    grid = time_grid(h, cfg.get("grid") == "accumulated")
    tiny = None
    for now, after in pairwise(grid):
        step = after - now
        if cfg.get("kp_switch") and abs(now - KP_SWITCH_T) < h / 2:
            pi.setReal([pvr["Kp"]], [KP_NEW])
        pi.setReal([pvr["y"]], [x[-1]])
        if scheme == "jacobi":
            msd.setReal([mvr["F"]], [u[-1]])
            pi.doStep(now, step)
            msd.doStep(now, step)
        else:
            pi.doStep(now, step)
            msd.setReal([mvr["F"]], pi.getReal([pvr["u"]]))
            msd.doStep(now, step)
        x_now, u_now = msd.getReal([mvr["x"]])[0], pi.getReal([pvr["u"]])[0]
        if step < h / 2:  # OMSimulator-style final sliver step: report it, keep it off the grid
            tiny = {"from": repr(now), "step": step, "u_before": u[-1], "u_after": u_now}
            continue
        t.append(after)
        x.append(x_now)
        u.append(u_now)
    wall = time.perf_counter() - started
    for slave in (pi, msd):
        slave.terminate()
        slave.freeInstance()
    return {"wall_s": round(wall, 4), "final_tiny_step": tiny, "signals": {"t": t, "x": x, "u": u}}


def child_fmi3_binary(cfg: dict[str, Any]) -> dict[str, Any]:
    """Simulate one FMI 3.0 Feedthrough, with or without Binary signals in the result file."""
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=cfg["tmp"])
    model.addResource(cfg["fmu"], new_name="resources/Feedthrough.fmu")
    model.addComponent(CRef("default", "A"), "resources/Feedthrough.fmu")
    inst = model.instantiate()
    inst.setResultFile(cfg["result"])
    if cfg["exclude_binary"]:
        inst.removeSignalsFromResults(".*Binary.*")
    inst.initialize()
    started = time.perf_counter()
    inst.simulate()
    wall = round(time.perf_counter() - started, 4)
    inst.terminate()
    inst.delete()
    columns = read_csv(Path(cfg["result"]))
    return {"simulate_s": wall, "rows": len(columns["time"]), "columns": len(columns)}


CHILDREN = {
    "oms_batch": child_oms_batch,
    "oms_stepped": child_oms_stepped,
    "fmpy_master": child_fmpy_master,
    "fmi3_binary": child_fmi3_binary,
}


def child_main(name: str, cfg_path: str) -> int:
    """Run one child probe and write its JSON report next to its config."""
    cfg = json.loads(Path(cfg_path).read_text())
    tempfile.tempdir = cfg["tmp"]  # OMSimulator's InstantiatedModel calls mkdtemp() and leaks
    try:
        report = {"ok": True} | CHILDREN[name](cfg)
    except Exception as error:  # record and return: this is a spike
        report = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    Path(cfg["report"]).write_text(json.dumps(report))
    return 0


# ---------------------------------------------------------------------------------------------
# Parent: builds, child orchestration, references, comparisons
# ---------------------------------------------------------------------------------------------


def run(cmd: list[str], cwd: Path, timeout: float = TIMEOUT) -> dict[str, Any]:
    """Run a command and record exit code, wall time and output tails."""
    started = time.perf_counter()
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return {
        "cmd": " ".join(cmd),
        "returncode": done.returncode,
        "wall_s": round(time.perf_counter() - started, 2),
        "stdout": done.stdout.strip().splitlines()[-8:],
        "stderr": done.stderr.strip().splitlines()[-8:],
    }


def build_pi() -> dict[str, Any]:
    """Copy the PI example and build it with the pythonfmu CLI."""
    src = OUT / "src"
    src.mkdir(parents=True, exist_ok=True)
    script = src / PI_SRC.name
    shutil.copyfile(PI_SRC, script)
    PI_FMU.unlink(missing_ok=True)
    cmd = ["uv", "run", "--no-sync", "python", "-I", "-B", "-m", "pythonfmu", "build"]
    outcome = run([*cmd, "-f", str(script), "-d", str(FMUS)], cwd=REPO)
    return outcome | {"fmu": str(PI_FMU), "exists": PI_FMU.exists()}


def build_msd() -> dict[str, Any]:
    """Build MSD.mo with omc (s:cvode, FMI 2.0, me_cs) in a scratch dir and copy the FMU out."""
    scratch = OUT / "omc"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    model = scratch / MSD_SRC.name
    shutil.copyfile(MSD_SRC, model)
    (scratch / "build.mos").write_text(MSD_MOS.format(mo=model))
    outcome = run(["omc", "build.mos"], cwd=scratch)
    built = scratch / "MSD.fmu"
    MSD_FMU.unlink(missing_ok=True)
    if built.exists():
        shutil.copyfile(built, MSD_FMU)
    return outcome | {"fmu": str(MSD_FMU), "exists": MSD_FMU.exists()}


def describe_fmu(path: Path) -> dict[str, Any]:
    """Summarize an FMU archive: size, hash, binaries, interfaces and NEEDED libs."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        xml = archive.read("modelDescription.xml").decode()
        binaries = [n for n in names if n.startswith("binaries/") and not n.endswith("/")]
        so = next(n for n in binaries if n.startswith("binaries/linux64/") and "sundials" not in n)
        target = OUT / "inspect" / path.stem
        target.mkdir(parents=True, exist_ok=True)
        lib = target / Path(so).name
        lib.write_bytes(archive.read(so))
    dynamic = subprocess.run(["readelf", "-d", str(lib)], capture_output=True, text=True).stdout
    return {
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "binaries": binaries,
        "interfaces": [i for i in ("ModelExchange", "CoSimulation") if f"<{i}" in xml],
        "needsExecutionTool": 'needsExecutionTool="true"' in xml,
        "needed": re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", dynamic),
    }


def run_child(
    name: str, label: str, cfg: dict[str, Any], tmp_root: Path | None = None
) -> dict[str, Any]:
    """Run a child probe in a fresh Python process (like a worker) and load its report."""
    tmp = (tmp_root or OUT / "tmp") / label
    work = OUT / "run" / label
    for directory in (tmp, work, OUT / "logs", OUT / "children"):
        directory.mkdir(parents=True, exist_ok=True)
    cfg = {"tmp": str(tmp), "report": str(OUT / "children" / f"{label}.json")} | cfg
    cfg_path = OUT / "children" / f"{label}.cfg.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    Path(cfg["report"]).unlink(missing_ok=True)
    cmd = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", name]
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"} | {"TMPDIR": str(tmp)}
    started = time.perf_counter()
    try:
        done = subprocess.run(
            [*cmd, str(cfg_path)],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
        code, stdout, stderr = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as expired:
        code, stdout, stderr = "timeout", str(expired.stdout or ""), str(expired.stderr or "")
    (OUT / "logs" / f"{label}.stdout.txt").write_text(stdout)
    (OUT / "logs" / f"{label}.stderr.txt").write_text(stderr)
    report_path = Path(cfg["report"])
    report = json.loads(report_path.read_text()) if report_path.exists() else {"ok": False}
    errors = [line for line in stderr.splitlines() if line.startswith("error")]
    return report | {
        "process": {
            "returncode": code,
            "wall_s": round(time.perf_counter() - started, 3),
            "stdout_lines": len(stdout.splitlines()),
            "stderr_lines": len(stderr.splitlines()),
            "stderr_error_lines": len(errors),
            "stderr_head": stderr.splitlines()[:4],
            "leaked_tmp_entries": len(list(tmp.iterdir())),
        }
    }


def base_cfg(h: float, label: str, **extra: Any) -> dict[str, Any]:
    """Return a child config for the PI + MSD loop at step h."""
    cfg = {"h": h, "pi_fmu": str(PI_FMU), "msd_fmu": str(MSD_FMU)}
    return cfg | {"result": str(OUT / "results" / f"{label}.csv")} | extra


# ----- references -----------------------------------------------------------------------------


def expm(matrix: Any) -> Any:
    """Matrix exponential by scaling and squaring of a Taylor series (small dense matrices)."""
    import numpy as np

    norm = float(np.abs(matrix).sum(axis=1).max())
    squarings = max(0, int(np.ceil(np.log2(norm))) + 1) if norm > 0 else 0
    scaled = matrix / 2**squarings
    result = term = np.eye(matrix.shape[0])
    for k in range(1, 25):
        term = term @ scaled / k
        result = result + term
    for _ in range(squarings):
        result = result @ result
    return result


def plant_zoh(h: float) -> tuple[Any, Any]:
    """Exact zero-order-hold discretization of the MSD plant: x_{k+1} = Ad x_k + Bd F_k."""
    import numpy as np

    augmented = np.zeros((3, 3))
    augmented[:2, :2] = [[0.0, 1.0], [-SPRING / MASS, -DAMPING / MASS]]
    augmented[1, 2] = 1.0 / MASS
    phi = expm(augmented * h)
    return phi[:2, :2], phi[:2, 2]


def sampled_reference(h: float, scheme: str, kp_switch: bool, kp_start: float = KP) -> dict:
    """Exact sampled-data loop: discrete PI (as coded in the FMU) + ZOH plant, given scheme."""
    import numpy as np

    ad, bd = plant_zoh(h)
    state, integral, u, kp = np.zeros(2), 0.0, 0.0, kp_start
    t, xs, us = [0.0], [0.0], [0.0]
    for k in range(round(STOP / h)):
        if kp_switch and abs(k * h - KP_SWITCH_T) < h / 2:
            kp = KP_NEW
        error = R - state[0]
        integral += error * h
        u_next = kp * error + KI * integral
        force = u if scheme == "jacobi" else u_next
        state = ad @ state + bd * force
        u = u_next
        t.append((k + 1) * h)
        xs.append(float(state[0]))
        us.append(u)
    return {"t": t, "x": xs, "u": us}


def continuous_reference(times: list[float]) -> dict[str, Any]:
    """Continuous-time PI + MSD loop, z = [x, v, integral], solved exactly with expm."""
    import numpy as np

    a = np.array(
        [
            [0.0, 1.0, 0.0],
            [-(SPRING + KP) / MASS, -DAMPING / MASS, KI / MASS],
            [-1.0, 0.0, 0.0],
        ]
    )
    b = np.array([0.0, KP * R / MASS, R])
    steady = -np.linalg.solve(a, b)
    xs = [float((steady + expm(a * t) @ (np.zeros(3) - steady))[0]) for t in times]
    poles = np.roots([1.0, DAMPING / MASS, (SPRING + KP) / MASS, KI / MASS])
    return {"t": times, "x": xs, "poles": [str(np.round(p, 4)) for p in poles]}


def step_metrics(t: list[float], x: list[float]) -> dict[str, Any]:
    """Overshoot, peak time, 2 % settling time and final value of a step response."""
    peak = max(range(len(x)), key=lambda i: x[i])
    outside = [i for i, value in enumerate(x) if abs(value - R) > 0.02 * abs(R)]
    settled = None if not outside else (t[outside[-1] + 1] if outside[-1] + 1 < len(t) else None)
    return {
        "overshoot_pct": round(max(0.0, (x[peak] - R) / R * 100), 3),
        "peak_time": round(t[peak], 4),
        "settling_time_2pct": None if settled is None else round(settled, 4),
        "x_end": round(x[-1], 6),
        "max_abs_x": round(max(abs(v) for v in x), 6),
    }


def max_diff(a: dict[str, Any], b: dict[str, Any], key: str = "x") -> dict[str, Any]:
    """Max |a - b| of one signal over the time points both share (rounded to 1e-6 s)."""
    other = {round(t, 6): v for t, v in zip(b["t"], b[key], strict=True)}
    pairs = [
        (v, other[round(t, 6)]) for t, v in zip(a["t"], a[key], strict=True) if round(t, 6) in other
    ]
    if not pairs:
        return {"points": 0}
    worst = max(abs(p - q) for p, q in pairs)
    return {"points": len(pairs), f"max_abs_d{key}": float(f"{worst:.3e}")}


def sample(sig: dict[str, Any], every: float = 1.0) -> list[list[float]]:
    """Downsample t, x, u to one row per `every` seconds for the report."""
    keep = []
    for i, t in enumerate(sig["t"]):
        if abs(t / every - round(t / every)) < 1e-6:
            keep.append([round(t, 4), round(sig["x"][i], 6), round(sig["u"][i], 6)])
    return keep


# ----- probes -----------------------------------------------------------------------------------


def closed_loop(h: float) -> dict[str, Any]:
    """OMSimulator batch run at step h against FMPy masters and the exact references."""
    label = f"loop_h{h}"
    exports = [str(OUT / "ssp" / f"loop_h{h}_{n}.ssp") for n in ("a", "b")] if h == 0.01 else []
    oms = run_child("oms_batch", f"oms_{label}", base_cfg(h, f"oms_{label}", exports=exports))
    out: dict[str, Any] = {"oms": {k: v for k, v in oms.items() if k != "signals"}}
    if not oms.get("ok"):
        return out
    sig = oms["signals"]
    out["oms"]["result_rows"] = sig["rows"]
    out["oms"]["result_columns"] = sig["header"]
    out["oms"]["metrics"] = step_metrics(sig["t"], sig["x"])
    out["oms"]["sampled_t_x_u"] = sample(sig)
    out["oms"]["post_step_rows_F_equals_previous_u"] = max(
        abs(f - u) for f, u in zip(sig["F"][1:], sig["u"][:-1], strict=True)
    )
    variants = {
        "fmpy_jacobi_accumulated_grid": {"scheme": "jacobi", "grid": "accumulated"},
        "fmpy_jacobi_kh_grid": {"scheme": "jacobi"},
        "fmpy_gauss_seidel_kh_grid": {"scheme": "gauss_seidel"},
    }
    if h == 0.01:
        variants["fmpy_jacobi_kh_grid_tol1e-4"] = {"scheme": "jacobi", "tolerance": 1e-4}
    refs: dict[str, Any] = {}
    for name, extra in variants.items():
        master = run_child("fmpy_master", f"{name}_h{h}", base_cfg(h, name, **extra))
        if master.get("ok"):
            refs[name] = master["signals"] | {"wall_s": master["wall_s"]}
            out.setdefault("fmpy_final_tiny_step", {})[name] = master["final_tiny_step"]
        else:
            out[name] = master
    for scheme in ("jacobi", "gauss_seidel"):
        refs[f"exact_{scheme}"] = sampled_reference(h, scheme, kp_switch=False)
    refs["continuous"] = continuous_reference(sig["t"])
    out["references"] = {
        name: {
            "metrics": step_metrics(ref["t"], ref["x"]),
            "oms_vs_ref": max_diff(sig, ref),
            "oms_vs_ref_u": max_diff(sig, ref, "u") if "u" in ref else None,
            "wall_s": ref.get("wall_s"),
        }
        for name, ref in refs.items()
    }
    out["continuous_poles"] = refs["continuous"]["poles"]
    out["references"]["exact_jacobi"]["vs_continuous"] = max_diff(
        refs["exact_jacobi"], refs["continuous"]
    )
    return out


def stepping() -> dict[str, Any]:
    """Step at h = 0.01 with a Kp change at t = 1 and compare against FMPy and the baseline."""
    h = 0.01
    stepped = run_child("oms_stepped", "oms_stepped_h0.01", base_cfg(h, "oms_stepped_h0.01"))
    out: dict[str, Any] = {k: v for k, v in stepped.items() if k != "signals"}
    if not stepped.get("ok"):
        return out
    sig = stepped["signals"]
    cfg = base_cfg(h, "fmpy_kpswitch", scheme="jacobi", grid="accumulated", kp_switch=True)
    master = run_child("fmpy_master", "fmpy_jacobi_kpswitch_h0.01", cfg)
    base_sig = signals(read_csv(OUT / "results" / "oms_loop_h0.01.csv"))
    out["rows"] = sig["rows"]
    out["header"] = sig["header"]
    out["metrics"] = step_metrics(sig["t"], sig["x"])
    out["Kp_column_values"] = sorted(set(sig["Kp"])) if "Kp" in sig else None
    out["Kp_first_time_new_value"] = next(
        (t for t, kp in zip(sig["t"], sig.get("Kp") or [], strict=False) if kp == KP_NEW), None
    )
    out["vs_fmpy_jacobi_kpswitch"] = (
        max_diff(sig, master["signals"]) if master.get("ok") else master
    )
    out["vs_exact_jacobi_kpswitch"] = max_diff(sig, sampled_reference(h, "jacobi", kp_switch=True))
    before = {"t": [t for t in sig["t"] if t <= KP_SWITCH_T + 1e-9]}
    before["x"] = sig["x"][: len(before["t"])]
    out["vs_batch_before_switch"] = max_diff(before, base_sig)
    out["vs_batch_whole_run"] = max_diff(sig, base_sig)
    return out


def zip_listing(path: Path) -> list[dict[str, Any]]:
    """List the entries of a zip archive with sizes, CRCs and timestamps."""
    with zipfile.ZipFile(path) as archive:
        return [
            {
                "name": info.filename,
                "bytes": info.file_size,
                "compressed": info.compress_size,
                "crc": f"{info.CRC:08x}",
                "date_time": "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*info.date_time),
            }
            for info in archive.infolist()
        ]


def ssd_normalized(path: Path) -> str:
    """Return SystemStructure.ssd from an SSP with generationDateAndTime blanked out."""
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("SystemStructure.ssd").decode()
    return re.sub(r'generationDateAndTime="[^"]*"', 'generationDateAndTime=""', xml)


def compare_ssp(first: Path, second: Path) -> dict[str, Any]:
    """Byte, entry and normalized-SSD comparison of two exported SSP files."""
    left, right = zip_listing(first), zip_listing(second)
    crc_left = {e["name"]: e["crc"] for e in left}
    crc_right = {e["name"]: e["crc"] for e in right}
    return {
        "bytes_identical": first.read_bytes() == second.read_bytes(),
        "sha256": [sha256(first), sha256(second)],
        "entry_order_identical": [e["name"] for e in left] == [e["name"] for e in right],
        "entries_with_different_content": sorted(
            n for n in crc_left.keys() | crc_right.keys() if crc_left.get(n) != crc_right.get(n)
        ),
        "entries_with_different_timestamp": sorted(
            a["name"] for a, b in zip(left, right, strict=False) if a["date_time"] != b["date_time"]
        ),
        "ssd_identical_without_generationDateAndTime": ssd_normalized(first)
        == ssd_normalized(second),
    }


def ssp_roundtrip() -> dict[str, Any]:
    """Inspect the exported SSP, re-import and run it, and test export reproducibility."""
    a, b = OUT / "ssp" / "loop_h0.01_a.ssp", OUT / "ssp" / "loop_h0.01_b.ssp"
    fresh = OUT / "ssp" / "loop_h0.01_fresh.ssp"
    time.sleep(2.1)  # zip timestamps have 2 s resolution
    export_only = base_cfg(0.01, "export_only", exports=[str(fresh)], simulate=False)
    exported = run_child("oms_batch", "oms_export_fresh_process", export_only)
    rerun = run_child(
        "oms_batch", "oms_reimport_h0.01", base_cfg(0.01, "oms_reimport_h0.01", ssp_in=str(a))
    )
    out: dict[str, Any] = {
        "listing": zip_listing(a),
        "ssp_bytes": a.stat().st_size,
        "export_twice_same_object": compare_ssp(a, b),
        "export_in_fresh_process": compare_ssp(a, fresh) if fresh.exists() else exported,
    }
    with zipfile.ZipFile(a) as archive:
        ssd = archive.read("SystemStructure.ssd").decode()
    (OUT / "ssp" / "SystemStructure_a.ssd").write_text(ssd)
    out["ssd_has_solver_annotation"] = "oms:SimulationInformation" in ssd and "fixedStepSize" in ssd
    out["reimport"] = {k: v for k, v in rerun.items() if k not in ("signals", "api_calls")}
    if rerun.get("ok"):
        base_sig = signals(read_csv(OUT / "results" / "oms_loop_h0.01.csv"))
        sig = rerun["signals"]
        out["reimport"]["rows"] = sig["rows"]["raw"]
        out["reimport"]["vs_original_x"] = max_diff(sig, base_sig)
        out["reimport"]["vs_original_u"] = max_diff(sig, base_sig, "u")
        out["reimport"]["csv_bytes_identical"] = (
            OUT / "results" / "oms_reimport_h0.01.csv"
        ).read_bytes() == (OUT / "results" / "oms_loop_h0.01.csv").read_bytes()
    return out


def validate_ssv(path: Path) -> dict[str, Any]:
    """Validate an SSV file against the SSP 2.0 schema that OMSimulator bundles (lxml only)."""
    from lxml import etree

    spec = importlib.util.find_spec("OMSimulator")
    assert spec is not None and spec.origin is not None
    xsd = Path(spec.origin).parent / "schema" / "ssp" / "SystemStructureParameterValues.xsd"
    schema = etree.XMLSchema(file=str(xsd))
    valid = schema.validate(etree.parse(str(path)))
    return {"valid": valid, "errors": [e.message for e in schema.error_log][:3]}


SSV_CASES = {
    # case: (writer, attach to, parameter name, Kp, keep the inline r binding on PI)
    "api_root": ("api", ["default"], "PI.Kp", SSV_API_KP, True),
    "hand_root": ("hand", ["default"], "PI.Kp", SSV_FILE_KP, True),
    "hand_component": ("hand", ["default", "PI"], "Kp", SSV_FILE_KP, True),
    "hand_component_no_inline": ("hand", ["default", "PI"], "Kp", SSV_FILE_KP, False),
}


def ssv_probe(case: str) -> dict[str, Any]:
    """Apply Kp from an SSV (oms3 API or hand-written; root or PI), export, re-import, re-run."""
    writer, target, name, kp, bind_r = SSV_CASES[case]
    directory = OUT / "ssv"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"kp_{case}.ssv"
    if writer == "hand":
        path.write_text(HANDWRITTEN_SSV.format(kp=kp).replace('name="Kp"', f'name="{name}"'))
    ssv = {
        "mode": writer,
        "path": str(path),
        "kp": kp,
        "resource": f"resources/kp_{case}.ssv",
        "target": target,
        "name": name,
    }
    exported = OUT / "ssp" / f"loop_ssv_{case}.ssp"
    source = {"ssp_in": str(OUT / "ssp" / "loop_h0.01_a.ssp")} if bind_r else {"bind_r": False}
    cfg = base_cfg(0.01, f"oms_ssv_{case}", ssv=ssv, exports=[str(exported)], **source)
    applied = run_child("oms_batch", f"oms_ssv_{case}", cfg)
    re_cfg = base_cfg(0.01, f"oms_ssv_{case}_re", ssp_in=str(exported))
    again = run_child("oms_batch", f"oms_ssv_{case}_reimport", re_cfg)
    out: dict[str, Any] = {
        "ssv_file": str(path),
        "ssv_xml": path.read_text() if path.exists() else None,
        "schema": validate_ssv(path) if path.exists() else None,
        "applied": {k: v for k, v in applied.items() if k not in ("signals", "api_calls")},
        "reimported": {k: v for k, v in again.items() if k not in ("signals", "api_calls")},
    }
    if applied.get("ok"):
        out["applied"]["set_calls"] = [c for c in applied["api_calls"] if "Kp" in c]
        ref = sampled_reference(0.01, "jacobi", kp_switch=False, kp_start=kp)
        out["applied"]["vs_exact_jacobi_with_ssv_kp"] = max_diff(applied["signals"], ref)
        out["applied"]["metrics"] = step_metrics(applied["signals"]["t"], applied["signals"]["x"])
        if exported.exists():
            out["exported_listing"] = [e["name"] for e in zip_listing(exported)]
            with zipfile.ZipFile(exported) as archive:
                ssd = archive.read("SystemStructure.ssd").decode()
            out["exported_ssd_parameter_bindings"] = re.findall(r"<ssd:ParameterBinding[^>]*>", ssd)
    if again.get("ok") and applied.get("ok"):
        out["reimported"]["vs_applied"] = max_diff(again["signals"], applied["signals"])
    return out


def mat_v4(path: Path) -> dict[str, Any]:
    """Minimal MATLAB v4 reader (numpy only) for OpenModelica-style result files."""
    import numpy as np

    data, pos, matrices = path.read_bytes(), 0, {}
    kinds = {0: "<f8", 1: "<f4", 2: "<i4", 3: "<i2", 4: "<u2", 5: "u1"}
    while pos + 20 <= len(data):
        mopt, rows, cols, imag, namelen = struct.unpack_from("<5i", data, pos)
        pos += 20
        name = data[pos : pos + namelen - 1].decode()
        pos += namelen
        dtype = np.dtype(kinds[(mopt // 10) % 10])
        count = rows * cols * (2 if imag else 1)
        matrices[name] = np.frombuffer(data, dtype, count, pos).reshape((cols, rows)).T
        pos += count * dtype.itemsize
    return matrices


def mat_probe() -> dict[str, Any]:
    """Run the loop with a .mat result (h = 0.1) and read x, u back with the v4 reader."""
    import numpy as np

    target = OUT / "results" / "oms_loop_h0.1.mat"
    oms = run_child(
        "oms_batch", "oms_loop_h0.1_mat", base_cfg(0.1, "mat") | {"result": str(target)}
    )
    out: dict[str, Any] = {"ok": oms.get("ok"), "error": oms.get("error"), "bytes": None}
    if not target.exists():
        return out
    out["bytes"] = target.stat().st_size
    try:
        mats = mat_v4(target)
        out["matrices"] = {name: list(m.shape) for name, m in mats.items()}
        names = ["".join(chr(c) for c in col).strip("\x00 ") for col in mats["name"].T]
        info = mats["dataInfo"]
        data2 = mats["data_2"]

        def series(name: str) -> list[float]:
            i = names.index(name)
            row = abs(int(info[1, i])) - 1
            return [float(v) for v in np.sign(info[1, i]) * data2[row, :]]

        csv_raw = read_csv(OUT / "results" / "oms_loop_h0.1.csv")
        mat_t = series("time")
        out["aclass"] = ["".join(chr(c) for c in row).strip("\x00 ") for row in mats["Aclass"]]
        out["names"] = names
        out["parameters_in_data_1"] = [n for i, n in enumerate(names) if int(info[0, i]) == 1]
        out["rows"] = len(mat_t)
        out["last_times_repr"] = [repr(t) for t in mat_t[-5:]]
        for key, suffix in (("x", ".MSD.x"), ("u", ".PI.u")):
            mat_values = series(next(n for n in names if n.endswith(suffix)))
            csv_values = pick(csv_raw, suffix)
            out[f"vs_csv_row_by_row_max_abs_d{key}"] = float(
                f"{max(abs(a - b) for a, b in zip(mat_values, csv_values, strict=True)):.3e}"
            )
    except Exception as error:  # record and continue: this is a spike
        out["parse_error"] = f"{type(error).__name__}: {error}"
    return out


def fmi3_note() -> dict[str, Any]:
    """Re-check spike (c): FMI 3.0 Feedthrough needs Binary signals excluded from results."""
    out = {}
    for exclude in (False, True):
        label = f"fmi3_feedthrough_exclude_{exclude}"
        cfg = {"fmu": str(FEEDTHROUGH3), "exclude_binary": exclude}
        cfg["result"] = str(OUT / "results" / f"{label}.csv")
        result = run_child("fmi3_binary", label, cfg)
        log = (OUT / "logs" / f"{label}.stderr.txt").read_text().splitlines()
        distinct = sorted({line for line in log if line.startswith("error")})
        result["stderr_distinct_errors"] = distinct[:3]
        out[label] = result
    return out


# ----- provenance -----------------------------------------------------------------------------


def record_hash_hex(record: str, entry: str) -> str | None:
    """Convert a RECORD sha256 (urlsafe base64) for one entry to hex."""
    for line in record.splitlines():
        name, digest, _ = line.rsplit(",", 2)
        if name == entry and digest.startswith("sha256="):
            raw = base64.urlsafe_b64decode(digest[7:] + "=" * (-len(digest[7:]) % 4))
            return raw.hex()
    return None


def shell(cmd: str) -> list[str]:
    """Run a shell command and return its combined output lines."""
    done = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
    return (done.stdout + done.stderr).strip().splitlines()


def provenance() -> dict[str, Any]:
    """Locate libOMSimulator.so, hash it, and describe how the package and apt provide it."""
    spec = importlib.util.find_spec("OMSimulator")
    assert spec is not None and spec.origin is not None
    package = Path(spec.origin).parent
    lib = package / "libOMSimulator.so"
    dist = next(package.parent.glob("omsimulator-*.dist-info"))
    record = (dist / "RECORD").read_text()
    version = re.search(rb"OMSimulator v[0-9][^\x00]*", lib.read_bytes())
    bound = set(re.findall(r"self\.obj\.(oms_\w+)", (package / "capi.py").read_text()))
    exported = set(shell(f"nm -D --defined-only {lib} | awk '{{print $3}}' | grep '^oms_'"))
    cache = Path.home() / ".cache" / "uv"
    sdist_src = next(cache.glob("sdists-v*/pypi/omsimulator/*/*/src"), None)
    wheel = next(cache.glob("sdists-v*/pypi/omsimulator/*/*/*.whl"), None)
    hook = (sdist_src / "build_helpers.py").read_text() if sdist_src else ""
    files = [line.split(",")[0] for line in record.splitlines()]
    return {
        "package_dir": str(package),
        "lib": str(lib),
        "lib_bytes": lib.stat().st_size,
        "lib_sha256": sha256(lib),
        "record_sha256_hex": record_hash_hex(record, "OMSimulator/libOMSimulator.so"),
        "version_string": version.group().decode() if version else None,
        "wheel_tag": (dist / "WHEEL").read_text().strip().splitlines()[-1],
        "ldd": shell(f"ldd {lib}"),
        "capi_binds_symbols": len(bound),
        "capi_symbols_missing_from_lib": sorted(bound - exported),
        "lib_exports_oms_symbols": len(exported),
        "installed_files": {
            "OMSimulator_py": sum(
                f.startswith("OMSimulator/") and f.endswith(".py") for f in files
            ),
            "OMSimulator_schema_xsd": sum("/schema/" in f for f in files),
            "OMSimulatorGui_files": sum(f.startswith("OMSimulatorGui/") for f in files),
        },
        "other_lib_copies": {
            str(p): sha256(p)
            for pattern in (
                "archive-v0/*/OMSimulator/libOMSimulator.so",
                "sdists-v*/pypi/omsimulator/*/*/src/build/lib/OMSimulator/libOMSimulator.so",
            )
            for p in cache.glob(pattern)
        },
        "built_wheel": {"path": str(wheel), "sha256": sha256(wheel)} if wheel else None,
        "build_hook_urls": re.findall(
            r"https://build\.openmodelica\.org/\S+?\.(?:tar\.gz|zip)", hook
        ),
        "build_hook_verify_false": "verify=False" in hook,
        "build_hook_copies": re.findall(r'(?:dll_path|source_path)\s*=\s*"([^"]+)"', hook)[:2],
        "apt": {
            "policy": shell("apt-cache policy omsimulator libomsimulator 2>&1"),
            "show": shell("apt-cache show omsimulator 2>&1 | head -5"),
            "lists_entries": len(list(Path("/var/lib/apt/lists").glob("*"))),
            "sources": shell("cat /etc/apt/sources.list.d/openmodelica.list"),
            "installed_om_packages": shell(
                "dpkg -l | awk '/^ii/ && ($2 ~ /^(omc|omlib|libomc|omsim|libomsim)/) "
                '{print $2" "$3}\''
            ),
            "other_libOMSimulator_on_system": shell(
                "find / -xdev \\( -path /lab -o -path /proc -o -path '*/.cache' \\) -prune "
                "-o -name 'libOMSimulator*' -print 2>/dev/null"
            ),
        },
    }


def versions() -> dict[str, Any]:
    """Record tool versions without loading OMSimulator in this process."""
    from importlib.metadata import version

    return {
        "python": sys.version.split()[0],
        "OMSimulator": version("OMSimulator"),
        "pythonfmu": version("pythonfmu"),
        "fmpy": version("fmpy"),
        "numpy": version("numpy"),
        "omc": shell("omc --version")[:1],
    }


def main() -> int:
    """Run every probe, write result.json and print a summary."""
    for sub in ("tmp", "run", "children", "logs", "results", "ssp", "ssv", "inspect"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    for sub in ("results", "ssp", "fmus"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    inputs = [PI_SRC, MSD_SRC, FEEDTHROUGH3]
    before = {str(p): sha256(p) for p in inputs}
    report: dict[str, Any] = {"versions": versions()}
    report["build"] = {"pythonfmu": build_pi(), "omc": build_msd()}
    if not (PI_FMU.exists() and MSD_FMU.exists()):
        print(json.dumps(report, indent=2))
        return 1
    report["fmus"] = {"PIController": describe_fmu(PI_FMU), "MSD": describe_fmu(MSD_FMU)}
    fmus_before = {str(p): sha256(p) for p in (PI_FMU, MSD_FMU)}
    report["closed_loop"] = {f"h={h}": closed_loop(h) for h in STEPS}
    report["closed_loop_rtol"] = closed_loop_rtol()
    report["container_local_tmp"] = container_local_tmp()
    report["stepping"] = stepping()
    report["ssp"] = ssp_roundtrip()
    report["ssv"] = {case: ssv_probe(case) for case in SSV_CASES}
    report["mat_result"] = mat_probe()
    report["fmi3"] = fmi3_note()
    report["provenance"] = provenance()
    after = {str(p): sha256(p) for p in inputs}
    fmus_after = {str(p): sha256(p) for p in (PI_FMU, MSD_FMU)}
    report["inputs_unchanged"] = before == after and fmus_before == fmus_after
    report["input_sha256"] = after | fmus_after
    leaked = sorted(str(p.relative_to(OUT)) for p in (OUT / "tmp").glob("*/*"))
    report["leaked_tmp_entries"] = len(leaked)
    shutil.rmtree(OUT / "tmp", ignore_errors=True)
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


def closed_loop_rtol() -> dict[str, Any]:
    """OMSimulator at h = 0.01 with relativeTolerance 1e-6 against the default-tolerance run."""
    cfg = base_cfg(0.01, "oms_loop_h0.01_rtol1e-6", rtol=1e-6)
    oms = run_child("oms_batch", "oms_loop_h0.01_rtol1e-6", cfg)
    if not oms.get("ok"):
        return oms
    base_sig = signals(read_csv(OUT / "results" / "oms_loop_h0.01.csv"))
    return {
        "api_calls_with_tolerance": [c for c in oms["api_calls"] if "Tolerance" in c],
        "vs_default_tolerance_run": max_diff(oms["signals"], base_sig),
        "process": oms["process"],
    }


def container_local_tmp() -> dict[str, Any]:
    """Re-run the h = 0.1 loop with OMSimulator's temp dirs on a container-local path."""
    root = Path(tempfile.mkdtemp(prefix="afmi-spike-g-"))
    try:
        cfg = base_cfg(0.1, "oms_loop_h0.1_local_tmp")
        oms = run_child("oms_batch", "oms_loop_h0.1_local_tmp", cfg, tmp_root=root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    keys = ("ok", "error", "addSubModel_s", "instantiate_s", "simulate_s", "delete_s", "process")
    return {"tmp_root": str(root), "removed": not root.exists()} | {k: oms.get(k) for k in keys}


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        raise SystemExit(child_main(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
