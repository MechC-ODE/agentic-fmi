"""Tests for ``store/runstore.py``: run-ID claims, per-run documents, results, the index, the
event log and log tails, on a temporary store.

Collaborators: ``agentic_fmi.store.runstore`` (under test), ``agentic_fmi.store.protocol`` (the
documents), ``agentic_fmi.config`` (where the store root comes from), and the errors in
``agentic_fmi.errors``. Concurrency across processes is in ``test_runstore_concurrency.py``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from agentic_fmi.config import STORE_DIR_ENV, STORE_DIR_NAME, load_config
from agentic_fmi.errors import ProtocolError, UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import UnsupportedProtocolError, dump_json
from agentic_fmi.store.protocol import (
    MAX_EVENT_LINE_BYTES,
    Event,
    EventType,
    Experiment,
    FmuRef,
    Index,
    IndexEntry,
    RunSpec,
    RunState,
    RunStatus,
    RunSummary,
    VariableSummary,
    Versions,
)

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 9, 12, 5, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """An initialised, empty store directory."""
    path = tmp_path / "store"
    runstore.ensure_store(path)
    return path


def _spec(run_id: str, model_name: str = "Model") -> RunSpec:
    return RunSpec(
        run_id=run_id,
        created_at=NOW,
        fmu=FmuRef(
            path=f"/ws/{model_name}.fmu",
            sha256="a" * 64,
            size_bytes=123,
            fmi_version="2.0",
            model_name=model_name,
        ),
        fmi_type="CoSimulation",
        experiment=Experiment(start_time=0.0, stop_time=10.0, output_interval=0.1),
        outputs=["x"],
        timeout_s=60.0,
        versions=Versions(agentic_fmi="0", fmpy="0", numpy="0", python="3.12", platform="x"),
    )


def _status(run_id: str, state: RunState = RunState.QUEUED, **changes: Any) -> RunStatus:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "state": state,
        "t_start": 0.0,
        "t_stop": 10.0,
        "updated_at": NOW,
    }
    return RunStatus(**{**fields, **changes})


def _entry(run_id: str, state: RunState = RunState.QUEUED, name: str = "Model") -> IndexEntry:
    return IndexEntry(
        run_id=run_id,
        state=state,
        fmu_name=name,
        fmu_path=f"/ws/{name}.fmu",
        created_at=NOW,
        updated_at=NOW,
    )


def _create_run(store: Path, state: RunState = RunState.QUEUED, **status: Any) -> str:
    """Claim a run and write its spec and status, as the server does."""
    run_id, path = runstore.claim_run_id(store)
    runstore.write_spec(path, _spec(run_id))
    runstore.write_status(path, _status(run_id, state, **status))
    return run_id


# --- store root from Config -----------------------------------------------------------------


def test_default_store_dir_is_inside_the_workspace(workspace: Path) -> None:
    config = load_config(workspace, env={})
    assert config.store_dir == workspace / STORE_DIR_NAME
    runstore.ensure_store(config.store_dir)
    run_id, path = runstore.claim_run_id(config.store_dir)
    assert path == workspace / STORE_DIR_NAME / "runs" / run_id


def test_afmi_store_dir_override_moves_the_whole_store(workspace: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    config = load_config(workspace, env={STORE_DIR_ENV: str(elsewhere)})
    assert config.store_dir == elsewhere.resolve()
    run_id = _create_run(config.store_dir)
    runstore.upsert_index(config.store_dir, _entry(run_id))
    runstore.append_event(
        config.store_dir, Event(at=NOW, run_id=run_id, type=EventType.CREATED, source="server")
    )
    assert (elsewhere / "runs" / run_id / "spec.json").is_file()
    assert (elsewhere / "index.json").is_file()
    assert (elsewhere / "events.jsonl").is_file()
    assert not (workspace / STORE_DIR_NAME).exists()


def test_relative_override_is_resolved_against_the_workspace(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STORE_DIR_ENV, "state/store")
    config = load_config(workspace)
    runstore.ensure_store(config.store_dir)
    assert config.store_dir == workspace / "state" / "store"
    assert (workspace / "state" / "store" / "runs").is_dir()


# --- ensure_store and run IDs ---------------------------------------------------------------


def test_ensure_store_creates_runs_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "store"
    runstore.ensure_store(path)
    runstore.ensure_store(path)
    assert (path / "runs").is_dir()


def test_claims_are_sequential_and_create_empty_directories(store: Path) -> None:
    claimed = [runstore.claim_run_id(store) for _ in range(3)]
    assert [run_id for run_id, _ in claimed] == ["r1", "r2", "r3"]
    for run_id, path in claimed:
        assert path == store / "runs" / run_id
        assert path.is_dir() and list(path.iterdir()) == []


def test_claim_works_on_a_store_that_was_never_initialised(tmp_path: Path) -> None:
    run_id, path = runstore.claim_run_id(tmp_path / "fresh")
    assert run_id == "r1" and path.is_dir()


def test_claim_starts_above_the_highest_run_and_ignores_other_names(store: Path) -> None:
    for name in ["r2", "r10", "r0", "r01", "r-1", "rx", "s20", "st30", "r٣", ".hidden"]:
        (store / "runs" / name).mkdir()
    (store / "runs" / "r50").write_text("a file, not a run")
    assert runstore.claim_run_id(store)[0] == "r11"
    assert runstore.list_run_ids(store) == ["r2", "r10", "r11"]


def test_claim_steps_over_an_entry_that_blocks_the_next_number(store: Path) -> None:
    (store / "runs" / "r2").mkdir()
    (store / "runs" / "r3").write_text("squatter")  # a file named like a run is not a run
    assert runstore.claim_run_id(store)[0] == "r4"


def test_claims_from_threads_are_distinct(store: Path) -> None:
    ids: list[str] = []
    guard = threading.Lock()

    def claim() -> None:
        for _ in range(10):
            run_id, _ = runstore.claim_run_id(store)
            with guard:
                ids.append(run_id)

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == len(set(ids)) == 40
    assert runstore.list_run_ids(store) == [f"r{n}" for n in range(1, 41)]


def test_list_run_ids_is_numeric_not_lexical(store: Path) -> None:
    for number in (10, 2, 1, 100, 9):
        (store / "runs" / f"r{number}").mkdir()
    assert runstore.list_run_ids(store) == ["r1", "r2", "r9", "r10", "r100"]


def test_list_run_ids_without_a_store_is_empty(tmp_path: Path) -> None:
    assert runstore.list_run_ids(tmp_path / "nothing") == []


def test_run_dir_returns_the_existing_run(store: Path) -> None:
    run_id, path = runstore.claim_run_id(store)
    assert runstore.run_dir(store, run_id) == path


@pytest.mark.parametrize("bad", ["", "r0", "r01", "x1", "../r1", "r1/", "R1", "r1\n", "st1"])
def test_run_dir_rejects_ids_outside_the_grammar(store: Path, bad: str) -> None:
    with pytest.raises(UserError, match="run_list"):
        runstore.run_dir(store, bad)


def test_run_dir_unknown_run_suggests_run_list(store: Path) -> None:
    with pytest.raises(UserError, match=r"no run r7.*run_list"):
        runstore.run_dir(store, "r7")


# --- spec, status, summary ------------------------------------------------------------------


def test_spec_round_trips_and_is_immutable(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    spec = _spec("r1")
    runstore.write_spec(path, spec)
    assert runstore.read_spec(path) == spec
    assert json.loads((path / "spec.json").read_text(encoding="utf-8"))["protocol"] == 1
    before = (path / "spec.json").read_bytes()
    with pytest.raises(FileExistsError):
        runstore.write_spec(path, _spec("r1", model_name="Other"))
    assert (path / "spec.json").read_bytes() == before


def test_status_is_replaced_atomically_on_every_write(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    runstore.write_status(path, _status("r1", RunState.QUEUED))
    running = _status("r1", RunState.RUNNING, t=2.5, progress=0.25, pid=42, started_at=NOW)
    runstore.write_status(path, running)
    assert runstore.read_status(path) == running
    assert sorted(p.name for p in path.iterdir()) == ["status.json"]


def test_status_with_an_error_round_trips(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    failed = _status(
        "r1",
        RunState.FAILED,
        finished_at=LATER,
        error={"code": "timeout", "message": "Too slow.", "log_tail": "step 1\nstep 2\n"},
    )
    runstore.write_status(path, failed)
    assert runstore.read_status(path) == failed


def test_status_rejects_nan_written_through_model_copy(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    bad = _status("r1").model_copy(update={"t": float("nan")})
    with pytest.raises(ValueError):
        runstore.write_status(path, bad)
    assert not (path / "status.json").exists()


@pytest.mark.parametrize("reader", [runstore.read_spec, runstore.read_status])
def test_reading_a_missing_document_raises_protocol_error(store: Path, reader: Any) -> None:
    _, path = runstore.claim_run_id(store)
    with pytest.raises(ProtocolError, match="missing"):
        reader(path)


@pytest.mark.parametrize(
    ("name", "reader"),
    [
        ("spec.json", runstore.read_spec),
        ("status.json", runstore.read_status),
        ("summary.json", runstore.read_summary),
    ],
)
def test_reading_a_corrupt_document_raises_protocol_error(
    store: Path, name: str, reader: Any
) -> None:
    _, path = runstore.claim_run_id(store)
    (path / name).write_text('{"protocol": 1, "run_id": ', encoding="utf-8")
    with pytest.raises(ProtocolError, match=name):
        reader(path)


def test_summary_round_trips_and_is_none_when_absent(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    assert runstore.read_summary(path) is None
    summary = RunSummary(
        run_id="r1",
        state=RunState.FINISHED,
        t_end=10.0,
        n_points=101,
        wall_time_s=0.5,
        columns=["x", "v"],
        variables=[
            VariableSummary(
                name="x", unit="m", min=0.0, max=1.0, final=0.5, min_at=0.0, max_at=3.0
            ),
            VariableSummary(name="v"),
        ],
    )
    runstore.write_summary(path, summary)
    assert runstore.read_summary(path) == summary


# --- result.csv -----------------------------------------------------------------------------


def _array(names: list[str], rows: list[tuple[float, ...]]) -> np.ndarray:
    return np.array(rows, dtype=[(name, np.float64) for name in names])


def test_result_round_trips_float64_exactly(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    rng = np.random.default_rng(1)
    result = _array(["time", "x", "v"], [tuple(row) for row in rng.normal(size=(50, 3)) * 1e3])
    result["time"] = np.linspace(0.0, 1.0, 50) / 3.0
    runstore.write_result(path, result)
    loaded = runstore.read_result(path)
    assert loaded.dtype.names == ("time", "x", "v")
    for name in loaded.dtype.names:
        assert np.array_equal(loaded[name], result[name])


def test_result_file_format(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    runstore.write_result(path, _array(["time", "x"], [(0.0, 0.1), (1.0, 1e-20), (2.0, 3.0)]))
    raw = (path / "result.csv").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == "time,x"
    assert lines[1] == "0,0.10000000000000001"
    assert len(lines) == 4
    assert sorted(p.name for p in path.iterdir()) == ["result.csv"]


def test_result_keeps_duplicate_times_in_file_order(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    result = _array(["time", "x"], [(0.0, 0.0), (1.0, 5.0), (1.0, -5.0), (2.0, -4.0)])
    runstore.write_result(path, result)
    loaded = runstore.read_result(path)
    assert loaded["time"].tolist() == [0.0, 1.0, 1.0, 2.0]
    assert loaded["x"].tolist() == [0.0, 5.0, -5.0, -4.0]


def test_result_non_finite_values_survive_the_csv(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    nan, inf = float("nan"), float("inf")
    runstore.write_result(path, _array(["time", "x"], [(0.0, nan), (1.0, inf), (2.0, -inf)]))
    loaded = runstore.read_result(path)
    assert np.isnan(loaded["x"][0]) and loaded["x"][1] == inf and loaded["x"][2] == -inf


def test_result_columns_select_variables_with_time_first(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    result = _array(["time", "a", "b", "c"], [(0.0, 1.0, 2.0, 3.0), (1.0, 4.0, 5.0, 6.0)])
    runstore.write_result(path, result)
    picked = runstore.read_result(path, ["c", "a", "time", "c"])
    assert picked.dtype.names == ("time", "c", "a")
    assert picked["c"].tolist() == [3.0, 6.0]
    assert picked["a"].tolist() == [1.0, 4.0]
    assert runstore.read_result(path, []).dtype.names == ("time",)


def test_result_names_with_commas_and_non_ascii_survive(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    names = ["time", "m[1,2]", "Δt", 'say "hi"']
    result = _array(names, [(0.0, 1.0, 2.0, 3.0), (1.0, 4.0, 5.0, 6.0)])
    runstore.write_result(path, result)
    loaded = runstore.read_result(path, names[1:])
    assert loaded.dtype.names == tuple(names)
    assert loaded["m[1,2]"].tolist() == [1.0, 4.0]
    assert loaded["Δt"].tolist() == [2.0, 5.0]


def test_result_integer_and_boolean_columns_are_stored_as_numbers(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    result = np.array(
        [(0.0, 1, True), (1.0, 2, False)],
        dtype=[("time", np.float64), ("n", np.int32), ("flag", np.bool_)],
    )
    runstore.write_result(path, result)
    loaded = runstore.read_result(path)
    assert loaded["n"].tolist() == [1.0, 2.0]
    assert loaded["flag"].tolist() == [1.0, 0.0]


def test_result_with_a_single_row_or_no_rows(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    runstore.write_result(path, _array(["time", "x"], [(0.0, 7.0)]))
    assert runstore.read_result(path)["x"].tolist() == [7.0]
    runstore.write_result(path, _array(["time", "x"], []))
    empty = runstore.read_result(path)
    assert empty.dtype.names == ("time", "x") and len(empty) == 0


def test_result_must_start_with_time(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    with pytest.raises(ValueError, match="time"):
        runstore.write_result(path, _array(["x", "time"], [(1.0, 0.0)]))
    assert not (path / "result.csv").exists()


def test_write_result_replaces_a_previous_file_atomically(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    runstore.write_result(path, _array(["time", "x"], [(0.0, 1.0)]))
    runstore.write_result(path, _array(["time", "x"], [(0.0, 2.0), (1.0, 3.0)]))
    assert runstore.read_result(path)["x"].tolist() == [2.0, 3.0]
    assert sorted(p.name for p in path.iterdir()) == ["result.csv"]


def test_read_result_without_a_result_is_a_user_error(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    with pytest.raises(UserError, match="no result yet"):
        runstore.read_result(path)


def test_read_result_unknown_column_lists_close_matches(store: Path) -> None:
    _, path = runstore.claim_run_id(store)
    runstore.write_result(path, _array(["time", "height", "velocity"], [(0.0, 1.0, 2.0)]))
    with pytest.raises(UserError) as error:
        runstore.read_result(path, ["heigth"])
    assert "'heigth'" in str(error.value) and "Close matches: height" in str(error.value)
    with pytest.raises(UserError) as other:
        runstore.read_result(path, ["zzz"])
    assert "Close matches" not in str(other.value)


@pytest.mark.parametrize(
    "content",
    ["", "x,y\n1,2\n", "time,x\n0,1\n1\n", "time,x\n0,abc\n"],
    ids=["empty", "no-time-column", "ragged", "not-a-number"],
)
def test_read_result_of_a_corrupt_file_is_a_protocol_error(store: Path, content: str) -> None:
    _, path = runstore.claim_run_id(store)
    (path / "result.csv").write_text(content, encoding="utf-8")
    with pytest.raises(ProtocolError, match=r"result\.csv"):
        runstore.read_result(path)


# --- index ----------------------------------------------------------------------------------


def test_read_index_of_an_empty_store_is_empty(store: Path) -> None:
    assert runstore.read_index(store) == Index()


def test_read_index_rebuilds_from_specs_and_statuses_when_missing(store: Path) -> None:
    first = _create_run(store)
    second = _create_run(store, RunState.RUNNING, updated_at=LATER)
    runstore.claim_run_id(store)  # r3: claimed, nothing written yet
    index = runstore.read_index(store)
    assert [e.run_id for e in index.runs] == [first, second]
    assert [e.state for e in index.runs] == [RunState.QUEUED, RunState.RUNNING]
    assert index.runs[1].updated_at == LATER
    assert index.runs[0].fmu_name == "Model" and index.runs[0].created_at == NOW
    assert runstore.read_index(store) == index
    assert (store / "index.json").is_file()


def test_status_json_is_authoritative_when_the_index_is_rebuilt(store: Path) -> None:
    run_id = _create_run(store)
    runstore.upsert_index(store, _entry(run_id, RunState.QUEUED))
    path = runstore.run_dir(store, run_id)
    runstore.write_status(path, _status(run_id, RunState.FINISHED, updated_at=LATER))
    assert runstore.read_index(store).runs[0].state is RunState.QUEUED  # the cache is stale
    (store / "index.json").unlink()
    assert runstore.read_index(store).runs[0].state is RunState.FINISHED


@pytest.mark.parametrize("content", ["", "{broken", "[]", '{"protocol": 1, "runs": 5}'])
def test_read_index_rebuilds_a_corrupt_index(store: Path, content: str) -> None:
    run_id = _create_run(store, RunState.FINISHED)
    (store / "index.json").write_text(content, encoding="utf-8")
    index = runstore.read_index(store)
    assert [e.run_id for e in index.runs] == [run_id]
    assert runstore.read_index(store) == index


def test_read_index_never_replaces_an_index_with_another_protocol_version(store: Path) -> None:
    _create_run(store)
    future = '{"protocol": 2, "runs": []}'
    (store / "index.json").write_text(future, encoding="utf-8")
    with pytest.raises(UnsupportedProtocolError, match="unsupported protocol version"):
        runstore.read_index(store)
    with pytest.raises(UnsupportedProtocolError):
        runstore.upsert_index(store, _entry("r9"))
    assert (store / "index.json").read_text(encoding="utf-8") == future


def test_rebuild_does_not_swallow_a_run_with_another_protocol_version(store: Path) -> None:
    run_id = _create_run(store)
    path = runstore.run_dir(store, run_id)
    document = json.loads((path / "status.json").read_text(encoding="utf-8"))
    (path / "status.json").write_text(json.dumps({**document, "protocol": 2}), encoding="utf-8")
    with pytest.raises(UnsupportedProtocolError):
        runstore.read_index(store)
    assert not (store / "index.json").exists()


def test_rebuild_skips_runs_with_damaged_documents(store: Path) -> None:
    good = _create_run(store)
    bad = _create_run(store)
    (runstore.run_dir(store, bad) / "status.json").write_text("{broken", encoding="utf-8")
    assert [e.run_id for e in runstore.read_index(store).runs] == [good]


def test_upsert_inserts_replaces_and_keeps_run_number_order(store: Path) -> None:
    for run_id in ("r10", "r2", "r1"):
        runstore.upsert_index(store, _entry(run_id))
    assert [e.run_id for e in runstore.read_index(store).runs] == ["r1", "r2", "r10"]
    runstore.upsert_index(store, _entry("r2", RunState.FINISHED, name="Changed"))
    runs = runstore.read_index(store).runs
    assert [e.run_id for e in runs] == ["r1", "r2", "r10"]
    assert runs[1].state is RunState.FINISHED and runs[1].fmu_name == "Changed"
    assert json.loads((store / "index.json").read_text(encoding="utf-8"))["protocol"] == 1


def test_upsert_on_a_store_without_an_index_rebuilds_first(store: Path) -> None:
    first = _create_run(store, RunState.FINISHED)
    second = _create_run(store)
    runstore.upsert_index(store, _entry(second, RunState.RUNNING))
    runs = runstore.read_index(store).runs
    assert [(e.run_id, e.state) for e in runs] == [
        (first, RunState.FINISHED),
        (second, RunState.RUNNING),
    ]


def test_upsert_threads_do_not_lose_entries(store: Path) -> None:
    def upsert(offset: int) -> None:
        for number in range(offset, offset + 10):
            runstore.upsert_index(store, _entry(f"r{number}"))

    threads = [threading.Thread(target=upsert, args=(1 + 10 * n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [e.run_id for e in runstore.read_index(store).runs] == [f"r{n}" for n in range(1, 41)]


# --- events ---------------------------------------------------------------------------------


def _event(message: str | None = None, run_id: str = "r1") -> Event:
    return Event(at=NOW, run_id=run_id, type=EventType.STARTED, source="worker", message=message)


def test_append_event_writes_one_compact_line_per_event(store: Path) -> None:
    runstore.append_event(store, _event("first"))
    runstore.append_event(store, _event(None, run_id="r2"))
    raw = (store / "events.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 2
    events = [Event.model_validate_json(line) for line in lines]
    assert [(e.run_id, e.message) for e in events] == [("r1", "first"), ("r2", None)]
    assert json.loads(lines[0])["protocol"] == 1 and ": " not in lines[0]


def test_append_event_with_a_newline_in_the_message_stays_on_one_line(store: Path) -> None:
    runstore.append_event(store, _event("line one\nline two"))
    lines = (store / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert Event.model_validate_json(lines[0]).message == "line one\nline two"


@pytest.mark.parametrize(
    "char", ["x", "é", "€", "\U0001d538", "\x01"], ids=["ascii", "2b", "3b", "4b", "ctl"]
)
def test_append_event_truncates_a_long_message_to_fit_4_kib(store: Path, char: str) -> None:
    runstore.append_event(store, _event(char * 10_000))
    raw = (store / "events.jsonl").read_bytes()
    assert len(raw) <= MAX_EVENT_LINE_BYTES and raw.endswith(b"\n")
    event = Event.model_validate_json(raw.decode("utf-8"))
    assert event.message is not None and event.message.endswith("…")
    assert set(event.message[:-1]) == {char}


def test_append_event_leaves_a_message_that_just_fits_untouched(store: Path) -> None:
    base = len(dump_json(_event("")).encode("utf-8")) + 1  # the line with an empty message
    message = "m" * (MAX_EVENT_LINE_BYTES - base)
    runstore.append_event(store, _event(message))
    raw = (store / "events.jsonl").read_bytes()
    assert len(raw) == MAX_EVENT_LINE_BYTES
    assert Event.model_validate_json(raw.decode("utf-8")).message == message


# --- tail_log -------------------------------------------------------------------------------


def _write_log(path: Path, data: bytes) -> Path:
    path.mkdir(exist_ok=True)
    (path / "log.txt").write_bytes(data)
    return path


def test_tail_log_without_a_log_is_empty(tmp_path: Path) -> None:
    assert runstore.tail_log(tmp_path, 100) == ""
    assert runstore.tail_log(tmp_path / "missing-run", 100) == ""


def test_tail_log_returns_a_short_log_whole(tmp_path: Path) -> None:
    _write_log(tmp_path, b"alpha\nbeta\n")
    assert runstore.tail_log(tmp_path, 100) == "alpha\nbeta\n"
    assert runstore.tail_log(tmp_path, len(b"alpha\nbeta\n")) == "alpha\nbeta\n"
    assert runstore.tail_log(tmp_path, 0) == ""


def test_tail_log_starts_at_a_line_boundary(tmp_path: Path) -> None:
    _write_log(tmp_path, b"alpha\nbeta\ngamma\n")
    assert runstore.tail_log(tmp_path, 9) == "gamma\n"  # cuts inside "beta"
    assert runstore.tail_log(tmp_path, 12) == "beta\ngamma\n"  # window starts on a newline
    assert runstore.tail_log(tmp_path, 11) == "beta\ngamma\n"  # window starts after a newline
    assert runstore.tail_log(tmp_path, 6) == "gamma\n"


def test_tail_log_keeps_the_partial_line_when_nothing_else_would_remain(tmp_path: Path) -> None:
    _write_log(tmp_path, b"head\n" + b"z" * 50 + b"\n")
    assert runstore.tail_log(tmp_path, 20) == "z" * 19 + "\n"
    _write_log(tmp_path, b"z" * 100)
    assert runstore.tail_log(tmp_path, 10) == "z" * 10


def test_tail_log_never_fails_on_a_split_multibyte_character(tmp_path: Path) -> None:
    _write_log(tmp_path, ("Δ" * 40).encode("utf-8"))  # no newline: the raw window is kept
    tail = runstore.tail_log(tmp_path, 7)
    assert tail.endswith("ΔΔ") and "�" in tail
    _write_log(tmp_path, b"ok\n" + b"\xff\xfe broken\n")
    assert "broken" in runstore.tail_log(tmp_path, 100)
