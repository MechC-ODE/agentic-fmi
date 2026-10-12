"""A child process for ``test_runstore_concurrency.py``: it hammers one store from its own
interpreter, standing in for a second MCP server or a worker.

Usage: ``python _store_child.py <store_dir> <sync_dir> <mode> <count> <tag>``. The child imports
everything, touches ``<sync_dir>/ready-<tag>``, waits for ``<sync_dir>/go`` so both children start
together, then runs ``mode`` ``count`` times:

- ``claim``: claim a run ID and print it.
- ``claim_index``: claim a run ID, upsert it into the index and print it.
- ``events``: append an event with a 3000-character message carrying ``<tag>`` and the sequence.

Collaborators: ``agentic_fmi.store.runstore``; it is started by ``test_runstore_concurrency.py``.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import Event, EventType, IndexEntry, RunState

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _entry(run_id: str) -> IndexEntry:
    return IndexEntry(
        run_id=run_id,
        state=RunState.QUEUED,
        fmu_name="Model",
        fmu_path="/ws/Model.fmu",
        created_at=NOW,
        updated_at=NOW,
    )


def _event(tag: str, sequence: int) -> Event:
    message = f"{tag}:{sequence}:".ljust(3000, tag[0])
    return Event(at=NOW, run_id="r1", type=EventType.STARTED, source="worker", message=message)


def main(store: Path, sync: Path, mode: str, count: int, tag: str) -> None:
    (sync / f"ready-{tag}").touch()
    while not (sync / "go").exists():
        time.sleep(0.001)
    for sequence in range(count):
        if mode == "events":
            runstore.append_event(store, _event(tag, sequence))
            continue
        run_id, _ = runstore.claim_run_id(store)
        if mode == "claim_index":
            runstore.upsert_index(store, _entry(run_id))
        print(run_id, flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5])
