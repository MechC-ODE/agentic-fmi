"""The run-store layout and its reads and writes: run-ID claims, per-run documents, the index,
the event log, results and log tails. Plain functions over ``store_dir``; no caching, no state.

Layout (protocol v1, [docs/protocol.md](../../../docs/protocol.md))::

    <store_dir>/index.json  events.jsonl  .lock  preferences.json
    <store_dir>/runs/<run_id>/spec.json status.json summary.json result.csv log.txt fmu/ plots/
    <store_dir>/runs/<run_id>/spec.json status.json log.txt source/ work/ build.log build.json
                                                                       (a build job, ADR 0008)
    <store_dir>/runs/<run_id>/spec.json status.json summary.json result.csv log.txt work/ plots/
                                                                    (a system run, ADR 0009)
    <store_dir>/runs/<run_id>/live.json commands/ outcomes/     (a live run of either kind,
                                                                 ADR 0010; see store/inbox.py)

Collaborators: ``store/atomic.py`` (atomic writes, appends, the lock), ``store/protocol.py``
(the documents), ``supervisor.py`` and ``runs.py`` on the server side, ``worker.py`` in the
worker. Run IDs follow ``protocol.RUN_ID_PATTERN`` and are claimed with an exclusive
``os.mkdir`` ([ADR 0007](../../../docs/adr/0007-run-store-location-ids-and-supervision.md)).
"""

from __future__ import annotations

import csv
import difflib
import io
import os
import re
import warnings
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from agentic_fmi.errors import ProtocolError, UserError
from agentic_fmi.store.atomic import (
    UnsupportedProtocolError,
    append_line,
    atomic_open,
    dump_json,
    locked,
    read_json,
    write_json,
)
from agentic_fmi.store.protocol import (
    MAX_EVENT_LINE_BYTES,
    RUN_ID_PATTERN,
    BuildEvent,
    BuildRecord,
    BuildSpec,
    Event,
    Index,
    IndexEntry,
    LiveSnapshot,
    Preferences,
    RunSpec,
    RunStatus,
    RunSummary,
    SystemSpec,
)

RUNS_DIR = "runs"
INDEX_FILE = "index.json"
EVENTS_FILE = "events.jsonl"
LOCK_FILE = ".lock"
SPEC_FILE = "spec.json"
STATUS_FILE = "status.json"
SUMMARY_FILE = "summary.json"
RESULT_FILE = "result.csv"
LOG_FILE = "log.txt"
FMU_DIR = "fmu"
"""Where the worker extracts the FMU; removed in the worker's ``finally``."""
PLOTS_DIR = "plots"
SOURCE_DIR = "source"
"""Build jobs: the private copy of the hashed source bytes, ``source/<file name>`` (ADR 0008)."""
WORK_DIR = "work"
"""Build jobs: the build tool's cwd and ``TMPDIR``; system runs: the private FMU copies and
OMSimulator's ``TMPDIR``. Removed in the worker's ``finally``."""
BUILD_LOG_FILE = "build.log"
"""Build jobs: the build tool's stdout and stderr (``log.txt`` keeps the worker's own output)."""
BUILD_FILE = "build.json"
"""Build jobs: the :class:`BuildRecord` of a successful build (also next to the FMU)."""
SYSTEM_FMUS_DIR = "fmus"
"""System runs: ``work/fmus/<component>.fmu``, the private FMU copies the worker hashed and
OMSimulator loads (ADR 0009)."""
WORK_TMP_DIR = "tmp"
"""``work/tmp``: the ``TMPDIR`` of a build tool or of OMSimulator."""
LIVE_FILE = "live.json"
"""Live runs: the :class:`LiveSnapshot` the worker's heartbeat thread replaces (ADR 0010)."""
PREFERENCES_FILE = "preferences.json"
"""The :class:`Preferences` in the store root (the response style); the server writes it."""
SCRATCH_DIRS = (FMU_DIR, WORK_DIR)
"""Per-run directories removed when the worker exits and by the server after a takeover."""

_RUN_ID = re.compile(RUN_ID_PATTERN, re.ASCII)
"""ASCII digits only: ``\\d`` would also accept other Unicode digits in a directory name."""
_TIME = "time"
_MAX_CLOSE_MATCHES = 3
_ELLIPSIS = "…"


def ensure_store(store_dir: Path) -> None:
    """Create ``store_dir`` and ``runs/`` if missing. Idempotent and safe under concurrency."""
    (store_dir / RUNS_DIR).mkdir(parents=True, exist_ok=True)


def _run_number(run_id: str) -> int:
    return int(run_id[1:])


def claim_run_id(store_dir: Path) -> tuple[str, Path]:
    """Claim the next free run ID and return it with its new, empty run directory.

    Start at one more than the highest existing ``r<n>`` under ``runs/`` and try
    ``os.mkdir(runs/r<n>)``; on ``FileExistsError`` increment ``n`` and retry. ``mkdir`` is
    atomic, so IDs stay unique across threads and across MCP servers sharing the store
    (acceptance: 2 servers x 20 concurrent claims give 40 distinct IDs). Entries that don't
    match the grammar are ignored.
    """
    ensure_store(store_dir)
    ids = list_run_ids(store_dir)
    number = (_run_number(ids[-1]) if ids else 0) + 1
    while True:
        run_id = f"r{number}"
        path = store_dir / RUNS_DIR / run_id
        try:
            os.mkdir(path)
        except FileExistsError:
            number += 1
        else:
            return run_id, path


def list_run_ids(store_dir: Path) -> list[str]:
    """Run IDs present under ``runs/``, in ascending numeric order (``r2`` before ``r10``)."""
    try:
        with os.scandir(store_dir / RUNS_DIR) as entries:
            ids = [e.name for e in entries if _RUN_ID.fullmatch(e.name) and e.is_dir()]
    except FileNotFoundError:
        return []
    return sorted(ids, key=_run_number)


def run_dir(store_dir: Path, run_id: str) -> Path:
    """The directory of an existing run.

    Raises:
        UserError: ``run_id`` doesn't match the grammar, or no such run exists. The message
            suggests ``run_list``.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise UserError(
            f"{run_id[:40]!r} is not a run ID; run IDs look like r1 or r12. "
            "Call run_list to see the runs."
        )
    path = store_dir / RUNS_DIR / run_id
    if not path.is_dir():
        raise UserError(f"There is no run {run_id}. Call run_list to see the runs.")
    return path


JobSpec = RunSpec | BuildSpec | SystemSpec
"""Any ``spec.json``: a simulation run, a build job or a system run (dispatch on ``kind``)."""


def write_spec(run_path: Path, spec: JobSpec) -> None:
    """Write ``spec.json`` once. Raises ``FileExistsError`` if it already exists (immutable)."""
    path = run_path / SPEC_FILE
    if path.exists():
        raise FileExistsError(f"{path} already exists; a run's spec is immutable")
    write_json(path, spec)


def read_spec(run_path: Path) -> RunSpec:
    """Read ``spec.json``. Raises ``ProtocolError`` if missing or invalid."""
    return read_json(run_path / SPEC_FILE, RunSpec)


class _SpecKind(BaseModel):
    """Just the ``kind`` of a ``spec.json`` (absent in no v1 file, but ``run`` if it were)."""

    model_config = ConfigDict(extra="ignore")
    kind: str = "run"


def read_job_spec(run_path: Path) -> JobSpec:
    """Read ``spec.json`` as a :class:`RunSpec`, :class:`BuildSpec` or :class:`SystemSpec`, by
    its ``kind``.

    The reader for every caller that may meet a build job (the worker, ``runs._view``, the
    index rebuild, ``Supervisor._sync_index``); ``read_spec`` stays for the callers that need a
    simulation run, and reads a run's spec here too.

    Raises:
        ProtocolError: missing, invalid, an unknown ``kind``, or another protocol version.
    """
    path = run_path / SPEC_FILE
    kind = read_json(path, _SpecKind).kind
    if kind == "build":
        return read_json(path, BuildSpec)
    if kind == "system":
        return read_json(path, SystemSpec)
    if kind == "run":
        return read_spec(run_path)
    raise ProtocolError(
        f"{path} has an unknown kind {kind[:40]!r}; expected 'run', 'build' or 'system'."
    )


def index_entry(spec: JobSpec, status: RunStatus) -> IndexEntry:
    """The ``index.json`` entry of a run, build job or system run: the one place that maps a
    spec to an entry (``runs``, the supervisor and the index rebuild use it). A build job's
    ``fmu_name`` is its build name and ``fmu_path`` its absolute output FMU path; a system
    run's are the system's name and the system file's path."""
    if isinstance(spec, BuildSpec):
        name, fmu_path = spec.name, str(Path(spec.workspace) / spec.fmu_path)
    elif isinstance(spec, SystemSpec):
        name, fmu_path = spec.name, spec.system.path
    else:
        name, fmu_path = spec.fmu.model_name, spec.fmu.path
    return IndexEntry(
        run_id=spec.run_id,
        kind=spec.kind,
        state=status.state,
        fmu_name=name,
        fmu_path=fmu_path,
        created_at=spec.created_at,
        updated_at=status.updated_at,
    )


def write_build_source(run_path: Path, file_name: str, data: bytes) -> Path:
    """Write the build job's private source copy ``source/<file_name>`` atomically (server,
    before ``spec.json``) and return its path."""
    directory = run_path / SOURCE_DIR
    directory.mkdir(exist_ok=True)
    path = directory / file_name
    with atomic_open(path) as handle:
        handle.write(data)
    return path


def write_build_record(directory: Path, record: BuildRecord) -> None:
    """Atomically write ``directory/build.json`` (a run directory or ``generated/<name>/``)."""
    write_json(directory / BUILD_FILE, record)


def read_build_record(directory: Path) -> BuildRecord | None:
    """Read ``directory/build.json``; ``None`` if it doesn't exist.

    Raises:
        ProtocolError: it exists but is invalid or from another protocol version (callers that
            decide whether ``generated/<name>/`` is ours treat that as "not ours").
    """
    path = directory / BUILD_FILE
    if not path.exists():
        return None
    return read_json(path, BuildRecord)


def write_status(run_path: Path, status: RunStatus) -> None:
    """Atomically replace ``status.json``. Callers must own the file (single-writer rule)."""
    write_json(run_path / STATUS_FILE, status)


def read_status(run_path: Path) -> RunStatus:
    """Read ``status.json``. Raises ``ProtocolError`` if missing or invalid."""
    return read_json(run_path / STATUS_FILE, RunStatus)


def write_live(run_path: Path, snapshot: LiveSnapshot) -> None:
    """Atomically replace ``live.json`` (the worker's heartbeat thread only, ADR 0010). The
    caller keeps it under ``Limits.live_max_bytes``."""
    write_json(run_path / LIVE_FILE, snapshot)


def read_live(run_path: Path) -> LiveSnapshot | None:
    """Read ``live.json``, or ``None`` if the run has none (a batch run, or a live run before
    its first sample).

    Raises:
        ProtocolError: it exists but is invalid or from another protocol version.
    """
    path = run_path / LIVE_FILE
    if not path.exists():
        return None
    return read_json(path, LiveSnapshot)


def write_preferences(store_dir: Path, preferences: Preferences) -> None:
    """Atomically replace ``preferences.json`` (the MCP server's ``response_style`` tool only)."""
    ensure_store(store_dir)
    write_json(store_dir / PREFERENCES_FILE, preferences)


def read_preferences(store_dir: Path) -> Preferences:
    """Read ``preferences.json``. A missing, unreadable or invalid file (or another protocol
    version) means the defaults: a preference must never fail a tool."""
    try:
        return read_json(store_dir / PREFERENCES_FILE, Preferences)
    except ProtocolError:
        return Preferences()


def write_summary(run_path: Path, summary: RunSummary) -> None:
    """Atomically write ``summary.json`` (worker only)."""
    write_json(run_path / SUMMARY_FILE, summary)


def read_summary(run_path: Path) -> RunSummary | None:
    """Read ``summary.json``, or ``None`` if the run produced no result."""
    path = run_path / SUMMARY_FILE
    if not path.exists():
        return None
    return read_json(path, RunSummary)


def _csv_header(names: Sequence[str]) -> str:
    """One CSV row of column names, quoted only where needed (``a[1,2]`` contains a comma)."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(names)
    return buffer.getvalue()


def write_result(run_path: Path, result: np.ndarray) -> None:
    """Write ``result.csv`` atomically (temporary file plus ``os.replace``).

    ``result`` is FMPy's structured array with ``time`` first. Header row of plain column
    names, ``%.17g`` floats, LF. Worker only.

    Every column is stored as a float (Boolean and Integer columns become 0, 1 and whole
    numbers); a non-finite value is written as ``nan`` or ``inf`` and read back unchanged.
    """
    names = result.dtype.names
    if not names or names[0] != _TIME:
        raise ValueError("result must be a structured array whose first field is 'time'")
    columns = np.column_stack([np.asarray(result[name], dtype=np.float64) for name in names])
    with atomic_open(run_path / RESULT_FILE) as handle:
        np.savetxt(
            handle,
            columns,
            fmt="%.17g",
            delimiter=",",
            newline="\n",
            header=_csv_header(names),
            comments="",
            encoding="utf-8",
        )


def _unknown_column(name: str, available: Sequence[str]) -> UserError:
    close = difflib.get_close_matches(name, available, n=_MAX_CLOSE_MATCHES)
    hint = f" Close matches: {', '.join(close)}." if close else ""
    return UserError(
        f"Unknown variable {name!r} in the result.{hint} "
        "Ask for variables the run recorded (its outputs; see run_status)."
    )


def _read_header(handle: io.TextIOBase, path: Path) -> list[str]:
    rows = list(csv.reader([handle.readline()]))
    header = rows[0] if rows else []
    if not header or header[0] != _TIME:
        raise ProtocolError(f"{path} is invalid: the header must start with the 'time' column.")
    return header


def read_result(run_path: Path, columns: Sequence[str] | None = None) -> np.ndarray:
    """Read ``result.csv`` as a structured float array with ``time`` first.

    ``columns`` selects variables (``time`` is always included); ``None`` means every column.
    Duplicate times (events) are kept in file order.

    Raises:
        UserError: the run has no result yet, or a column is unknown (with close matches).
        ProtocolError: ``result.csv`` exists but cannot be parsed.
    """
    path = run_path / RESULT_FILE
    try:
        handle = path.open(encoding="utf-8", newline="")
    except FileNotFoundError:
        raise UserError(
            "This run has no result yet. Check run_status; only finished or stopped runs "
            "have results."
        ) from None
    with handle:
        header = _read_header(handle, path)
        chosen = header[1:] if columns is None else columns
        wanted = [_TIME, *(c for c in dict.fromkeys(chosen) if c != _TIME)]
        for name in wanted:
            if name not in header:
                raise _unknown_column(name, header[1:])
        indices = [header.index(name) for name in wanted]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)  # a header-only file is no error
                values = np.loadtxt(handle, delimiter=",", usecols=indices, dtype=np.float64)
        except (ValueError, IndexError) as error:
            raise ProtocolError(f"{path} is invalid: {error}.") from error
    table = values.reshape(-1, len(indices))
    result = np.empty(len(table), dtype=[(name, np.float64) for name in wanted])
    for position, name in enumerate(wanted):
        result[name] = table[:, position]
    return result


def _rebuild_index(store_dir: Path) -> Index:
    """The index as derived from ``runs/*/spec.json`` and ``status.json``. Runs whose documents
    are missing or unreadable are skipped; an unsupported protocol version is not."""
    entries: list[IndexEntry] = []
    for run_id in list_run_ids(store_dir):
        path = store_dir / RUNS_DIR / run_id
        try:
            spec, status = read_job_spec(path), read_status(path)
        except UnsupportedProtocolError:
            raise
        except ProtocolError:
            continue  # claimed but not yet written, or damaged; a later upsert adds it
        entries.append(index_entry(spec, status))
    return Index(runs=entries)


def _index_or_rebuild(store_dir: Path) -> Index:
    """Read ``index.json``, rebuilding and writing it if missing or corrupt. Caller holds the
    lock. A file with another protocol version is intact, so it raises instead of being replaced."""
    path = store_dir / INDEX_FILE
    try:
        return read_json(path, Index)
    except UnsupportedProtocolError:
        raise
    except ProtocolError:
        index = _rebuild_index(store_dir)
        write_json(path, index)
        return index


def read_index(store_dir: Path) -> Index:
    """Read ``index.json``; if it is missing or unreadable, rebuild it from ``runs/*/spec.json``
    and ``status.json`` (under the lock) and return the rebuilt index."""
    try:
        return read_json(store_dir / INDEX_FILE, Index)
    except UnsupportedProtocolError:
        raise
    except ProtocolError:
        ensure_store(store_dir)
        with locked(store_dir / LOCK_FILE):
            return _index_or_rebuild(store_dir)


def upsert_index(store_dir: Path, entry: IndexEntry) -> None:
    """Insert or replace ``entry`` in ``index.json`` under ``locked(<store>/.lock)``.

    Read-modify-write; entries stay in ascending run number. MCP servers only.
    """
    ensure_store(store_dir)
    with locked(store_dir / LOCK_FILE):
        current = _index_or_rebuild(store_dir)
        runs = [e for e in current.runs if e.run_id != entry.run_id] + [entry]
        runs.sort(key=lambda e: _run_number(e.run_id))
        write_json(store_dir / INDEX_FILE, Index(runs=runs))


def _with_message(event: Event | BuildEvent, message: str | None) -> str:
    return dump_json(event.model_copy(update={"message": message}))


def _fits(line: str) -> bool:
    return len(line.encode("utf-8")) + 1 <= MAX_EVENT_LINE_BYTES  # the newline counts


def _event_line(event: Event | BuildEvent) -> str:
    """``event`` as one compact line; a message too long for the 4 KiB limit is cut to the
    longest prefix that fits, followed by an ellipsis."""
    line = dump_json(event)
    if _fits(line) or not event.message:
        return line
    text = event.message[:MAX_EVENT_LINE_BYTES]  # a character takes at least one byte
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _fits(_with_message(event, text[:middle] + _ELLIPSIS)):
            low = middle
        else:
            high = middle - 1
    return _with_message(event, text[:low] + _ELLIPSIS if low else None)


def append_event(store_dir: Path, event: Event | BuildEvent) -> None:
    """Append ``event`` to ``events.jsonl`` as one compact line (at most 4 KiB; a longer
    ``message`` is truncated to fit). Server and workers."""
    append_line(store_dir / EVENTS_FILE, _event_line(event))


def tail_log(run_path: Path, max_bytes: int) -> str:
    """The last ``max_bytes`` of ``log.txt`` decoded as UTF-8 with replacement, starting at a
    line boundary when possible. Empty string if there is no log."""
    if max_bytes <= 0:
        return ""
    try:
        with (run_path / LOG_FILE).open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(size - max_bytes, 0)
            # One byte before the window tells whether the window begins on a line boundary.
            handle.seek(max(start - 1, 0))
            data = handle.read()
    except FileNotFoundError:
        return ""
    if start > 0:
        data = _from_line_boundary(data)
    return data.decode("utf-8", errors="replace")


def _from_line_boundary(data: bytes) -> bytes:
    """``data`` is the window plus the byte before it: drop that byte and, unless it was a
    newline, the partial first line. Keeps the partial line if nothing else would remain."""
    window = data[1:]
    if data[:1] == b"\n":
        return window
    newline = window.find(b"\n")
    rest = window[newline + 1 :] if newline >= 0 else b""
    return rest or window
