"""Tests for ``store/atomic.py``: atomic JSON replace, validated reads, single-line appends and
the advisory lock.

Collaborators: ``agentic_fmi.store.atomic``, the protocol models in ``agentic_fmi.store.protocol``
(the documents written and read back), and ``agentic_fmi.errors.ProtocolError``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.errors import ProtocolError
from agentic_fmi.store.atomic import (
    UnsupportedProtocolError,
    append_line,
    locked,
    read_json,
    write_json,
)
from agentic_fmi.store.protocol import (
    Event,
    EventType,
    Index,
    IndexEntry,
    RunState,
    RunStatus,
)

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _status(run_id: str = "r1", **changes: Any) -> RunStatus:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "state": RunState.QUEUED,
        "t_start": 0.0,
        "t_stop": 10.0,
        "updated_at": NOW,
    }
    return RunStatus(**{**fields, **changes})


def _entry(number: int) -> IndexEntry:
    return IndexEntry(
        run_id=f"r{number}",
        state=RunState.FINISHED,
        fmu_name="Model",
        fmu_path="/ws/Model.fmu",
        created_at=NOW,
        updated_at=NOW,
    )


# --- write_json -----------------------------------------------------------------------------


def test_write_json_is_compact_utf8_with_protocol_and_newline(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    write_json(path, _status(t=1.5))
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    text = raw.decode("utf-8")
    assert '"protocol":1' in text
    assert ": " not in text and ", " not in text
    assert json.loads(text)["updated_at"] == "2026-10-09T12:00:00Z"


@pytest.mark.parametrize(
    "model",
    [
        _status(),
        Index(runs=[_entry(1), _entry(2)]),
        Event(at=NOW, run_id="r1", type=EventType.CREATED, source="server", message="hi"),
    ],
    ids=["status", "index", "event"],
)
def test_every_document_carries_protocol_1_and_round_trips(tmp_path: Path, model: Any) -> None:
    path = tmp_path / "doc.json"
    write_json(path, model)
    assert json.loads(path.read_text(encoding="utf-8"))["protocol"] == 1
    assert read_json(path, type(model)) == model


def test_write_json_keeps_non_ascii_text(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    event = Event(at=NOW, run_id="r1", type=EventType.FAILED, source="worker", message="Größe Δt")
    write_json(path, event)
    assert "Größe Δt" in path.read_text(encoding="utf-8")
    assert read_json(path, Event) == event


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_write_json_rejects_non_finite_floats_and_keeps_old_file(
    tmp_path: Path, bad: float
) -> None:
    path = tmp_path / "status.json"
    write_json(path, _status(t=1.0))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_json(path, _status().model_copy(update={"t": bad}))  # copy skips validation
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["status.json"]


def test_write_json_rejects_an_invalid_run_id_that_skipped_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_json(tmp_path / "status.json", _status().model_copy(update={"run_id": "r01"}))
    assert list(tmp_path.iterdir()) == []


def test_write_json_missing_directory_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write_json(tmp_path / "missing" / "status.json", _status())


def test_write_json_uses_same_dir_temp_file_and_fsyncs_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.json"
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    sources: list[Path] = []

    def fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace(src: Any, dst: Any) -> None:
        calls.append("replace")
        sources.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    write_json(path, _status())
    assert calls == ["fsync", "replace"]
    assert sources[0].parent == tmp_path
    assert sources[0].name == f"status.json.tmp-{os.getpid()}-{threading.get_ident()}"
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_failed_replace_removes_the_temp_file_and_keeps_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.json"
    write_json(path, _status(t=1.0))
    before = path.read_bytes()

    def broken_replace(src: Any, dst: Any) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk on fire"):
        write_json(path, _status(t=2.0))
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_readers_never_see_a_partial_file_while_it_is_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    small, large = Index(runs=[_entry(1)]), Index(runs=[_entry(n) for n in range(1, 400)])
    write_json(path, small)
    stop = threading.Event()
    failures: list[Exception] = []

    def writer() -> None:
        while not stop.is_set():
            write_json(path, large)
            write_json(path, small)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(300):
            try:
                assert len(read_json(path, Index).runs) in (1, 399)
            except Exception as error:
                failures.append(error)
    finally:
        stop.set()
        thread.join()
    assert failures == []


# --- read_json ------------------------------------------------------------------------------


def test_read_json_missing_file_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError, match=r"status\.json.*missing"):
        read_json(tmp_path / "status.json", RunStatus)


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (b"{not json", "not valid JSON"),
        (b"", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (b"[1, 2]", "not a JSON object"),
        (b'{"protocol": 1, "t_start": NaN}', "not valid JSON"),
    ],
    ids=["garbage", "empty", "bad-utf8", "array", "nan-constant"],
)
def test_read_json_corrupt_file_raises_protocol_error(
    tmp_path: Path, content: bytes, fragment: str
) -> None:
    path = tmp_path / "status.json"
    path.write_bytes(content)
    with pytest.raises(ProtocolError, match=fragment) as error:
        read_json(path, RunStatus)
    assert "status.json" in str(error.value)


def test_read_json_invalid_document_names_the_field_not_the_value(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"protocol": 1, "run_id": "SECRET-ID", "state": "queued"}))
    with pytest.raises(ProtocolError) as error:
        read_json(path, RunStatus)
    message = str(error.value)
    assert "status.json" in message and "run_id" in message and "SECRET-ID" not in message


@pytest.mark.parametrize("protocol", [2, 0, "1", None, True], ids=repr)
def test_read_json_other_protocol_versions_are_unsupported(tmp_path: Path, protocol: Any) -> None:
    path = tmp_path / "status.json"
    document = json.loads(_status().model_dump_json())
    path.write_text(json.dumps({**document, "protocol": protocol}))
    with pytest.raises(UnsupportedProtocolError, match="unsupported protocol version") as error:
        read_json(path, RunStatus)
    assert isinstance(error.value, ProtocolError)
    assert "status.json" in str(error.value)


def test_read_json_missing_protocol_key_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    document = json.loads(_status().model_dump_json())
    del document["protocol"]
    path.write_text(json.dumps(document))
    with pytest.raises(UnsupportedProtocolError, match="unsupported protocol version"):
        read_json(path, RunStatus)


def test_read_json_ignores_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    document = json.loads(_status().model_dump_json())
    path.write_text(json.dumps({**document, "added_in_a_later_minor": {"x": 1}}))
    assert read_json(path, RunStatus) == _status()


# --- append_line ----------------------------------------------------------------------------


def test_append_line_adds_one_newline_terminated_line_per_call(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    append_line(path, "first")
    append_line(path, "second Δ")
    assert path.read_bytes() == "first\nsecond Δ\n".encode()


def test_append_line_rejects_embedded_newlines(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="newline"):
        append_line(tmp_path / "events.jsonl", "a\nb")
    assert not (tmp_path / "events.jsonl").exists()


def test_append_line_limit_includes_the_newline(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    append_line(path, "x" * 4095)  # 4096 bytes with the newline
    with pytest.raises(ValueError, match="4097 bytes"):
        append_line(path, "x" * 4096)
    with pytest.raises(ValueError, match="limit is 10"):
        append_line(path, "y" * 10, max_bytes=10)
    assert len(path.read_bytes()) == 4096


def test_append_line_is_one_write_on_an_append_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []
    writes: list[bytes] = []
    real_open, real_write = os.open, os.write

    def spy_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(flags)
        return real_open(path, flags, *args, **kwargs)

    def spy_write(fd: int, data: Any) -> int:
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "write", spy_write)
    append_line(tmp_path / "events.jsonl", "payload")
    assert len(opened) == 1 and opened[0] & os.O_APPEND
    assert writes == [b"payload\n"]


# --- locked ---------------------------------------------------------------------------------


def test_locked_creates_the_lock_file(tmp_path: Path) -> None:
    lock = tmp_path / ".lock"
    with locked(lock):
        assert lock.exists()


def test_locked_excludes_a_second_holder_until_released(tmp_path: Path) -> None:
    lock = tmp_path / ".lock"
    first_holds, release_first = threading.Event(), threading.Event()
    second_got_it = threading.Event()

    def first() -> None:
        with locked(lock):
            first_holds.set()
            release_first.wait(timeout=10)

    def second() -> None:
        with locked(lock):
            second_got_it.set()

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_holds.wait(timeout=10)
    t2 = threading.Thread(target=second)
    t2.start()
    assert not second_got_it.wait(timeout=0.3)
    release_first.set()
    assert second_got_it.wait(timeout=10)
    t1.join()
    t2.join()


def test_locked_serialises_read_modify_write_across_threads(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    counter.write_text("0")

    def bump() -> None:
        for _ in range(25):
            with locked(tmp_path / ".lock"):
                value = int(counter.read_text())
                time.sleep(0.0005)  # widen the race window
                counter.write_text(str(value + 1))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counter.read_text() == "100"


def test_locked_releases_after_an_exception(tmp_path: Path) -> None:
    lock = tmp_path / ".lock"
    with pytest.raises(RuntimeError), locked(lock):
        raise RuntimeError("boom")
    acquired = threading.Event()

    def again() -> None:
        with locked(lock):
            acquired.set()

    thread = threading.Thread(target=again)
    thread.start()
    assert acquired.wait(timeout=10)
    thread.join()
