"""Spike (b): step control of FMPy ``simulate_fmu`` through ``step_finished``.

Throwaway evidence for docs/spikes.md, not product code. Runs BouncingBall (FMI 2.0 and
3.0, Co-Simulation and Model Exchange) with a ``step_finished`` callback that publishes
its state to ``state.json`` and polls an inbox directory for JSON commands. The commands
come from a separate writer process (this file started with ``--writer``) which reacts to
the published simulation time: pause for 2 s at t~0.5, set ``e`` 0.7 -> 0.5 at t~1, stop
at t~2.5. The simulation is paced to wall-clock time so the writer can react. Each
command's latency (file written -> applied) is recorded. Two extra probes record what a
naive ``e`` change does in Model Exchange and how FMPy's ``timeout`` treats a pause.

Collaborators: FMPy (in-process), Reference FMUs v0.0.41 under ``.cache/reference-fmus``.
All outputs go to ``.cache/spikes/b``; one extra run keeps its IPC files in a temporary
directory on the container-local filesystem to compare against the bind mount.

Run inside the dev container::

    uv run python spikes/b_step_control.py
"""

import contextlib
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REF = REPO / ".cache" / "reference-fmus" / "0.0.41"
OUT = REPO / ".cache" / "spikes" / "b"
E_VR = 6
E_NEW = 0.5
PAUSE_AT, PAUSE_S, SET_AT, STOP_AT = 0.5, 2.0, 1.0, 2.5
POLL_S = 0.005
RUNS = [
    ("2.0", "CoSimulation"),
    ("2.0", "ModelExchange"),
    ("3.0", "CoSimulation"),
    ("3.0", "ModelExchange"),
]


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON next to path and rename it into place so readers never see half a file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


# --- writer process -------------------------------------------------------------------


def wait_for(state_path: Path, condition: Any, limit_s: float = 30.0) -> dict[str, Any]:
    """Poll the published state until condition(state) holds."""
    deadline = time.time() + limit_s
    while time.time() < deadline:
        with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
            state = json.loads(state_path.read_text())
            if condition(state):
                return state
        time.sleep(POLL_S)
    raise TimeoutError(f"writer: condition not met within {limit_s} s")


def send(inbox: Path, seq: int, command: dict[str, Any]) -> None:
    """Drop one command file into the inbox, stamped with its wall-clock write time."""
    command = {**command, "seq": seq, "written_at": time.time()}
    write_json_atomic(inbox / f"{seq:03d}-{command['cmd']}.json", command)


def writer_main(run_dir: Path) -> int:
    """Issue pause/resume, set e and stop in reaction to the published simulation time."""
    state, inbox = run_dir / "state.json", run_dir / "inbox"
    wait_for(state, lambda s: s["t"] >= PAUSE_AT)
    send(inbox, 1, {"cmd": "pause"})
    wait_for(state, lambda s: s["paused"])
    time.sleep(PAUSE_S + 0.2)
    send(inbox, 2, {"cmd": "resume"})
    wait_for(state, lambda s: s["t"] >= SET_AT and not s["paused"])
    send(inbox, 3, {"cmd": "set", "name": "e", "value": E_NEW})
    wait_for(state, lambda s: s["t"] >= STOP_AT)
    send(inbox, 4, {"cmd": "stop"})
    return 0


# --- simulation side --------------------------------------------------------------------


def set_e(fmu: Any, version: str, kind: str, value: float) -> float:
    """Set e on the live instance and read it back; ME needs Event Mode around the set."""
    is_me = kind == "ModelExchange"
    if is_me:
        fmu.enterEventMode()
    if version == "2.0":
        fmu.setReal([E_VR], [value])
        readback = fmu.getReal([E_VR])[0]
    else:
        fmu.setFloat64([E_VR], [value])
        readback = fmu.getFloat64([E_VR])[0]
    if is_me:
        update = fmu.newDiscreteStates if version == "2.0" else fmu.updateDiscreteStates
        while update()[0]:  # first field: newDiscreteStatesNeeded / discreteStatesNeedUpdate
            pass
        fmu.enterContinuousTimeMode()
    return float(readback)


class StepController:
    """step_finished callback: publishes state, applies inbox commands, paces to wall clock."""

    def __init__(self, run_dir: Path, version: str, kind: str) -> None:
        self.state_path, self.inbox = run_dir / "state.json", run_dir / "inbox"
        self.version, self.kind = version, kind
        self.started = time.time()
        self.paused_total = 0.0
        self.calls = 0
        self.busy_s = 0.0
        self.fmu_class = ""
        self.applied: list[dict[str, Any]] = []
        self.pause_samples: list[tuple[float, float]] = []

    def publish(self, sim_time: float, paused: bool) -> None:
        """Write the current simulation time and a wall-clock heartbeat."""
        write_json_atomic(
            self.state_path, {"t": sim_time, "paused": paused, "heartbeat": time.time()}
        )

    def poll(self) -> list[dict[str, Any]]:
        """Read and remove every pending command file, oldest first."""
        commands = []
        for path in sorted(self.inbox.glob("*.json")):
            commands.append(json.loads(path.read_text()))
            path.unlink()
        return commands

    def record(self, command: dict[str, Any], sim_time: float, **extra: Any) -> None:
        """Store an applied command with its latency."""
        latency_ms = (time.time() - command["written_at"]) * 1000
        self.applied.append(
            {"cmd": command["cmd"], "t": sim_time, "latency_ms": round(latency_ms, 2), **extra}
        )

    def pause(self, sim_time: float) -> None:
        """Hold the simulation, keep the heartbeat going, return on a resume command."""
        began = time.time()
        while True:
            self.publish(sim_time, paused=True)
            self.pause_samples.append((time.time(), sim_time))
            resume = [c for c in self.poll() if c["cmd"] == "resume"]
            if resume:
                self.record(resume[0], sim_time)
                break
            time.sleep(POLL_S)
        self.paused_total += time.time() - began

    def apply(self, command: dict[str, Any], sim_time: float, fmu: Any) -> bool:
        """Apply one command; return False when the simulation must stop."""
        if command["cmd"] == "pause":
            self.record(command, sim_time)
            self.pause(sim_time)
        elif command["cmd"] == "set":
            readback = set_e(fmu, self.version, self.kind, command["value"])
            self.record(command, sim_time, readback=readback)
        elif command["cmd"] == "stop":
            self.record(command, sim_time)
            return False
        return True

    def __call__(self, sim_time: float, recorder: Any) -> bool:
        """FMPy hook, called after every output step and event."""
        entered, paused_before = time.perf_counter(), self.paused_total
        self.calls += 1
        self.fmu_class = type(recorder.fmu).__name__
        self.publish(sim_time, paused=False)
        for command in self.poll():
            if not self.apply(command, sim_time, recorder.fmu):
                return False
        self.busy_s += time.perf_counter() - entered - (self.paused_total - paused_before)
        lag = sim_time + self.paused_total - (time.time() - self.started)
        if lag > 0:
            time.sleep(lag)
        return True


def apexes(result: Any) -> list[tuple[float, float]]:
    """Return (time, h) of interior local maxima of h, i.e. the bounce apexes."""
    t, h = result["time"], result["h"]
    return [
        (round(float(t[i]), 4), round(float(h[i]), 4))
        for i in range(1, len(h) - 1)
        if h[i] > h[i - 1] and h[i] >= h[i + 1]
    ]


def e_declaration(fmu_path: Path) -> dict[str, Any]:
    """Return e's causality and variability from the modelDescription."""
    from fmpy import read_model_description

    md = read_model_description(str(fmu_path))
    e = next(v for v in md.modelVariables if v.name == "e")
    return {"causality": e.causality, "variability": e.variability, "start": e.start}


def controlled_run(version: str, kind: str, ipc_root: Path) -> dict[str, Any]:
    """Run one BouncingBall variant under writer-process control, IPC files under ipc_root."""
    from fmpy import simulate_fmu

    fmu_path = REF / version / "BouncingBall.fmu"
    run_dir = ipc_root / f"fmi{version.replace('.', '')}_{kind}"
    (run_dir / "inbox").mkdir(parents=True, exist_ok=True)
    for stale in [*run_dir.glob("inbox/*"), run_dir / "state.json"]:
        stale.unlink(missing_ok=True)
    controller = StepController(run_dir, version, kind)
    calls: deque[str] = deque(maxlen=4)
    controller.publish(0.0, paused=False)
    writer = subprocess.Popen([sys.executable, __file__, "--writer", str(run_dir)])
    started = time.time()
    result = simulate_fmu(
        str(fmu_path),
        fmi_type=kind,
        output=["h", "v"],
        step_finished=controller,
        fmi_call_logger=calls.append,
    )
    wall = time.time() - started
    writer_rc = writer.wait(timeout=30)
    t_set = next(a["t"] for a in controller.applied if a["cmd"] == "set")
    peaks = apexes(result)
    frozen = {t for _, t in controller.pause_samples}
    heartbeat_span = controller.pause_samples[-1][0] - controller.pause_samples[0][0]
    return {
        "e_declaration": e_declaration(fmu_path),
        "recorder_fmu_class": controller.fmu_class,
        "wall_s": round(wall, 3),
        "ipc_root": str(ipc_root),
        "callback_calls": controller.calls,
        "callback_overhead_ms_mean": round(controller.busy_s / controller.calls * 1000, 3),
        "result_rows": len(result),
        "last_time": round(float(result["time"][-1]), 4),
        "writer_returncode": writer_rc,
        "applied": controller.applied,
        "pause": {
            "sim_time_values_during_pause": sorted(frozen),
            "heartbeats": len(controller.pause_samples),
            "heartbeat_span_s": round(heartbeat_span, 3),
        },
        "apexes_before_set": [p for p in peaks if p[0] < t_set],
        "apexes_after_set": [p for p in peaks if p[0] >= t_set],
        "last_fmi_calls": list(calls),
    }


def baseline_apexes(version: str, kind: str) -> list[tuple[float, float]]:
    """Apexes of an uncontrolled run (e stays 0.7) for comparison."""
    from fmpy import simulate_fmu

    result = simulate_fmu(str(REF / version / "BouncingBall.fmu"), fmi_type=kind, output=["h"])
    return apexes(result)[:4]


def naive_me_set(version: str) -> dict[str, Any]:
    """Set e from step_finished in ME without entering Event Mode and record what happens."""
    from fmpy import simulate_fmu

    def callback(sim_time: float, recorder: Any) -> bool:
        if sim_time >= SET_AT and "tried" not in outcome:
            outcome["tried"] = sim_time
            setter = recorder.fmu.setReal if version == "2.0" else recorder.fmu.setFloat64
            try:
                setter([E_VR], [E_NEW])
            except Exception as error:  # record the FMU's verdict
                outcome["set_error"] = f"{type(error).__name__}: {error}"
        return True

    outcome: dict[str, Any] = {}
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        try:
            simulate_fmu(
                str(REF / version / "BouncingBall.fmu"),
                fmi_type="ModelExchange",
                step_finished=callback,
            )
            outcome["simulation"] = "completed"
        except Exception as error:
            outcome["simulation"] = f"{type(error).__name__}: {error}"
    outcome["fmu_log"] = [line for line in printed.getvalue().splitlines() if line.strip()]
    return outcome


def timeout_vs_pause() -> dict[str, Any]:
    """Pause 2 s inside step_finished with simulate_fmu(timeout=1.0) and see what FMPy does."""
    from fmpy import simulate_fmu

    def callback(sim_time: float, recorder: Any) -> bool:
        if sim_time >= PAUSE_AT and "paused_at" not in seen:
            seen["paused_at"] = sim_time
            time.sleep(PAUSE_S)
        return True

    seen: dict[str, Any] = {}
    started = time.time()
    try:
        result = simulate_fmu(
            str(REF / "2.0" / "BouncingBall.fmu"), timeout=1.0, step_finished=callback
        )
        seen["outcome"] = (
            f"returned normally, last time {float(result['time'][-1]):.3f} of stop 3.0"
        )
    except Exception as error:
        seen["outcome"] = f"{type(error).__name__}: {error}"
    seen["wall_s"] = round(time.time() - started, 3)
    return seen


def main() -> int:
    """Run all probes, write result.json and print it."""
    import fmpy

    OUT.mkdir(parents=True, exist_ok=True)
    inputs = [REF / v / "BouncingBall.fmu" for v in ("2.0", "3.0")]
    before = {str(p): sha256(p) for p in inputs}
    report: dict[str, Any] = {
        "versions": {
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "fmpy": fmpy.__version__,
        },
        "runs": {},
    }
    for version, kind in RUNS:
        label = f"fmi{version.replace('.', '')}_{kind}"
        run = controlled_run(version, kind, OUT)
        run["baseline_apexes_e07"] = baseline_apexes(version, kind)
        report["runs"][label] = run
    with tempfile.TemporaryDirectory(prefix="afmi-spike-b-") as local:  # container-local FS
        report["runs"]["fmi30_CoSimulation_local_ipc"] = controlled_run(
            "3.0", "CoSimulation", Path(local)
        )
    report["naive_me_set"] = {v: naive_me_set(v) for v in ("2.0", "3.0")}
    report["timeout_vs_pause"] = timeout_vs_pause()
    after = {str(p): sha256(p) for p in inputs}
    report["input_sha256"] = {"before": before, "after": after, "unchanged": before == after}
    (OUT / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--writer":
        raise SystemExit(writer_main(Path(sys.argv[2])))
    raise SystemExit(main())
