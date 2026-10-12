"""Spike (e): build a Python FMU with pythonfmu and run it in FMPy, OMSimulator and C.

Throwaway evidence for docs/spikes.md, not product code. Writes a tiny ``Fmi2Slave``
(Gain: y = k * u, k a tunable parameter) under ``.cache/spikes/e/src``, builds it with
``uv run --no-sync pythonfmu build``, then runs it in child processes:

1. FMPy ``simulate_fmu`` with an input signal and k set as a start value;
2. FMPy again with the site-packages copy of ``pythonfmu`` hidden, to see whether the copy
   that pythonfmu bundles into the FMU's resources is enough;
3. OMSimulator, Reference Feedthrough (FMI 2.0) -> Gain, via an SSP;
4. a minimal C host compiled with gcc that dlopens the FMU binary, instantiates it and does
   one step, without and with LD_PRELOAD / PYTHONHOME / PYTHONPATH.

It also records the uv Python's shared-library configuration.

Collaborators: pythonfmu (build CLI), FMPy and OMSimulator (in child processes), gcc,
Reference FMUs v0.0.41 under ``.cache/reference-fmus``. Outputs go to ``.cache/spikes/e``.

Run inside the dev container::

    uv run python spikes/e_pythonfmu.py
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
import sysconfig
import time
import zipfile
from importlib.machinery import PathFinder
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REF = REPO / ".cache" / "reference-fmus" / "0.0.41"
OUT = REPO / ".cache" / "spikes" / "e"
FEEDTHROUGH = REF / "2.0" / "Feedthrough.fmu"
K = 3.0
U = 2.0

GAIN_SOURCE = '''\
"""Gain FMU for spike (e): y = k * u with k a tunable parameter."""

from pythonfmu import DefaultExperiment, Fmi2Causality, Fmi2Slave, Fmi2Variability, Real


class Gain(Fmi2Slave):
    author = "Agentic FMI spike (e)"
    description = "y = k * u"
    default_experiment = DefaultExperiment(start_time=0.0, stop_time=1.0, step_size=0.1)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k = 2.0
        self.u = 0.0
        self.y = 0.0
        self.register_variable(
            Real("k", causality=Fmi2Causality.parameter, variability=Fmi2Variability.tunable)
        )
        self.register_variable(Real("u", causality=Fmi2Causality.input))
        self.register_variable(Real("y", causality=Fmi2Causality.output))

    def do_step(self, current_time, step_size):
        self.y = self.k * self.u
        return True
'''

NATIVE_HOST_C = r"""
#include <dlfcn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct { void *logger, *allocate, *release, *stepFinished, *env; } callbacks_t;
typedef const char *(*version_fn)(void);
typedef void *(*instantiate_fn)(const char *, int, const char *, const char *,
                                const void *, int, int);
typedef int (*setup_fn)(void *, int, double, double, int, double);
typedef int (*instance_fn)(void *);
typedef int (*real_fn)(void *, const unsigned int *, size_t, double *);
typedef int (*step_fn)(void *, double, double, int);

static void logger(void *env, const char *name, int status, const char *cat, const char *msg, ...) {
    va_list ap;
    va_start(ap, msg);
    printf("fmu log: ");
    vprintf(msg, ap);
    printf("\n");
    va_end(ap);
}

int main(int argc, char **argv) {
    void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!lib) { printf("dlopen failed: %s\n", dlerror()); return 2; }
    printf("fmi2GetVersion: %s\n", ((version_fn)dlsym(lib, "fmi2GetVersion"))());
    callbacks_t cb = {(void *)logger, (void *)calloc, (void *)free, NULL, NULL};
    fflush(stdout);
    instantiate_fn instantiate = (instantiate_fn)dlsym(lib, "fmi2Instantiate");
    void *c = instantiate("gain", 1, argv[2], argv[3], &cb, 0, 1);
    printf("fmi2Instantiate: %s\n", c ? "instance created" : "NULL");
    if (!c) return 3;
    unsigned int vk = atoi(argv[4]), vu = atoi(argv[5]), vy = atoi(argv[6]);
    double k = 3.0, u = 2.0, y = -1.0;
    ((setup_fn)dlsym(lib, "fmi2SetupExperiment"))(c, 0, 0.0, 0.0, 0, 0.0);
    ((instance_fn)dlsym(lib, "fmi2EnterInitializationMode"))(c);
    ((real_fn)dlsym(lib, "fmi2SetReal"))(c, &vk, 1, &k);
    ((instance_fn)dlsym(lib, "fmi2ExitInitializationMode"))(c);
    ((real_fn)dlsym(lib, "fmi2SetReal"))(c, &vu, 1, &u);
    int status = ((step_fn)dlsym(lib, "fmi2DoStep"))(c, 0.0, 0.1, 1);
    ((real_fn)dlsym(lib, "fmi2GetReal"))(c, &vy, 1, &y);
    printf("fmi2DoStep status %d, y after one step with k=3, u=2: %g\n", status, y);
    ((instance_fn)dlsym(lib, "fmi2Terminate"))(c);
    return 0;
}
"""


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd: list[str], env: dict[str, str] | None = None, timeout: float = 120) -> dict[str, Any]:
    """Run a command and keep its exit code and the tails of its output."""
    started = time.perf_counter()
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    return {
        "returncode": done.returncode,
        "wall_s": round(time.perf_counter() - started, 3),
        "stdout_tail": [line for line in done.stdout.splitlines() if line.strip()][-6:],
        "stderr_tail": [line for line in done.stderr.splitlines() if line.strip()][-6:],
    }


def python_shared_library() -> dict[str, Any]:
    """Describe how the running uv Python provides libpython."""
    exe = Path(sys.executable).resolve()
    ldd = subprocess.run(["ldd", str(exe)], capture_output=True, text=True).stdout
    exported = subprocess.run(
        ["nm", "-D", "--defined-only", str(exe)], capture_output=True, text=True
    ).stdout
    libdir = Path(sysconfig.get_config_var("LIBDIR"))
    return {
        "executable": str(exe),
        "Py_ENABLE_SHARED": sysconfig.get_config_var("Py_ENABLE_SHARED"),
        "LDLIBRARY": sysconfig.get_config_var("LDLIBRARY"),
        "libpython": str(libdir / sysconfig.get_config_var("INSTSONAME")),
        "executable_links_libpython": "libpython" in ldd,
        "executable_exports_Py_symbols": sum(" T Py" in line for line in exported.splitlines()),
    }


def build() -> tuple[Path, dict[str, Any]]:
    """Write the slave script and build Gain.fmu with the pythonfmu CLI."""
    src = OUT / "src"
    src.mkdir(parents=True, exist_ok=True)
    script = src / "gain.py"
    script.write_text(GAIN_SOURCE)
    fmu = OUT / "Gain.fmu"
    fmu.unlink(missing_ok=True)
    outcome = run(
        ["uv", "run", "--no-sync", "pythonfmu", "build", "-f", str(script), "-d", str(OUT)]
    )
    names = zipfile.ZipFile(fmu).namelist() if fmu.exists() else []
    outcome |= {"fmu": str(fmu), "exists": fmu.exists(), "archive": sorted(names)}
    return fmu, outcome


class SitePackagesBlocker:
    """Meta path finder that resolves ``pythonfmu`` only from non-site-packages sys.path entries."""

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        """Find pythonfmu (and submodules) while ignoring site-packages."""
        if name != "pythonfmu":
            return None
        allowed = [entry for entry in sys.path if "site-packages" not in entry]
        spec = PathFinder.find_spec(name, allowed)
        if spec is None:
            raise ImportError("pythonfmu hidden from site-packages and not found elsewhere")
        return spec


def fmpy_child(fmu: str, hide_site_pythonfmu: bool) -> int:
    """Child: simulate Gain with FMPy, u = U then 2U, k = K; print a JSON summary."""
    if hide_site_pythonfmu:
        sys.meta_path.insert(0, SitePackagesBlocker())
    import numpy as np
    from fmpy import simulate_fmu

    signal = np.array(
        [(0.0, U), (0.5, U), (0.5, 2 * U), (1.0, 2 * U)], dtype=[("time", float), ("u", float)]
    )
    try:
        result = simulate_fmu(fmu, input=signal, start_values={"k": K}, output=["u", "y"])
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 0
    rows = [(round(float(r["time"]), 2), float(r["u"]), float(r["y"])) for r in result]
    used = getattr(sys.modules.get("pythonfmu"), "__file__", None)
    print(json.dumps({"ok": True, "rows": len(rows), "pythonfmu_used": used, "t_u_y": rows}))
    return 0


def oms_child(fmu: str, out_dir: str) -> int:
    """Child: Feedthrough.Float64_continuous_output -> Gain.u in OMSimulator; print JSON."""
    import tempfile

    tempfile.tempdir = out_dir
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=out_dir)
    model.addResource(str(FEEDTHROUGH), new_name="resources/Feedthrough.fmu")
    model.addResource(fmu, new_name="resources/Gain.fmu")
    model.addComponent(CRef("default", "ft"), "resources/Feedthrough.fmu")
    model.addComponent(CRef("default", "gain"), "resources/Gain.fmu")
    model.addConnection(
        CRef("default", "ft", "Float64_continuous_output"), CRef("default", "gain", "u")
    )
    inst = model.instantiate()
    inst.setResultFile(str(Path(out_dir) / "oms_gain.csv"))
    inst.setValue(CRef("default", "ft", "Float64_continuous_input"), U)
    inst.setValue(CRef("default", "gain", "k"), K)
    inst.initialize()
    inst.stepUntil(0.5)
    mid = inst.getValue(CRef("default", "gain", "y"))
    inst.setValue(CRef("default", "gain", "k"), 2 * K)
    inst.stepUntil(1.0)
    end = inst.getValue(CRef("default", "gain", "y"))
    inst.terminate()
    inst.delete()
    print(json.dumps({"ok": True, "y_at_0.5": mid, "y_at_1.0_after_k_doubled": end}))
    return 0


def native_host(fmu: Path, libpython: str) -> dict[str, Any]:
    """Compile the C host, extract Gain.fmu and try three environments."""
    host_dir = OUT / "native"
    extracted = host_dir / "Gain"
    extracted.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(fmu).extractall(extracted)
    source, binary = host_dir / "host.c", host_dir / "host"
    source.write_text(NATIVE_HOST_C)
    compiled = run(["gcc", "-o", str(binary), str(source), "-ldl"])
    if compiled["returncode"] != 0:
        return {"compile": compiled}
    guid = next(
        line.split('"')[1]
        for line in (extracted / "modelDescription.xml").read_text().split()
        if line.startswith("guid=")
    )
    from fmpy import read_model_description

    vrs = {v.name: str(v.valueReference) for v in read_model_description(str(fmu)).modelVariables}
    library = str(extracted / "binaries" / "linux64" / "Gain.so")
    args = [
        str(binary),
        library,
        guid,
        (extracted / "resources").as_uri(),
        vrs["k"],
        vrs["u"],
        vrs["y"],
    ]
    base = {
        k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH", "LD_PRELOAD")
    }
    site = sysconfig.get_paths()["purelib"]
    variants = {
        "plain": base,
        "LD_PRELOAD": base | {"LD_PRELOAD": libpython},
        "LD_PRELOAD+PYTHONHOME": base | {"LD_PRELOAD": libpython, "PYTHONHOME": sys.base_prefix},
        "LD_PRELOAD+PYTHONHOME+PYTHONPATH": base
        | {"LD_PRELOAD": libpython, "PYTHONHOME": sys.base_prefix, "PYTHONPATH": site},
    }
    return {name: run(args, env=env, timeout=60) for name, env in variants.items()}


def child_json(outcome: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON line a child printed, keeping the raw tails when there is none."""
    lines = [line for line in outcome["stdout_tail"] if line.startswith("{")]
    return {"returncode": outcome["returncode"], **json.loads(lines[-1])} if lines else outcome


def main() -> int:
    """Build the FMU, run every host, check input hashes, write result.json."""
    import fmpy
    import OMSimulator
    import pythonfmu

    OUT.mkdir(parents=True, exist_ok=True)
    before = sha256(FEEDTHROUGH)
    shared = python_shared_library()
    fmu, built = build()
    report: dict[str, Any] = {
        "versions": {
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "pythonfmu": pythonfmu.__version__,
            "fmpy": fmpy.__version__,
            "OMSimulator": OMSimulator.__version__,
        },
        "python_shared_library": shared,
        "build": built,
    }
    if fmu.exists():
        me = [sys.executable, __file__]
        oms_tmp = OUT / "oms-tmp"
        oms_tmp.mkdir(exist_ok=True)
        report["fmpy_host"] = child_json(run([*me, "--fmpy", str(fmu)]))
        report["fmpy_host_site_pythonfmu_hidden"] = child_json(
            run([*me, "--fmpy-hidden", str(fmu)])
        )
        report["oms_host"] = child_json(run([*me, "--oms", str(fmu), str(oms_tmp)]))
        report["native_host"] = native_host(fmu, shared["libpython"])
    after = sha256(FEEDTHROUGH)
    report["input_sha256"] = {
        str(FEEDTHROUGH): {"before": before, "after": after},
        "unchanged": before == after,
    }
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    flag = sys.argv[1] if len(sys.argv) > 1 else ""
    if flag in ("--fmpy", "--fmpy-hidden"):
        raise SystemExit(fmpy_child(sys.argv[2], hide_site_pythonfmu=flag == "--fmpy-hidden"))
    if flag == "--oms":
        raise SystemExit(oms_child(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
