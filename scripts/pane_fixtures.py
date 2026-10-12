"""Generate the pane's fixtures, ``pane/fixtures/store.ts``, from the run-store protocol models.

The pane (``pane/``, ADR 0005 and ADR 0010) reads only the run store, and ``claude plugin test``
runs its tests without a filesystem, so the tests serve a fake store from this module's output:
every document is built with the pydantic models of ``agentic_fmi.store.protocol`` and dumped
the way the workers and the server write them. A model change that the pane doesn't follow
then fails the pane's tests once the fixtures are regenerated, and
``tests/store/test_pane_fixtures.py`` fails until they are (it compares this module's output
with the committed file byte for byte, and checks the worst-case ``live.json`` size and the
golden pane commands).

What it writes (a TypeScript module, because a plugin may import only script files):
- ``FILES``: absolute path -> file text of a store at ``/ws/.agentic-fmi``: a paused live run of
  one FMU (``r1``, with an applied and a rejected command outcome), a running live system run
  (``r2``, two components in a loop), a finished batch run (``r3``), a finished build job with
  ``build.json`` (``r4``) and a failed run (``r5``).
- ``VARIANTS``: documents a newer or broken writer could leave (``protocol: 2``, an unknown
  state, a torn ``live.json``), for the "shown generically, previous data kept" tests.
- ``WORST_LIVE``: the largest ``live.json`` the limits allow (``Limits.live_points`` points of
  ``Limits.live_variables`` series, ``Limits.live_max_columns`` columns with long names).
- ``DISCOVERY``: ``AFMI_STORE_DIR`` cases with the store directory ``config.load_config``
  resolves for a session root of ``/ws``, so the pane's discovery can be tested against it.
- ``PANE_COMMANDS``: per button, the command file the pane must write at ``NOW_MS``
  (``Command`` models, ``source: "pane"``), with JavaScript's ISO time format. ``chart`` is what
  the pane writes for r1 (charts h and v) when ✕ is pressed on v: ``names: ["h"]``.

Usage: ``uv run python scripts/pane_fixtures.py`` (writes the file); ``--check`` exits 1 if the
committed file differs. Deterministic: fixed times, versions and values.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentic_fmi.analysis.summary import round_sig
from agentic_fmi.config import Limits, Timing, load_config
from agentic_fmi.store.protocol import (
    BuildEvent,
    BuildRecord,
    BuildSpec,
    BuildSummary,
    BuildVersions,
    BuiltFmu,
    Command,
    CommandOutcome,
    ComponentSpec,
    ErrorCode,
    Experiment,
    FileRef,
    FmuRef,
    Index,
    IndexEntry,
    LiveColumn,
    LiveSnapshot,
    MasterAlgorithm,
    OmsVersion,
    ResolvedValue,
    RunError,
    RunSpec,
    RunState,
    RunStatus,
    SourceRef,
    SystemConnection,
    SystemSpec,
    Versions,
)

ROOT = "/ws"
STORE = f"{ROOT}/.agentic-fmi"
HOME = "/home/dev"
OUTPUT = Path(__file__).resolve().parents[1] / "pane" / "fixtures" / "store.ts"
NOW_MS = 1_760_000_000_000
"""The mocked clock of the pane tests (``mock.clock(on, { now: NOW_MS })``)."""
T0 = datetime.fromtimestamp(NOW_MS / 1000, UTC) - timedelta(minutes=5)
SHA = "0123456789abcdef" * 4
VERSIONS = Versions(
    agentic_fmi="0.1.0", fmpy="0.3.32", numpy="2.3.4", python="3.12.12", platform="x86_64-linux"
)
TIMING = Timing()
LIMITS = Limits()
DIGITS = LIMITS.significant_digits


def _text(model: BaseModel) -> str:
    """A document as the store holds it: compact JSON plus a newline (``atomic.write_json``)."""
    data = type(model).model_validate(model.model_dump()).model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _round(value: float) -> float | None:
    return round_sig(value, DIGITS)


def _fmu(name: str) -> FmuRef:
    return FmuRef(
        path=f"{ROOT}/{name}.fmu", sha256=SHA, size_bytes=65_536, fmi_version="2.0", model_name=name
    )


def _ball(t: float) -> tuple[float, float]:
    """A bouncing ball's height and speed (restitution 0.7), for chart-shaped data."""
    g, e, v0 = 9.81, 0.7, 4.0
    while True:
        flight = 2 * v0 / g
        if t < flight or v0 < 1e-3:
            return v0 * t - g * t * t / 2, v0 - g * t
        t -= flight
        v0 *= e


def _live(run_id: str, names: list[tuple[str, str | None]], t_end: float, h: float) -> LiveSnapshot:
    """A live.json at ``t_end`` with ``LIMITS.live_points``-capped history on a step ``h``."""
    count = min(LIMITS.live_points, round(t_end / h) + 1)
    times = [_round(t_end - (count - 1 - i) * h) or 0.0 for i in range(count)]

    def value(name: str, t: float) -> float:
        height, speed = _ball(t)
        return {"h": height, "v": speed, "plant.x": 1 - math.exp(-t) * math.cos(3 * t)}.get(
            name, 10 * math.exp(-t) * math.sin(3 * t)
        )

    charted = [name for name, _ in names][: LIMITS.live_variables]
    return LiveSnapshot(
        run_id=run_id,
        seq=42,
        t=t_end,
        values=[LiveColumn(name=n, unit=u, value=_round(value(n, t_end))) for n, u in names],
        time=times,
        series={name: [_round(value(name, t)) for t in times] for name in charted},
    )


def _status(run_id: str, state: RunState, t: float, t_stop: float, **fields: Any) -> RunStatus:
    terminal = state in (RunState.FINISHED, RunState.FAILED, RunState.STOPPED)
    return RunStatus(
        run_id=run_id,
        state=state,
        t=t,
        t_start=0.0,
        t_stop=t_stop,
        progress=min(1.0, t / t_stop) if t_stop else 0.0,
        updated_at=_at(60),
        started_at=_at(1),
        finished_at=_at(60) if terminal else None,
        heartbeat_at=_at(60),
        heartbeat_mono=None if terminal else 12_345.5,
        pid=None if terminal else 4242,
        **fields,
    )


def store_files() -> dict[str, str]:
    """Path -> text of every file of the fixture store."""
    files: dict[str, str] = {}

    def put(relative: str, model: BaseModel) -> None:
        files[f"{STORE}/{relative}"] = _text(model)

    experiment = Experiment(start_time=0.0, stop_time=3.0, output_interval=0.01)
    ball = RunSpec(
        run_id="r1",
        created_at=_at(0),
        fmu=_fmu("BouncingBall"),
        fmi_type="CoSimulation",
        experiment=experiment,
        start_values={"e": 0.7},
        outputs=["h", "v"],
        timeout_s=TIMING.default_timeout_s,
        versions=VERSIONS,
        live=True,
        idle_limit_s=TIMING.live_idle_limit_s,
    )
    put("runs/r1/spec.json", ball)
    pause_id, bad_set_id = f"{NOW_MS - 9000}-pane-0a1b2c3d", f"{NOW_MS - 4000}-pane-4e5f6a7b"
    put(
        "runs/r1/status.json",
        _status("r1", RunState.PAUSED, 1.25, 3.0, speed=1.0, last_cmd=bad_set_id),
    )
    put("runs/r1/live.json", _live("r1", [("h", "m"), ("v", "m/s")], 1.25, 0.01))
    put(
        f"runs/r1/outcomes/{pause_id}.json",
        CommandOutcome(
            run_id="r1",
            id=pause_id,
            source="pane",
            action="pause",
            status="applied",
            t=1.25,
            at=_at(51),
        ),
    )
    put(
        f"runs/r1/outcomes/{bad_set_id}.json",
        CommandOutcome(
            run_id="r1",
            id=bad_set_id,
            source="pane",
            action="set",
            status="rejected",
            t=1.25,
            at=_at(56),
            message="g is a fixed parameter, so it can't change during a run. Set it with "
            "start_values in a new run.",
        ),
    )

    pi, plant = _fmu("pi_controller"), _fmu("MSD")
    loop = SystemSpec(
        run_id="r2",
        created_at=_at(2),
        name="loop",
        system=FileRef(path=f"{ROOT}/loop.json", sha256=SHA, size_bytes=420),
        components={"PI": ComponentSpec(fmu=pi), "plant": ComponentSpec(fmu=plant)},
        connections=[
            SystemConnection.model_validate({"from": "PI.u", "to": "plant.F"}),
            SystemConnection.model_validate({"from": "plant.x", "to": "PI.y"}),
        ],
        parameters={
            "PI.Ki": ResolvedValue(value=15.0, source="system"),
            "PI.Kp": ResolvedValue(value=10.0, source="start_values"),
        },
        master=MasterAlgorithm(step_size=0.01),
        experiment=Experiment(start_time=0.0, stop_time=10.0, output_interval=0.01),
        outputs=["plant.x", "PI.u"],
        timeout_s=TIMING.default_timeout_s,
        versions=VERSIONS,
        omsimulator=OmsVersion(version="3.0.0.post198", sha256=SHA),
        live=True,
        idle_limit_s=TIMING.live_idle_limit_s,
    )
    put("runs/r2/spec.json", loop)
    put("runs/r2/status.json", _status("r2", RunState.RUNNING, 4.2, 10.0, speed=2.0))
    put("runs/r2/live.json", _live("r2", [("plant.x", "m"), ("PI.u", "N")], 4.2, 0.01))

    batch = ball.model_copy(update={"run_id": "r3", "live": False, "idle_limit_s": None})
    put("runs/r3/spec.json", batch)
    put("runs/r3/status.json", _status("r3", RunState.FINISHED, 3.0, 3.0))

    source = SourceRef(path="pi_controller.py", sha256=SHA, size_bytes=1_234)
    options = {"fmi_version": "2.0", "fmu_type": "cs"}
    put(
        "runs/r4/spec.json",
        BuildSpec(
            run_id="r4",
            created_at=_at(3),
            workspace=ROOT,
            source=source,
            name="pi_controller",
            fmu_path="generated/pi_controller/pi_controller.fmu",
            backend="pythonfmu",
            options=options,
            timeout_s=TIMING.build_timeout_s,
            versions=VERSIONS,
        ),
    )
    status = _status("r4", RunState.FINISHED, 0.0, 0.0).model_copy(
        update={"t": 0.0, "progress": 1.0}
    )
    put("runs/r4/status.json", status)
    put(
        "runs/r4/build.json",
        BuildRecord(
            run_id="r4",
            name="pi_controller",
            built_at=_at(20),
            backend="pythonfmu",
            source=source,
            options=options,
            versions=BuildVersions(
                agentic_fmi="0.1.0", python="3.12.12", platform="x86_64-linux", pythonfmu="0.7.0"
            ),
            fmu=BuiltFmu(
                path="generated/pi_controller/pi_controller.fmu",
                sha256=SHA,
                size_bytes=660_000,
                fmi_version="2.0",
                model_name="PIController",
                model_identifier="PIController",
                interfaces=["CoSimulation"],
            ),
            duration_s=6.4,
            warnings=[],
            summary=BuildSummary(
                counts={"input": 1, "output": 1, "parameter": 3, "total": 5},
                inputs=["y"],
                outputs=["u"],
                parameters=["Kp", "Ki", "r"],
            ),
        ),
    )

    failed = batch.model_copy(update={"run_id": "r5", "start_values": {"e": "bouncy"}})
    put("runs/r5/spec.json", failed)
    error = RunError(
        code=ErrorCode.SIMULATION_ERROR,
        message="The simulation failed: ValueError: could not convert string to float: 'bouncy'. "
        "Fix start_values and start a new run.",
        log_tail="Traceback (most recent call last): ...",
    )
    put("runs/r5/status.json", _status("r5", RunState.FAILED, 0.0, 3.0, error=error))

    entries = []
    for spec, state in (
        (ball, RunState.PAUSED),
        (loop, RunState.RUNNING),
        (batch, RunState.FINISHED),
        (None, RunState.FINISHED),
        (failed, RunState.FAILED),
    ):
        if spec is None:
            entries.append(
                IndexEntry(
                    run_id="r4",
                    kind="build",
                    state=state,
                    fmu_name="pi_controller",
                    fmu_path=f"{ROOT}/generated/pi_controller/pi_controller.fmu",
                    created_at=_at(3),
                    updated_at=_at(60),
                )
            )
            continue
        name = spec.name if isinstance(spec, SystemSpec) else spec.fmu.model_name
        path = spec.system.path if isinstance(spec, SystemSpec) else spec.fmu.path
        entries.append(
            IndexEntry(
                run_id=spec.run_id,
                kind=spec.kind,
                state=state,
                fmu_name=name,
                fmu_path=path,
                created_at=spec.created_at,
                updated_at=_at(60),
            )
        )
    put("index.json", Index(runs=entries))
    files[f"{STORE}/events.jsonl"] = _text(
        BuildEvent(
            at=_at(20),
            run_id="r4",
            fmu_path="generated/pi_controller/pi_controller.fmu",
            fmu_sha256=SHA,
            message="pi_controller (pythonfmu, 6.40 s)",
        )
    )
    return dict(sorted(files.items()))


def variants(files: dict[str, str]) -> dict[str, str]:
    """Documents the pane must survive: a newer protocol, an unknown state, a torn file."""
    status = json.loads(files[f"{STORE}/runs/r1/status.json"])
    newer = {**status, "protocol": 2}
    unknown = {**status, "state": "hibernating"}
    live = files[f"{STORE}/runs/r1/live.json"]
    return {
        "status_protocol_2": json.dumps(newer, separators=(",", ":")) + "\n",
        "status_unknown_state": json.dumps(unknown, separators=(",", ":")) + "\n",
        "live_torn": live[: len(live) // 2],
        "live_empty": "",
    }


def worst_live() -> str:
    """The largest live.json the limits allow (long names and units, 12-character numbers)."""
    columns = [
        (f"component_{i:02d}.{'a_long_variable_name_' * 4}{i:03d}"[:100], "kg.m2/(s3.A)" + "x" * 4)
        for i in range(LIMITS.live_max_columns)
    ]
    count = LIMITS.live_points
    times = [_round(-1.23457e-05 * (count - i)) or 0.0 for i in range(count)]
    snapshot = LiveSnapshot(
        run_id="r999999",
        seq=999_999_999,
        t=times[-1],
        values=[LiveColumn(name=n, unit=u, value=-1.23457e-05) for n, u in columns],
        time=times,
        series={n: [-1.23457e-05] * count for n, _ in columns[: LIMITS.live_variables]},
    )
    return _text(snapshot)


def discovery() -> list[dict[str, str | None]]:
    """``AFMI_STORE_DIR`` cases and the store ``config.load_config`` resolves for root ``/ws``.

    Computed in a temporary directory standing for ``/`` (``<tmp>/ws`` is the session root) and
    with ``HOME`` mapped to ``/home/dev``, so the cases read as the pane sees them."""
    cases: list[str | None] = [
        None,
        "",
        "   ",
        "/lab/store",
        " /lab/store ",
        "store",
        "nested/store/",
        "../shared/store",
        "./.afmi",
        "~/afmi-store",
    ]
    real_home = str(Path("~").expanduser())
    rows: list[dict[str, str | None]] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        root = base / "ws"
        root.mkdir()
        for value in cases:
            env = {} if value is None else {"AFMI_STORE_DIR": value}
            store = str(load_config(root, env=env).store_dir)
            if store.startswith(str(base)):
                store = store[len(str(base)) :] or "/"
            elif store.startswith(real_home):
                store = HOME + store[len(real_home) :]
            rows.append({"env": value, "home": HOME, "store": store})
    return rows


def pane_commands() -> dict[str, str]:
    """The file each pane button writes at ``NOW_MS``: ``id`` ``<NOW_MS>-pane-<8 hex>`` (the
    tests normalize the random part) and ``issued_at`` as ``new Date(NOW_MS).toISOString()``."""
    command_id = f"{NOW_MS}-pane-00000000"
    issued = datetime.fromtimestamp(NOW_MS / 1000, UTC)
    js_time = issued.strftime("%Y-%m-%dT%H:%M:%S.") + f"{issued.microsecond // 1000:03d}Z"
    arguments: dict[str, dict[str, Any]] = {
        "pause": {"action": "pause"},
        "resume": {"action": "resume"},
        "step": {"action": "step", "n": 1},
        "set": {"action": "set", "values": {"e": 0.6}},
        "stop": {"action": "stop"},
        "chart": {"action": "chart", "names": ["h"]},  # r1: the chart of v removed with its ✕
    }
    golden: dict[str, str] = {}
    for button, fields in arguments.items():
        command = Command(id=command_id, source="pane", issued_at=issued, **fields)
        data = command.model_dump(mode="json", exclude_none=True)
        data["issued_at"] = js_time
        golden[button] = json.dumps(data, separators=(",", ":"))
    return golden


def _ts_value(value: object) -> str:
    """JSON is valid TypeScript for strings, numbers, null, arrays and objects."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def render() -> str:
    """The whole ``pane/fixtures/store.ts`` text."""
    files = store_files()
    parts = [
        "// Generated by scripts/pane_fixtures.py from the pydantic models in",
        "// src/agentic_fmi/store/protocol.py. Do not edit: run",
        "// `uv run python scripts/pane_fixtures.py` and commit the result",
        "// (tests/store/test_pane_fixtures.py compares it byte for byte).",
        "",
        f"export const ROOT = {_ts_value(ROOT)}",
        f"export const STORE = {_ts_value(STORE)}",
        f"export const NOW_MS = {NOW_MS}",
        "",
        "/** Absolute path -> file text of the fixture run store. */",
        f"export const FILES: Readonly<Record<string, string>> = {_ts_value(files)}",
        "",
        "/** Files a newer or broken writer could leave (kept apart from FILES). */",
        f"export const VARIANTS: Readonly<Record<string, string>> = {_ts_value(variants(files))}",
        "",
        "/** The largest live.json the limits allow (under Limits.live_max_bytes). */",
        f"export const WORST_LIVE: string = {_ts_value(worst_live())}",
        "",
        "/** AFMI_STORE_DIR (null: unset) -> the store config.load_config resolves for ROOT. */",
        "export const DISCOVERY: readonly { env: string | null; home: string; store: string }[] = "
        + _ts_value(discovery()),
        "",
        "/** Button -> the command JSON the pane writes at NOW_MS (id random part 00000000). */",
        "export const PANE_COMMANDS: Readonly<Record<string, string>> = "
        + _ts_value(pane_commands()),
        "",
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT} is stale; run uv run python scripts/pane_fixtures.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} ({len(text.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
