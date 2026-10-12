"""The run use cases in ``runs.py``, without the MCP layer and mostly without workers.

Collaborators: ``agentic_fmi.runs`` (under test), ``agentic_fmi.budget`` (sizes),
``agentic_fmi.supervisor.Supervisor`` (real, with a faked ``spawn`` where no process is wanted),
``agentic_fmi.store.runstore`` (to fabricate finished runs with chosen result tables, so result,
comparison and plot logic can be checked on exact numbers), and the shared ``workspace`` and
``fmu`` fixtures. No FMU binary is loaded here.
"""

from __future__ import annotations

import threading
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from agentic_fmi import budget, runs
from agentic_fmi.analysis.summary import summarize
from agentic_fmi.config import Config, Timing, load_config
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.inspect import read_description
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import (
    Experiment,
    FmuRef,
    IndexEntry,
    RunError,
    RunSpec,
    RunState,
    RunStatus,
    RunSummary,
    Table,
    Versions,
    utc_now,
)
from agentic_fmi.supervisor import Supervisor

BUDGET = 8_192
HARD_CAP = 16_384
AddRun = Callable[..., str]
FmuFactory = Callable[[str, str], Path]


@pytest.fixture
def config(workspace: Path) -> Config:
    return replace(
        load_config(workspace), timing=Timing(heartbeat_stale_s=4.0, poll_interval_s=0.02)
    )


@pytest.fixture
def supervisor(config: Config) -> Supervisor:
    return Supervisor(config)


@pytest.fixture
def add_run(config: Config) -> AddRun:
    """``add_run(columns=..., rows=..., state=..., start_values=..., inputs=...)`` fabricates a
    run in the store: spec, status, summary, index entry and (for finished or stopped runs)
    ``result.csv``. ``columns`` maps names to arrays, or to a function of the time array."""

    def build(
        columns: dict[str, Any] | None = None,
        *,
        rows: int = 101,
        t_end: float = 1.0,
        state: RunState = RunState.FINISHED,
        start_values: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        result: bool = True,
        error: RunError | None = None,
        model: str = "Fake",
    ) -> str:
        run_id, path = runstore.claim_run_id(config.store_dir)
        time = np.linspace(0.0, t_end, rows)
        values = columns if columns is not None else {"h": lambda t: 1.0 - t}
        table = {
            name: v(time) if callable(v) else np.asarray(v, dtype=float)
            for name, v in values.items()
        }
        spec = RunSpec(
            run_id=run_id,
            created_at=utc_now(),
            fmu=FmuRef(
                path=str(config.workspace / "Fake.fmu"),
                sha256="a" * 64,
                size_bytes=1,
                fmi_version="3.0",
                model_name=model,
            ),
            fmi_type="CoSimulation",
            experiment=Experiment(start_time=0.0, stop_time=t_end, output_interval=t_end / 100),
            start_values=start_values or {},
            inputs=inputs or {},
            outputs=list(table),
            timeout_s=60.0,
            versions=Versions.current(),
        )
        runstore.write_spec(path, spec)
        status = RunStatus(
            run_id=run_id,
            state=state,
            t=t_end,
            t_start=0.0,
            t_stop=t_end,
            progress=1.0,
            updated_at=utc_now(),
            error=error,
        )
        runstore.write_status(path, status)
        if result and state in (RunState.FINISHED, RunState.STOPPED):
            array = np.empty(rows, dtype=[("time", float), *((n, float) for n in table)])
            array["time"] = time
            for name, column in table.items():
                array[name] = column
            runstore.write_result(path, array)
            runstore.write_summary(
                path,
                RunSummary(
                    run_id=run_id,
                    state=state,
                    t_end=t_end,
                    n_points=rows,
                    wall_time_s=0.1,
                    columns=list(table),
                    variables=summarize(array, {name: "m" for name in table}),
                ),
            )
        runstore.upsert_index(
            config.store_dir,
            IndexEntry(
                run_id=run_id,
                state=state,
                fmu_name=model,
                fmu_path=spec.fmu.path,
                created_at=spec.created_at,
                updated_at=status.updated_at,
            ),
        )
        return run_id

    return build


def noisy(seed: int) -> Callable[[np.ndarray], np.ndarray]:
    """A column of many-digit random numbers (the worst case for the JSON size)."""
    return lambda t: np.random.default_rng(seed).normal(size=t.size) * 1234.5678


# --- run_results -------------------------------------------------------------------------------


def test_results_default_to_100_points_of_the_first_four_variables(
    config: Config, add_run: AddRun
) -> None:
    run = add_run({name: noisy(i) for i, name in enumerate("abcdef")}, rows=2000)
    result = runs.get_results(config, run)
    assert [s.name for s in result.series] == ["a", "b", "c", "d"]
    assert result.n_points_total == 2000
    assert result.t_start == 0.0 and result.t_end == 1.0
    assert all(len(s.t) <= result.n_points_total for s in result.series)
    assert (
        result.note is not None and "6 variables were recorded; showing the first 4" in result.note
    )
    assert "e, f" in result.note
    assert budget.json_size(result) <= BUDGET


def test_results_are_rounded_to_six_significant_digits_and_keep_the_extrema(
    config: Config, add_run: AddRun
) -> None:
    run = add_run({"x": lambda t: np.sin(t * 20) * 0.123456789}, rows=1000, t_end=3.0)
    result = runs.get_results(config, run, variables=["x"], max_points=30)
    (series,) = result.series
    assert len(series.y) <= 30
    assert max(y for y in series.y if y is not None) == pytest.approx(0.123457, abs=1e-6)
    assert series.t[0] == 0.0 and series.t[-1] == 3.0
    assert all(
        len(repr(y).replace("-", "").replace(".", "").lstrip("0")) <= 8 for y in series.y if y
    )
    assert series.unit == "m"


def test_too_many_points_are_halved_until_the_json_fits_and_the_note_says_so(
    config: Config, add_run: AddRun
) -> None:
    run = add_run({f"v{i}": noisy(i) for i in range(8)}, rows=2000)
    result = runs.get_results(config, run, variables=[f"v{i}" for i in range(8)])
    assert budget.json_size(result) <= BUDGET
    points = max(len(s.t) for s in result.series)
    assert 4 <= points < 100
    assert result.note is not None and f"Reduced from 100 to {points} points" in result.note
    assert "run_plot" in result.note


def test_raising_max_points_allows_the_hard_cap_and_max_points_is_capped_at_500(
    config: Config, add_run: AddRun
) -> None:
    run = add_run({"a": noisy(1), "b": noisy(2), "c": noisy(3)}, rows=5000)
    result = runs.get_results(config, run, variables=["a", "b", "c"], max_points=10_000)
    size = budget.json_size(result)
    assert BUDGET < size <= HARD_CAP
    assert all(len(s.t) <= 500 for s in result.series)
    assert max(len(s.t) for s in result.series) > 100


def test_non_finite_values_come_back_as_nulls_and_windows_select_rows(
    config: Config, add_run: AddRun
) -> None:
    run = add_run({"x": lambda t: np.where(t > 0.5, np.nan, t)}, rows=11)
    full = runs.get_results(config, run, variables=["x"])
    assert full.series[0].y.count(None) == 5
    window = runs.get_results(config, run, variables=["x"], start_time=0.2, stop_time=0.4)
    assert window.series[0].t == [0.2, 0.3, 0.4]
    assert window.n_points_total == 3 and window.t_start == 0.2 and window.t_end == 0.4


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"variables": ["hh"]}, "Close matches: h."),
        ({"variables": [f"v{i}" for i in range(9)]}, "at most 8 variables"),
        ({"max_points": 3}, "max_points must be at least 4"),
        ({"start_time": 0.5, "stop_time": 0.5}, "must be greater than start_time"),
        ({"start_time": 5.0}, "run covers t=0 to t=1"),
    ],
)
def test_results_errors_name_the_fix(
    config: Config, add_run: AddRun, arguments: dict[str, Any], expected: str
) -> None:
    run = add_run()
    with pytest.raises(UserError, match=expected):
        runs.get_results(config, run, **arguments)


def test_results_of_runs_without_a_result_say_why(config: Config, add_run: AddRun) -> None:
    running = add_run(state=RunState.RUNNING)
    failed = add_run(
        state=RunState.FAILED,
        error=RunError(code="timeout", message="too slow"),
    )
    stopped = add_run(state=RunState.STOPPED, result=False)
    with pytest.raises(UserError, match=f"Run {running} is running; only finished or stopped"):
        runs.get_results(config, running)
    with pytest.raises(UserError, match=f"Run {failed} failed \\(timeout\\)"):
        runs.get_results(config, failed)
    with pytest.raises(UserError, match=f"Run {stopped} was stopped before it wrote a result"):
        runs.get_results(config, stopped)
    with pytest.raises(UserError, match="There is no run r99"):
        runs.get_results(config, "r99")
    with pytest.raises(UserError, match="not a run ID"):
        runs.get_results(config, "../x")


# --- run_compare -------------------------------------------------------------------------------


def test_compare_defaults_to_the_variables_every_run_recorded(
    config: Config, add_run: AddRun
) -> None:
    first = add_run({"h": lambda t: 1 - t, "v": lambda t: -t}, start_values={"e": 0.5})
    second = add_run({"h": lambda t: 1 - 2 * t, "w": lambda t: t}, start_values={"e": 0.8})
    comparison = runs.compare_runs(config, [first, second])
    assert [v.name for v in comparison.variables] == ["h"]
    (difference,) = comparison.spec_differences
    assert difference.field == "start_values.e" and difference.values == {first: 0.5, second: 0.8}
    metrics = {m.run_id: m for m in comparison.variables[0].runs}
    assert metrics[second].max_abs_diff == pytest.approx(1.0)
    assert metrics[first].max_abs_diff is None


def test_compare_rejects_bad_run_lists_and_unknown_variables(
    config: Config, add_run: AddRun
) -> None:
    first, second = add_run(), add_run({"x": lambda t: t})
    with pytest.raises(UserError, match="listed twice"):
        runs.compare_runs(config, [first, first])
    with pytest.raises(UserError, match="Pass 2 to 6 runs"):
        runs.compare_runs(config, [first])
    with pytest.raises(UserError, match="Pass 2 to 6 runs"):
        runs.compare_runs(config, [add_run() for _ in range(7)])
    with pytest.raises(UserError, match="no recorded variable in common"):
        runs.compare_runs(config, [first, second])
    with pytest.raises(UserError, match=r"Unknown variable 'hh'.*Close matches: h"):
        runs.compare_runs(config, [first, add_run()], variables=["hh"])
    with pytest.raises(UserError, match="At most 6 variables"):
        runs.compare_runs(config, [first, add_run()], variables=[f"v{i}" for i in range(7)])


def test_compare_clips_long_input_tables(config: Config, add_run: AddRun) -> None:
    first = add_run(inputs={"u": Table(points=[(i / 100, float(i)) for i in range(500)])})
    second = add_run(inputs={"u": Table(points=[(i / 100, float(2 * i)) for i in range(500)])})
    comparison = runs.compare_runs(config, [first, second])
    (difference,) = comparison.spec_differences
    assert difference.field == "inputs.u"
    texts = [v for v in difference.values.values() if isinstance(v, str)]
    assert len(texts) == 2 and all(len(t) == runs.MAX_DIFFERENCE_CHARS for t in texts)
    assert all(t.endswith("...") for t in texts)
    assert budget.json_size(comparison) <= BUDGET


def test_compare_caps_the_number_of_spec_differences_and_says_so(
    config: Config, add_run: AddRun
) -> None:
    values = {f"p{i}": float(i) for i in range(40)}
    first = add_run(start_values=values)
    second = add_run(start_values={k: v + 1 for k, v in values.items()})
    comparison = runs.compare_runs(config, [first, second])
    assert len(comparison.spec_differences) == runs.MAX_SPEC_DIFFERENCES
    assert comparison.notes == [f"{runs.DIFFERENCES_NOTE} 25 of 40 spec differences."]
    assert budget.json_size(comparison) <= BUDGET


# --- run_plot ----------------------------------------------------------------------------------


def test_plot_saves_one_png_under_the_first_run_and_labels_lines_by_spec(
    config: Config, add_run: AddRun
) -> None:
    first = add_run(start_values={"e": 0.5})
    second = add_run(start_values={"e": 0.8})
    plot = runs.plot_runs(config, [first, second], title="demo")
    assert plot.png.startswith(b"\x89PNG") and len(plot.png) <= config.limits.plot_max_bytes
    assert plot.path == config.store_dir / "runs" / first / "plots" / f"h__{first}+{second}.png"
    assert plot.path.read_bytes() == plot.png
    assert plot.caption.endswith(f"saved to .agentic-fmi/runs/{first}/plots/{plot.path.name}")
    labels = runs._legend_labels(
        [runstore.read_spec(config.store_dir / "runs" / r) for r in (first, second)]
    )
    assert labels == {first: f"{first} (e=0.5)", second: f"{second} (e=0.8)"}


def test_plot_file_names_are_sanitized_short_and_distinct() -> None:
    name = runs._plot_file_name(["der(h)", "m[1,2]"], ["r1", "r2"])
    assert name == "der_h_+m_1_2___r1+r2.png"
    names = [f"variable_with_a_long_name_{i:02d}" for i in range(8)]
    long_one = runs._plot_file_name(names, ["r1"])
    other = runs._plot_file_name([*names[:-1], "variable_with_a_long_name_xx"], ["r1"])
    assert len(long_one) <= runs.MAX_PLOT_FILE_CHARS and long_one.endswith(".png")
    assert long_one != other and len(other) <= runs.MAX_PLOT_FILE_CHARS


def test_plot_errors_name_the_fix(
    config: Config, add_run: AddRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = add_run()
    with pytest.raises(UserError, match=r"Unknown variable 'hh'.*Close matches: h"):
        runs.plot_runs(config, [run], variables=["hh"])
    with pytest.raises(UserError, match="At most 6 variables"):
        runs.plot_runs(config, [run], variables=[f"v{i}" for i in range(7)])
    with pytest.raises(UserError, match="Pass 1 to 6 runs"):
        runs.plot_runs(config, [])
    monkeypatch.setattr(
        runs, "render_png", lambda *a, **k: (_ for _ in ()).throw(ValueError("plot fewer runs"))
    )
    with pytest.raises(UserError, match="plot fewer runs"):
        runs.plot_runs(config, [run])
    assert not (config.store_dir / "runs" / run / "plots").exists()


# --- run_list, run_stop, views -----------------------------------------------------------------


def test_list_runs_pages_newest_first_and_filters_by_state(
    config: Config, supervisor: Supervisor, add_run: AddRun
) -> None:
    ids = [add_run(state=RunState.FINISHED if i % 2 else RunState.FAILED) for i in range(5)]
    page = runs.list_runs(config, supervisor, limit=2)
    assert [i.run_id for i in page.items] == ["r5", "r4"]
    assert (page.total, page.offset, page.limit, page.next_offset) == (5, 0, 2, 2)
    last = runs.list_runs(config, supervisor, offset=4, limit=2)
    assert [i.run_id for i in last.items] == ["r1"] and last.next_offset is None
    failed = runs.list_runs(config, supervisor, state=RunState.FAILED)
    assert [i.run_id for i in failed.items] == [ids[4], ids[2], ids[0]]
    assert runs.list_runs(config, supervisor, state=RunState.STOPPED).total == 0
    with pytest.raises(UserError, match="past the end: there are 5 runs"):
        runs.list_runs(config, supervisor, offset=5)


def test_list_runs_reports_the_reconciled_state_of_a_dead_worker(
    config: Config, supervisor: Supervisor, add_run: AddRun
) -> None:
    run = add_run(state=RunState.RUNNING)
    path = config.store_dir / "runs" / run
    gone = runstore.read_status(path).model_copy(
        update={"pid": 2**22 - 3, "pid_start_time": 1, "heartbeat_at": utc_now()}
    )
    runstore.write_status(path, gone)
    page = runs.list_runs(config, supervisor)
    assert [(i.run_id, i.state) for i in page.items] == [(run, RunState.FAILED)]
    view = runs.get_run(config, supervisor, run)
    assert view.error is not None and view.error.code.value == "worker_lost"
    assert "run_start" in (view.hint or "")


def test_stop_run_says_to_call_again_when_the_worker_is_not_confirmed_stopped(
    config: Config, add_run: AddRun
) -> None:
    run = add_run(state=RunState.RUNNING)
    status = runstore.read_status(config.store_dir / "runs" / run)

    class Stuck:
        def stop(self, run_id: str) -> RunStatus:
            return status

    view = runs.stop_run(config, Stuck(), run)
    assert view.state is RunState.RUNNING
    assert view.hint is not None and f"call run_stop('{run}') again" in view.hint


def test_view_rounds_and_shrinks_to_the_budget(
    config: Config, add_run: AddRun, supervisor: Supervisor
) -> None:
    names = {f"variable_number_{i:03d}": (lambda t: t * 0.123456789) for i in range(60)}
    run = add_run(names)
    view = runs.get_run(config, supervisor, run)
    assert view.summary is not None
    assert len(view.summary.variables) <= config.limits.inspect_max_items
    assert view.summary.truncated is True
    assert budget.json_size(view) <= BUDGET
    assert view.progress == 1.0 and view.t_stop == 1.0
    assert view.fmu == "Fake.fmu"
    long_tail = RunError(code="simulation_error", message="m", log_tail="x" * 60_000)
    failed = budget.fit_to_budget(
        view.model_copy(update={"summary": None, "error": long_tail, "state": RunState.FAILED}),
        BUDGET,
        runs._smaller_view,
    )
    assert failed.error is not None and failed.error.log_tail is not None
    assert len(failed.error.log_tail) < 60_000 and budget.json_size(failed) <= BUDGET


# --- start_run checks that need no worker --------------------------------------------------------


def fmi2_xml(variables: Sequence[tuple[str, str, str, str]]) -> str:
    """A schema-valid FMI 2.0 CS description from ``(name, causality, variability, type)``."""
    rows, outputs = [], []
    for index, (name, causality, variability, kind) in enumerate(variables):
        given = causality in ("parameter", "input")
        start = (' start="x"' if kind == "String" else ' start="0"') if given else ""
        initial = ' initial="calculated"' if causality == "output" else ""
        rows.append(
            f'<ScalarVariable name="{name}" valueReference="{index}" causality="{causality}" '
            f'variability="{variability}"{initial}><{kind}{start}/></ScalarVariable>'
        )
        if causality == "output":
            outputs.append(f'<Unknown index="{index + 1}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<fmiModelDescription fmiVersion="2.0" modelName="Synth" guid="{s}">'
        '<CoSimulation modelIdentifier="Synth"/>'
        f"<ModelVariables>{''.join(rows)}</ModelVariables>"
        f"<ModelStructure>{f'<Outputs>{chr(10).join(outputs)}</Outputs>' if outputs else ''}"
        "</ModelStructure>"
        "</fmiModelDescription>"
    )


def description(workspace: Path, variables: Sequence[tuple[str, str, str, str]]) -> Any:
    path = workspace / "synth.fmu"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelDescription.xml", fmi2_xml(variables))
    return read_description(path)


def test_outputs_default_to_the_fmus_outputs_then_to_continuous_states(
    config: Config, workspace: Path
) -> None:
    with_outputs = description(
        workspace,
        [
            ("p", "parameter", "fixed", "Real"),
            ("y", "output", "continuous", "Real"),
            ("s", "output", "discrete", "String"),
            ("n", "output", "discrete", "Integer"),
        ],
    )
    assert runs._checked_outputs(with_outputs, None, config.limits) == ["y", "n"]
    without = description(
        workspace,
        [
            ("p", "parameter", "fixed", "Real"),
            ("x", "local", "continuous", "Real"),
            ("u", "input", "continuous", "Real"),
            ("k", "local", "discrete", "Integer"),
        ],
    )
    assert runs._checked_outputs(without, [], config.limits) == ["x", "u"]
    small = replace(config.limits, page_max=1)
    assert runs._checked_outputs(without, None, small) == ["x"]
    empty = description(workspace, [("p", "parameter", "fixed", "Real")])
    with pytest.raises(UserError, match="declares no outputs and no continuous variables"):
        runs._checked_outputs(empty, None, config.limits)


def test_requested_outputs_are_checked_against_the_model(config: Config, workspace: Path) -> None:
    md = description(
        workspace,
        [("y", "output", "continuous", "Real"), ("s", "parameter", "fixed", "String")],
    )
    assert runs._checked_outputs(md, ["y", "y", "time"], config.limits) == ["y"]
    with pytest.raises(UserError, match="Output 's' is a String variable"):
        runs._checked_outputs(md, ["s"], config.limits)
    with pytest.raises(UserError, match=r"Unknown variable 'yy' in outputs[.] Close matches: y"):
        runs._checked_outputs(md, ["yy"], config.limits)
    with pytest.raises(UserError, match="names only 'time'"):
        runs._checked_outputs(md, ["time"], config.limits)


def test_start_run_validates_limits_and_values_before_creating_anything(
    config: Config, supervisor: Supervisor, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    ball = "3.0/BouncingBall.fmu"
    for arguments, expected in (
        ({"timeout_s": 0.0}, "timeout_s must be above 0"),
        ({"timeout_s": 1e9}, "timeout_s must be above 0 and at most 86400"),
        ({"wait_s": -1.0}, "wait_s must be 0 or more"),
        ({"start_values": {"e": float("nan")}}, "must be finite numbers"),
        ({"stop_time": 0.0, "start_time": 1.0}, "stop_time"),
        ({"outputs": ["nope"]}, "Unknown variable 'nope' in outputs"),
    ):
        with pytest.raises(UserError, match=expected):
            runs.start_run(config, supervisor, ball, **arguments)
    assert runstore.list_run_ids(config.store_dir) == []  # no ID was claimed


def test_concurrent_starts_cannot_both_take_the_last_worker_slot(
    config: Config, fmu: FmuFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fmu("3.0", "BouncingBall")
    monkeypatch.setattr(Supervisor, "spawn", lambda self, run_dir: 12345)
    supervisor = Supervisor(replace(config, max_workers=1))
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def start() -> None:
        barrier.wait()
        try:
            view = runs.start_run(
                replace(config, max_workers=1), supervisor, "3.0/BouncingBall.fmu", wait_s=0
            )
            outcomes.append(view.run_id)
        except UserError as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    started = [o for o in outcomes if o.startswith("r") and o[1:].isdigit()]
    refused = [o for o in outcomes if "limit is 1" in o]
    assert len(started) == 1 and len(refused) == 5, outcomes
    assert runstore.list_run_ids(config.store_dir) == started
