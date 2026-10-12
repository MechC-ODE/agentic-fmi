"""Cross-process tests for the run store: two interpreters share one store directory, as two MCP
servers (or a server and its workers) do.

Collaborators: ``_store_child.py`` (the process that runs the store calls), ``agentic_fmi.store``
(``runstore`` and ``protocol``). Children start together on a file-based gate so their calls
really overlap; each test asserts on the files they left behind.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import MAX_EVENT_LINE_BYTES, Event

CHILD = Path(__file__).with_name("_store_child.py")
PROCESSES = 2
CALLS = 20
TIMEOUT_S = 60.0


def _run_children(store: Path, sync: Path, mode: str, count: int = CALLS) -> list[str]:
    """Start ``PROCESSES`` children, release them together, and return their stdout texts."""
    sync.mkdir()
    children = [
        subprocess.Popen(
            [sys.executable, str(CHILD), str(store), str(sync), mode, str(count), tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in "AB"[:PROCESSES]
    ]
    try:
        deadline = time.monotonic() + TIMEOUT_S
        while len(list(sync.glob("ready-*"))) < PROCESSES:
            if time.monotonic() > deadline or any(c.poll() is not None for c in children):
                raise AssertionError("a child exited or never became ready before the gate opened")
            time.sleep(0.01)
        (sync / "go").touch()
        outputs = [child.communicate(timeout=TIMEOUT_S) for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
    for child, (_, stderr) in zip(children, outputs, strict=True):
        assert child.returncode == 0, stderr
        assert stderr == ""
    return [stdout for stdout, _ in outputs]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    runstore.ensure_store(path)
    return path


def test_two_processes_claim_forty_distinct_run_ids(store: Path, tmp_path: Path) -> None:
    outputs = _run_children(store, tmp_path / "sync", "claim")
    claimed = [line for out in outputs for line in out.split()]
    assert len(claimed) == PROCESSES * CALLS
    assert len(set(claimed)) == PROCESSES * CALLS
    assert sorted(claimed, key=lambda run_id: int(run_id[1:])) == [
        f"r{n}" for n in range(1, PROCESSES * CALLS + 1)
    ]
    assert runstore.list_run_ids(store) == [f"r{n}" for n in range(1, PROCESSES * CALLS + 1)]


def test_each_process_sees_its_own_ids_increase(store: Path, tmp_path: Path) -> None:
    outputs = _run_children(store, tmp_path / "sync", "claim")
    for out in outputs:
        numbers = [int(line[1:]) for line in out.split()]
        assert numbers == sorted(numbers) and len(set(numbers)) == CALLS


def test_concurrent_claim_and_upsert_leave_a_complete_ordered_index(
    store: Path, tmp_path: Path
) -> None:
    _run_children(store, tmp_path / "sync", "claim_index")
    expected = [f"r{n}" for n in range(1, PROCESSES * CALLS + 1)]
    assert [e.run_id for e in runstore.read_index(store).runs] == expected
    assert sorted(p.name for p in store.iterdir()) == [".lock", "index.json", "runs"]


def test_concurrent_appenders_produce_only_whole_lines(store: Path, tmp_path: Path) -> None:
    _run_children(store, tmp_path / "sync", "events", count=100)
    raw = (store / "events.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.split(b"\n")[:-1]
    assert len(lines) == PROCESSES * 100
    assert all(len(line) + 1 <= MAX_EVENT_LINE_BYTES for line in lines)
    events = [Event.model_validate_json(line) for line in lines]
    for tag in "AB"[:PROCESSES]:
        sequences = [
            int(e.message.split(":")[1]) for e in events if e.message and e.message[0] == tag
        ]
        assert sequences == list(range(100))  # every event present, in each writer's own order
