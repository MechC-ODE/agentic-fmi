"""Spike (h): the OMSimulator facts the system engine relies on (its design probe).

Throwaway evidence for docs/spikes.md, not product code. It builds the shipped PI example and
an exact zero-order-hold plant with pythonfmu into ``.cache/spikes/h/fmus`` and runs, each in a
fresh child Python process the way a worker would (``--child NAME CFG``; the parent never
imports OMSimulator):
- the closed loop built through the API, with the result file left on, switched off with
  ``setResultFile("")``, with ``removeSignalsFromResults(".*")`` only, and both; ``setValue``
  before ``initialize()``; ``doStep()`` plus ``getValue()`` (post-exchange values) against the
  exact Jacobi recurrence; the time grid; a ``doStep()`` at the stop time;
- ``setValue`` on an output with ``initial="exact"``;
- two FMI 3.0 Feedthrough instances: setters and getters per type, step time;
- hand-written SSD/SSV files (versions 1.0 and 2.0) against the bundled XSDs, written twice
  for byte reproducibility, re-imported with ``SSP(path)``;
- the package-tree digest that ``oms_pin.package_digest`` pins;
- added for the engine: a non-zero start time, a step a component discards (``do_step``
  returns ``False``) and a slave that raises in ``do_step``.

Collaborators: pythonfmu (build CLI), OMSimulator (children only), lxml (XSD checks), the
Reference FMUs 0.0.41 (FMI 3.0 Feedthrough). Outputs go to ``.cache/spikes/h`` and the
container-local ``/tmp/afmi-h`` (instantiate is 20 times faster there than on the bind mount).

Run inside the dev container::

    uv run --no-sync python spikes/h_oms_engine_probe.py
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = REPO / ".cache" / "spikes" / "h"
TMP = Path("/tmp/afmi-h")  # container-local: instantiate is 20x faster than on the bind mount
FMUS = OUT / "fmus"
PI_SRC = REPO / "skills" / "fmu-authoring" / "examples" / "pi_controller.py"
FT3 = REPO / ".cache" / "reference-fmus" / "0.0.41" / "3.0" / "Feedthrough.fmu"
H, STOP = 0.01, 1.0
KP, KI, R = 10.0, 15.0, 1.0
K, TAU = 2.0, 0.5

PLANT_PY = '''"""First-order plant with exact zero-order-hold discretization (pythonfmu CS slave).

x' = (K*F - x)/tau, so over a step h with F held: x <- a*x + K*(1-a)*F, a = exp(-h/tau).
"""

import math

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Fmi2Variability, Real

TUNABLE = {"causality": Fmi2Causality.parameter, "variability": Fmi2Variability.tunable}
OUTPUT = {"causality": Fmi2Causality.output, "initial": Fmi2Initial.exact}


class Plant(Fmi2Slave):
    author = "probe"
    description = "first-order lag, exact ZOH"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.K, self.tau = 2.0, 0.5
        self.F, self.x = 0.0, 0.0
        self.register_variable(Real("K", **TUNABLE))
        self.register_variable(Real("tau", **TUNABLE))
        self.register_variable(Real("F", causality=Fmi2Causality.input))
        self.register_variable(Real("x", **OUTPUT))

    def do_step(self, current_time, step_size):
        a = math.exp(-step_size / self.tau)
        self.x = a * self.x + self.K * (1.0 - a) * self.F
        return True
'''

STEPPER_PY = '''"""y = t until t = 0.5; then do_step returns False (MODE "discard") or raises ("raise")."""

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Real

MODE = "{mode}"


class {cls}(Fmi2Slave):
    author = "probe"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.y = 0.0
        self.register_variable(Real("y", causality=Fmi2Causality.output, initial=Fmi2Initial.exact))

    def do_step(self, current_time, step_size):
        if current_time + step_size > 0.5 + 1e-9:
            if MODE == "raise":
                raise RuntimeError("probe slave refuses to step past t = 0.5")
            return False
        self.y = current_time + step_size
        return True
'''  # noqa: E501


# ------------------------------------------------------------------------------------------
# children
# ------------------------------------------------------------------------------------------


def snapshot(*dirs: Path) -> set[str]:
    files: set[str] = set()
    for d in dirs:
        if d.exists():
            files |= {str(p) for p in d.rglob("*")}
    return files


def build_loop(cfg: dict) -> object:
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=cfg["ssp_tmp"])
    model.addResource(cfg["pi"], new_name="resources/PI.fmu")
    model.addResource(cfg["plant"], new_name="resources/P.fmu")
    model.addComponent(CRef("default", "PI"), "resources/PI.fmu")
    model.addComponent(CRef("default", "P"), "resources/P.fmu")
    model.addConnection(CRef("default", "PI", "u"), CRef("default", "P", "F"))
    model.addConnection(CRef("default", "P", "x"), CRef("default", "PI", "y"))
    model.newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": cfg["h"]})
    model.setSolver(CRef("default", "PI"), "ma")
    model.setSolver(CRef("default", "P"), "ma")
    model.activeVariant.stopTime = cfg["stop"]
    return model


def step_and_read(
    inst: object, names: list[tuple[str, str]], stop: float, h: float, sysname: str = "default"
) -> dict:
    from OMSimulator import CRef

    crefs = [CRef(sysname, c, v) for c, v in names]
    rows = [[inst.getTime(), *[inst.getValue(c) for c in crefs]]]
    walls = []
    while inst.getTime() < stop - h / 2:
        t0 = time.perf_counter()
        inst.doStep()
        walls.append(time.perf_counter() - t0)
        rows.append([inst.getTime(), *[inst.getValue(c) for c in crefs]])
    walls.sort()
    return {
        "rows": rows,
        "doStep_ms_median": round(walls[len(walls) // 2] * 1000, 4) if walls else None,
        "doStep_ms_max": round(walls[-1] * 1000, 4) if walls else None,
    }


def child_loop(cfg: dict) -> dict:
    """Build via the API, switch the result file off (variant), step with doStep + getValue."""
    import warnings

    os.chdir(cfg["cwd"])
    before = snapshot(Path(cfg["cwd"]), TMP)
    import OMSimulator
    from OMSimulator import CRef

    report: dict = {
        "version": OMSimulator.version,
        "tempfile_dir": __import__("tempfile").gettempdir(),
    }
    model = (
        build_loop(cfg)
        if not cfg.get("ssp_in")
        else OMSimulator.SSP(cfg["ssp_in"], temp_dir=cfg["ssp_tmp"])
    )
    if cfg.get("ssp_in") is None and cfg.get("export"):
        model.export(cfg["export"])
    inst = model.instantiate()
    variant = cfg["variant"]
    sysname = cfg.get("sysname", "default")
    if variant == "empty":
        inst.setResultFile("")
    elif variant == "remove_all":
        inst.removeSignalsFromResults(".*")
    elif variant == "both":
        inst.setResultFile("")
        inst.removeSignalsFromResults(".*")
    # parameters after instantiate, before initialize
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if not cfg.get("no_set"):
            report["set_Kp_status"] = str(
                inst.setValue(CRef(sysname, "PI", "Kp"), cfg.get("kp", KP))
            )
            report["set_r_status"] = str(inst.setValue(CRef(sysname, "PI", "r"), R))
        if cfg.get("set_u"):
            report["set_output_u_status"] = str(inst.setValue(CRef(sysname, "PI", "u"), 5.0))
    report["set_warnings"] = [str(w.message)[:200] for w in caught]
    inst.initialize()
    report["Kp_after_init"] = inst.getValue(CRef(sysname, "PI", "Kp"))
    names = [("PI", "u"), ("PI", "y"), ("P", "x"), ("P", "F")]
    report |= step_and_read(inst, names, cfg["stop"], cfg["h"], sysname)
    report["end_time"] = repr(inst.getTime())
    try:
        inst.doStep()
        report["doStep_after_stop"] = repr(inst.getTime())
    except Exception as error:  # record
        report["doStep_after_stop"] = f"{type(error).__name__}: {error}"
    inst.terminate()
    inst.delete()
    after = snapshot(Path(cfg["cwd"]), TMP)
    report["new_files"] = sorted(p for p in after - before if not p.startswith(cfg["ssp_tmp"]))[:40]
    report["result_like_files"] = sorted(
        p for p in after if p.endswith((".mat", ".csv")) and "fmus" not in p
    )[:20]
    mods = {
        n: getattr(m, "__file__", None)
        for n, m in sys.modules.items()
        if n.split(".")[0] in ("pythonfmu", "pi_controller", "plant")
    }
    report["python_modules"] = mods
    return report


def child_fmi3(cfg: dict) -> dict:
    """Two FMI 3.0 Feedthrough instances, result file off: getters per type and step time."""
    os.chdir(cfg["cwd"])
    from OMSimulator import SSP, Capi, CRef

    model = SSP(temp_dir=cfg["ssp_tmp"])
    model.addResource(cfg["fmu"], new_name="resources/FT.fmu")
    model.addComponent(CRef("default", "A"), "resources/FT.fmu")
    model.addComponent(CRef("default", "B"), "resources/FT.fmu")
    for out, inp in (
        ("Float64_continuous_output", "Float64_continuous_input"),
        ("Int32_output", "Int32_input"),
        ("Boolean_output", "Boolean_input"),
    ):
        model.addConnection(CRef("default", "A", out), CRef("default", "B", inp))
    model.newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": 0.01})
    model.setSolver(CRef("default", "A"), "ma")
    model.setSolver(CRef("default", "B"), "ma")
    model.activeVariant.stopTime = 1.0
    inst = model.instantiate()
    inst.setResultFile("")
    report: dict = {}
    for var, value in (
        ("Float64_continuous_input", 3.5),
        ("Int32_input", 7),
        ("Boolean_input", True),
    ):
        try:
            report[f"set_A.{var}"] = str(inst.setValue(CRef("default", "A", var), value))
        except Exception as error:
            report[f"set_A.{var}"] = f"{type(error).__name__}: {error}"
    inst.initialize()
    t0 = time.perf_counter()
    n = 0
    while inst.getTime() < 1.0 - 0.005:
        inst.doStep()
        n += 1
    report["doStep_ms_mean"] = round((time.perf_counter() - t0) / max(n, 1) * 1000, 4)
    for var in (
        "Float64_continuous_output",
        "Int32_output",
        "Boolean_output",
        "Float32_continuous_output",
        "UInt64_output",
        "Binary_output",
        "String_output",
    ):
        path = f"model.root.B.{var}"
        vtype, status = Capi.getVariableType(path)
        try:
            value = inst.getValue(CRef("default", "B", var))
            report[f"get_B.{var}"] = {"type": vtype, "value": repr(value)}
        except Exception as error:
            report[f"get_B.{var}"] = {
                "type": vtype,
                "status": str(status),
                "error": f"{type(error).__name__}: {error}"[:200],
            }
    real, st = Capi.getReal("model.root.B.Float64_continuous_output")
    report["capi_getReal_B.Float64"] = [real, str(st)]
    inst.terminate()
    inst.delete()
    return report


def child_start(cfg: dict) -> dict:
    """The plant alone with startTime 0.5: where does the clock start?"""
    os.chdir(cfg["cwd"])
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=cfg["ssp_tmp"])
    model.addResource(cfg["plant"], new_name="resources/P.fmu")
    model.addComponent(CRef("default", "P"), "resources/P.fmu")
    model.newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": 0.1})
    model.setSolver(CRef("default", "P"), "ma")
    model.activeVariant.startTime = 0.5
    model.activeVariant.stopTime = 1.0
    inst = model.instantiate()
    inst.setResultFile("")
    report = {"getStartTime": inst.getStartTime(), "time_after_instantiate": inst.getTime()}
    inst.setStartTime(0.5)
    inst.initialize()
    report["time_after_initialize"] = inst.getTime()
    inst.doStep()
    report["time_after_doStep"] = inst.getTime()
    inst.terminate()
    inst.delete()
    return report


def child_stepper(cfg: dict) -> dict:
    """A component that discards (or raises in) its steps past t = 0.5."""
    os.chdir(cfg["cwd"])
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=cfg["ssp_tmp"])
    model.addResource(cfg["fmu"], new_name="resources/S.fmu")
    model.addComponent(CRef("default", "S"), "resources/S.fmu")
    model.newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": 0.1})
    model.setSolver(CRef("default", "S"), "ma")
    model.activeVariant.stopTime = 1.0
    inst = model.instantiate()
    inst.setResultFile("")
    inst.initialize()
    times = []
    try:
        for _ in range(12):
            inst.doStep()
            times.append(inst.getTime())
            print("t", times[-1], flush=True)  # the last line before an abort
        error = None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    inst.terminate()
    inst.delete()
    return {"times": times, "error": error}


CHILDREN = {"loop": child_loop, "fmi3": child_fmi3, "start": child_start, "step": child_stepper}


# ------------------------------------------------------------------------------------------
# parent helpers
# ------------------------------------------------------------------------------------------


def run_child(name: str, cfg: dict) -> dict:
    cfg_path = OUT / f"{name}_{cfg.get('tag', '')}.json"
    cfg_path.write_text(json.dumps(cfg))
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    env["TMPDIR"] = str(TMP / f"proc-{name}-{cfg.get('tag', '')}")
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-I", "-B", __file__, "--child", name, str(cfg_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    (OUT / f"{name}_{cfg.get('tag', '')}.stdout").write_text(proc.stdout)
    (OUT / f"{name}_{cfg.get('tag', '')}.stderr").write_text(proc.stderr)
    tail = proc.stdout.strip().splitlines()
    try:
        report = json.loads(next(line for line in reversed(tail) if line.startswith("{")))
    except (StopIteration, json.JSONDecodeError):
        report = {
            "error": "no report",
            "stderr": proc.stderr[-1500:],
            "stdout": proc.stdout[-1500:],
        }
    report["exit"] = proc.returncode
    report["wall_s"] = round(time.perf_counter() - started, 3)
    report["stderr_lines"] = len(proc.stderr.splitlines())
    report["stdout_lines"] = len(proc.stdout.splitlines()) - 1
    report["leftover_tmp"] = sorted(str(p.relative_to(TMP)) for p in Path(env["TMPDIR"]).iterdir())[
        :10
    ]
    return report


def build_fmu(src: Path, text: str | None = None) -> Path:
    work = OUT / "src"
    work.mkdir(parents=True, exist_ok=True)
    copy = work / src.name
    if text is None:
        shutil.copy2(src, copy)
    else:
        copy.write_text(text)
    FMUS.mkdir(parents=True, exist_ok=True)
    before = set(FMUS.glob("*.fmu"))
    command = [sys.executable, "-I", "-B", "-m", "pythonfmu", "build", "-f", str(copy)]
    subprocess.run([*command, "-d", str(FMUS)], check=True, capture_output=True)
    (built,) = set(FMUS.glob("*.fmu")) - before
    return built


def recurrence(n: int, h: float, kp: float) -> dict[str, list[float]]:
    """Jacobi: both step from point k with inputs exchanged at k; then outputs -> inputs."""
    a = math.exp(-h / TAU)
    u, x, integral = 0.0, 0.0, 0.0
    us, xs = [u], [x]
    for _ in range(n):
        y, F = x, u  # post-exchange inputs at point k
        error = R - y
        integral += error * h
        u = kp * error + KI * integral
        x = a * x + K * (1.0 - a) * F
        us.append(u)
        xs.append(x)
    return {"u": us, "x": xs}


def tree_digest(pkg: Path) -> str:
    """The pin algorithm the design specifies (oms_pin.package_digest)."""
    digest = hashlib.sha256()
    entries = []
    for root, dirs, files in os.walk(pkg, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in files:
            path = Path(root) / name
            entries.append(path.relative_to(pkg).as_posix())
    for rel in sorted(entries):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256((pkg / rel).read_bytes()).digest())
    return digest.hexdigest()


# --- a deterministic SSP writer prototype (what ssp.py will do) ----------------------------

NS = {
    "ssd": "http://ssp-standard.org/SSP1/SystemStructureDescription",
    "ssc": "http://ssp-standard.org/SSP1/SystemStructureCommon",
    "ssv": "http://ssp-standard.org/SSP1/SystemStructureParameterValues",
    "oms": "https://raw.githubusercontent.com/OpenModelica/OMSimulator/master/schema/oms.xsd",
}


def ssd_text(version: str, h: float, stop: float) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ssd:SystemStructureDescription xmlns:ssc="{NS["ssc"]}" xmlns:ssd="{NS["ssd"]}" xmlns:oms="{NS["oms"]}" name="loop" version="{version}" generationTool="Agentic FMI probe">
  <ssd:System name="loop">
    <ssd:ParameterBindings>
      <ssd:ParameterBinding source="resources/parameters.ssv"/>
    </ssd:ParameterBindings>
    <ssd:Elements>
      <ssd:Component name="P" type="application/x-fmu-sharedlibrary" source="resources/P.fmu" implementation="CoSimulation">
        <ssd:Connectors>
          <ssd:Connector name="F" kind="input"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="x" kind="output"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="K" kind="parameter"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="tau" kind="parameter"><ssc:Real/></ssd:Connector>
        </ssd:Connectors>
        <ssd:Annotations><ssc:Annotation type="org.openmodelica"><oms:Annotations><oms:SimulationInformation name="ma"/></oms:Annotations></ssc:Annotation><ssc:Annotation type="org.agentic-fmi"><afmi:Resource xmlns:afmi="urn:agentic-fmi" sha256="00"/></ssc:Annotation></ssd:Annotations>
      </ssd:Component>
      <ssd:Component name="PI" type="application/x-fmu-sharedlibrary" source="resources/PI.fmu" implementation="CoSimulation">
        <ssd:Connectors>
          <ssd:Connector name="Kp" kind="parameter"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="Ki" kind="parameter"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="r" kind="parameter"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="u" kind="output"><ssc:Real/></ssd:Connector>
          <ssd:Connector name="y" kind="input"><ssc:Real/></ssd:Connector>
        </ssd:Connectors>
        <ssd:Annotations><ssc:Annotation type="org.openmodelica"><oms:Annotations><oms:SimulationInformation name="ma"/></oms:Annotations></ssc:Annotation></ssd:Annotations>
      </ssd:Component>
    </ssd:Elements>
    <ssd:Connections>
      <ssd:Connection startElement="P" startConnector="x" endElement="PI" endConnector="y"/>
      <ssd:Connection startElement="PI" startConnector="u" endElement="P" endConnector="F"/>
    </ssd:Connections>
    <ssd:Annotations><ssc:Annotation type="org.openmodelica"><oms:Annotations><oms:SimulationInformation name="ma" method="oms_ma" fixedStepSize="{h!r}"/></oms:Annotations></ssc:Annotation></ssd:Annotations>
  </ssd:System>
  <ssd:DefaultExperiment startTime="0.0" stopTime="{stop!r}"/>
</ssd:SystemStructureDescription>
"""  # noqa: E501


def ssv_text(version: str, values: dict[str, float]) -> str:
    params = "\n".join(
        f'    <ssv:Parameter name="{k}"><ssv:Real value="{v!r}"/></ssv:Parameter>'
        for k, v in sorted(values.items())
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ssv:ParameterSet xmlns:ssc="{NS["ssc"]}" xmlns:ssv="{NS["ssv"]}" version="{version}" name="parameters">
  <ssv:Parameters>
{params}
  </ssv:Parameters>
</ssv:ParameterSet>
"""  # noqa: E501


def write_ssp(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            stored = name.endswith(".fmu")
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            zf.writestr(info, members[name])


def xsd_check(xml: bytes, xsd: Path) -> str:
    from lxml import etree

    try:
        schema = etree.XMLSchema(etree.parse(str(xsd)))
    except etree.XMLSchemaParseError as error:
        return f"xsd unusable: {error}"
    doc = etree.fromstring(xml)
    return "valid" if schema.validate(doc) else "; ".join(str(e) for e in schema.error_log)[:600]


def loop_metrics(rows: list[list[float]], ref: dict[str, list[float]]) -> dict:
    u = [row[1] for row in rows]
    y = [row[2] for row in rows]
    x = [row[3] for row in rows]
    f = [row[4] for row in rows]
    return {
        "n_rows": len(rows),
        "first_rows": rows[:3],
        "last_row": rows[-1],
        "post_exchange_y_eq_x": max(abs(a - b) for a, b in zip(y, x, strict=True)),
        "post_exchange_F_eq_u": max(abs(a - b) for a, b in zip(f, u, strict=True)),
        "max_abs_dx_vs_recurrence": max(abs(a - b) for a, b in zip(x, ref["x"], strict=True)),
        "max_abs_du_vs_recurrence": max(abs(a - b) for a, b in zip(u, ref["u"], strict=True)),
        "time_grid_max_dev_from_kh": max(abs(row[0] - k * H) for k, row in enumerate(rows)),
    }


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)
    OUT.mkdir(parents=True)
    TMP.mkdir(parents=True)
    pi = build_fmu(PI_SRC)
    plant = build_fmu(Path("plant.py"), PLANT_PY)
    steppers = {
        mode: build_fmu(
            Path(f"{mode}_slave.py"),
            STEPPER_PY.replace("{mode}", mode).replace("{cls}", mode.capitalize()),
        )
        for mode in ("discard", "raise")
    }
    report: dict = {"fmus": {"pi": str(pi), "plant": str(plant)}}
    cwd = OUT / "cwd"
    cwd.mkdir()
    base = {"pi": str(pi), "plant": str(plant), "h": H, "stop": STOP, "cwd": str(cwd)}
    n = round(STOP / H)
    ref = recurrence(n, H, KP)
    for variant in ("empty", "remove_all", "both", "default"):
        cfg = base | {"variant": variant, "tag": variant, "ssp_tmp": str(TMP / f"ssp-{variant}")}
        if variant == "empty":
            cfg["export"] = str(OUT / "oms_export.ssp")
        r = run_child("loop", cfg)
        rows = r.pop("rows", None)
        if rows:
            r |= loop_metrics(rows, ref)
        report[f"loop_{variant}"] = r
        # cwd cleanup between variants so leftovers are attributable
        for p in cwd.iterdir():
            p.unlink() if p.is_file() else shutil.rmtree(p)
    setu = base | {
        "variant": "empty",
        "tag": "setu",
        "set_u": True,
        "ssp_tmp": str(TMP / "ssp-setu"),
    }
    report["set_output_u"] = {
        k: v
        for k, v in run_child("loop", setu).items()
        if k in ("set_output_u_status", "set_warnings", "first_rows")
    }
    # FMI 3.0 getters
    report["fmi3"] = run_child(
        "fmi3", {"fmu": str(FT3), "cwd": str(cwd), "tag": "ft", "ssp_tmp": str(TMP / "ssp-ft")}
    )
    # engine facts: start time, a discarded step, a raising slave
    report["start_time"] = run_child(
        "start", base | {"tag": "start", "ssp_tmp": str(TMP / "ssp-start")}
    )
    for mode, fmu in steppers.items():
        cfg = {"fmu": str(fmu), "cwd": str(cwd), "tag": mode, "ssp_tmp": str(TMP / f"ssp-{mode}")}
        child = run_child("step", cfg)
        child["stdout_tail"] = (OUT / f"step_{mode}.stdout").read_text().splitlines()[-3:]
        child["stderr_tail"] = (OUT / f"step_{mode}.stderr").read_text().splitlines()[-2:]
        report[f"step_{mode}"] = child
    # hand-written SSPs (our writer prototype), version 1.0 and 2.0, Kp=7 via root SSV
    pkg = Path(importlib.util.find_spec("OMSimulator").submodule_search_locations[0])
    xsd_dir = pkg / "schema" / "ssp"
    ref7 = recurrence(n, H, 7.0)
    for version in ("1.0", "2.0"):
        ssd = ssd_text(version, H, STOP).encode()
        ssv = ssv_text(version, {"PI.Kp": 7.0, "PI.Ki": KI, "PI.r": R}).encode()
        members = {
            "SystemStructure.ssd": ssd,
            "resources/PI.fmu": pi.read_bytes(),
            "resources/P.fmu": plant.read_bytes(),
            "resources/parameters.ssv": ssv,
        }
        a, b = OUT / f"ours_{version}_a.ssp", OUT / f"ours_{version}_b.ssp"
        write_ssp(a, members)
        write_ssp(b, members)
        entry: dict = {
            "byte_identical_repeat": a.read_bytes() == b.read_bytes(),
            "ssd_xsd": xsd_check(ssd, xsd_dir / "SystemStructureDescription.xsd"),
            "ssd_xsd11": xsd_check(ssd, xsd_dir / "SystemStructureDescription11.xsd"),
            "ssv_xsd": xsd_check(ssv, xsd_dir / "SystemStructureParameterValues.xsd"),
        }
        cfg = base | {
            "variant": "empty",
            "tag": f"ours{version}",
            "ssp_in": str(a),
            "ssp_tmp": str(TMP / f"ssp-ours{version}"),
            "kp": 7.0,
            "no_set": True,
            "sysname": "loop",
        }
        r = run_child("loop", cfg)
        rows = r.pop("rows", None)
        if rows:
            x = [row[3] for row in rows]
            entry["n_rows"] = len(rows)
            entry["max_abs_dx_vs_recurrence_kp7"] = max(
                abs(p - q) for p, q in zip(x, ref7["x"], strict=True)
            )
        keys = (
            "exit",
            "Kp_after_init",
            "set_warnings",
            "error",
            "stderr",
            "end_time",
            "result_like_files",
            "new_files",
        )
        entry["child"] = {k: r.get(k) for k in keys}
        report[f"ours_ssp_{version}"] = entry
    # pin digest
    report["pin"] = {
        "package_dir": str(pkg),
        "version_metadata": importlib.metadata.version("OMSimulator"),
        "tree_sha256": tree_digest(pkg),
        "lib_sha256": hashlib.sha256((pkg / "libOMSimulator.so").read_bytes()).hexdigest(),
        "omsimulator_imported_in_parent": "OMSimulator" in sys.modules,
    }
    t0 = time.perf_counter()
    tree_digest(pkg)
    report["pin"]["digest_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=1, default=str)[:20000])
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        result = CHILDREN[sys.argv[2]](json.loads(Path(sys.argv[3]).read_text()))
        sys.stdout.flush()
        print("\n" + json.dumps(result, default=str))
    else:
        main()
