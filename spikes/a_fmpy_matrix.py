"""Spike (a): FMPy simulate_fmu matrix over the Reference FMUs.

Throwaway evidence for docs/spikes.md, not product code. Runs every Reference FMU
(v0.0.41, FMI 2.0 and 3.0) with ``fmpy.simulate_fmu`` under its default experiment, once
per interface it declares (CoSimulation, ModelExchange). FMUs that declare neither (FMI 3.0
ScheduledExecution only) get one run with FMPy's automatic interface choice so the error is
recorded. Each run happens in a child process with a hard timeout so a crash or hang in
native code is recorded rather than fatal. A last probe reruns a hanging case with FMPy's own
``timeout`` argument to see whether FMPy can interrupt it. FMI 1.0 is out of scope by
project decision.

Collaborators: FMPy (in the child processes), Reference FMUs v0.0.41 under
``.cache/reference-fmus``. Outputs go to ``.cache/spikes/a``.

Run inside the dev container::

    uv run python spikes/a_fmpy_matrix.py
"""

import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REF = REPO / ".cache" / "reference-fmus" / "0.0.41"
OUT = REPO / ".cache" / "spikes" / "a"
VERSIONS = ("2.0", "3.0")
HARD_TIMEOUT_S = 120
PROBE_HARD_TIMEOUT_S = 60
PROBE_FMPY_TIMEOUT_S = 5.0


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def interfaces(fmu: Path) -> list[str]:
    """Return the interface types the modelDescription declares."""
    from fmpy import read_model_description

    md = read_model_description(str(fmu))
    declared = {
        "CoSimulation": md.coSimulation,
        "ModelExchange": md.modelExchange,
        "ScheduledExecution": getattr(md, "scheduledExecution", None),
    }
    return [name for name, value in declared.items() if value is not None]


def child_main(fmu: str, fmi_type: str, fmpy_timeout: float | None) -> int:
    """Simulate one FMU/interface pair and print a JSON line with the outcome."""
    from fmpy import simulate_fmu

    started = time.perf_counter()
    try:
        result = simulate_fmu(
            fmu, fmi_type=None if fmi_type == "auto" else fmi_type, timeout=fmpy_timeout
        )
        outcome = {
            "ok": True,
            "rows": len(result),
            "columns": list(result.dtype.names),
            "t_end": float(result["time"][-1]),
        }
    except Exception as error:  # the matrix records failures verbatim
        outcome = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    outcome["sim_wall_s"] = round(time.perf_counter() - started, 4)
    print(json.dumps(outcome))
    return 0


def run_one(
    fmu: Path,
    fmi_type: str,
    hard_timeout: float = HARD_TIMEOUT_S,
    fmpy_timeout: float | None = None,
) -> dict[str, Any]:
    """Run child_main in a fresh interpreter and parse its last stdout line."""
    started = time.perf_counter()
    try:
        child = subprocess.run(
            [sys.executable, __file__, "--child", str(fmu), fmi_type, str(fmpy_timeout)],
            capture_output=True,
            text=True,
            timeout=hard_timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"hard timeout after {hard_timeout} s (child killed)"}
    process_wall = round(time.perf_counter() - started, 3)
    lines = [line for line in child.stdout.splitlines() if line.startswith("{")]
    if child.returncode != 0 or not lines:
        tail = child.stderr.strip().splitlines()[-3:]
        return {"ok": False, "error": f"child exit {child.returncode}", "stderr_tail": tail}
    outcome = json.loads(lines[-1])
    outcome["process_wall_s"] = process_wall
    stderr = [line for line in child.stderr.splitlines() if line.strip()]
    if stderr:
        outcome["stderr_lines"] = len(stderr)
        outcome["stderr_head"] = stderr[:2]
    return outcome


def matrix() -> list[dict[str, Any]]:
    """Build the full FMU x interface matrix."""
    rows = []
    for version in VERSIONS:
        for fmu in sorted((REF / version).glob("*.fmu")):
            declared = interfaces(fmu)
            runnable = [i for i in declared if i in ("CoSimulation", "ModelExchange")] or ["auto"]
            for fmi_type in runnable:
                row = {"fmi": version, "fmu": fmu.stem, "declared": declared, "fmi_type": fmi_type}
                row.update(run_one(fmu, fmi_type))
                print(json.dumps({k: row[k] for k in row if k != "columns"}), flush=True)
                rows.append(row)
    return rows


def fmpy_timeout_probe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rerun every hard-timeout case with simulate_fmu(timeout=PROBE_FMPY_TIMEOUT_S)."""
    probes = []
    for row in rows:
        if "hard timeout" in row.get("error", ""):
            fmu = REF / row["fmi"] / f"{row['fmu']}.fmu"
            outcome = run_one(fmu, row["fmi_type"], PROBE_HARD_TIMEOUT_S, PROBE_FMPY_TIMEOUT_S)
            probes.append(
                {"fmi": row["fmi"], "fmu": row["fmu"], "fmi_type": row["fmi_type"], **outcome}
            )
    return probes


def main() -> int:
    """Run the matrix, check input hashes, write result.json."""
    import fmpy

    OUT.mkdir(parents=True, exist_ok=True)
    inputs = sorted(p for v in VERSIONS for p in (REF / v).glob("*.fmu"))
    before = {str(p): sha256(p) for p in inputs}
    rows = matrix()
    probes = fmpy_timeout_probe(rows)
    after = {str(p): sha256(p) for p in inputs}
    report = {
        "versions": {
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "fmpy": fmpy.__version__,
        },
        "fmi_1_0": "out of scope by project decision",
        "rows": rows,
        "passed": sum(r["ok"] for r in rows),
        "total": len(rows),
        "fmpy_timeout_probe": {
            "fmpy_timeout_s": PROBE_FMPY_TIMEOUT_S,
            "hard_timeout_s": PROBE_HARD_TIMEOUT_S,
            "runs": probes,
        },
        "input_sha256": {"before": before, "after": after, "unchanged": before == after},
    }
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("passed", "total", "fmpy_timeout_probe")}))
    print("inputs unchanged:", report["input_sha256"]["unchanged"])
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        timeout_arg = None if sys.argv[4] == "None" else float(sys.argv[4])
        raise SystemExit(child_main(sys.argv[2], sys.argv[3], timeout_arg))
    raise SystemExit(main())
