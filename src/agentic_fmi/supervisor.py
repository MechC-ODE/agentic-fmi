"""Server-side worker supervision: spawn, liveness, reconciliation, stop, kill-and-reap.

Rules (ADR 0002, ADR 0007):
- Spawn with ``[sys.executable, "-m", "agentic_fmi", "worker", run_dir]``,
  ``start_new_session=True`` (the worker's pid is its process-group id), stdin from
  ``/dev/null``, stdout and stderr appended to ``log.txt``, ``close_fds=True``.
- A pid is "our worker" only if ``/proc/<pid>/stat`` exists and its start time equals
  ``status.pid_start_time``; a zombie (state ``Z``) or dead (``X``) process isn't alive.
- The server writes a terminal state to ``status.json`` only after the worker is confirmed
  gone: ``killpg(pid, SIGKILL)``, then poll. Our own child is gone only once its ``Popen``
  handle has reaped it (a zombie leader can't be reaped while its other threads are still
  exiting); a process we didn't spawn is gone once ``/proc/<pid>`` is gone or a zombie (only
  its parent or init can reap it). Only a live process with the recorded start time is ever
  signalled, and never this process or its group.
- Takeover: ``running`` (or ``paused``: a paused worker keeps beating) with a dead pid
  or a heartbeat older than ``Timing.heartbeat_stale_s`` gives ``failed{worker_lost}`` plus the
  log tail. :func:`heartbeat_age` measures the age with ``time.monotonic() -
  status.heartbeat_mono`` when the field is present (CLOCK_MONOTONIC is shared by the processes
  of one machine and doesn't jump with the wall clock, so a host waking from sleep kills no
  healthy worker) and falls back to ``heartbeat_at`` for files without it; ``queued`` older than
  ``Timing.spawn_timeout_s``, or whose spawned child already exited, gives
  ``failed{worker_lost, "worker never started"}``.
- Every server-side terminal write is a compare-and-set under the store lock: it writes only
  if ``status.json`` is still in a state the decision allows, so a worker's own last word, its
  ``queued`` -> ``running`` and a second server's takeover are never overwritten. The scratch
  directories (``fmu/``, a build job's ``work/``) are removed only after that write, or once
  the worker is confirmed dead. Events use the status's ``run_id``; the index entry is brought
  in line with ``status.json`` on every :meth:`Supervisor.reconcile`.

No background thread: the MCP tools call :meth:`Supervisor.reconcile` on demand
(``run_status``, ``run_list``) and the lifespan calls :meth:`Supervisor.reconcile_all` once at
startup. Every call also reaps this server's exited children and forgets those whose run ended.
:meth:`Supervisor.peek_active_run_ids` counts live runs without reconciling (nothing is written or
signalled), for read-only reports. :attr:`Supervisor.admission` is the per-server lock that
``runs.start_run`` holds while it checks capacity and claims a run ID.

Collaborators: ``store/runstore.py``, ``store/atomic.py`` (the store lock),
``store/protocol.py``, ``config.Config``; ``runs.py`` and ``mcp/server.py`` (through the
lifespan state) on the server side; ``worker.py`` imports :func:`proc_stat` for its own start
time.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from agentic_fmi.config import Config
from agentic_fmi.errors import ProtocolError, UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import locked
from agentic_fmi.store.protocol import (
    TERMINAL_STATES,
    ErrorCode,
    Event,
    EventType,
    RunError,
    RunState,
    RunStatus,
    utc_now,
)

WORKER_COMMAND: Final = (sys.executable, "-m", "agentic_fmi", "worker")
"""The worker command line, without the run directory."""

DEAD_STATES: Final = frozenset({"Z", "X", "x"})
"""``/proc`` states of a process that has exited (zombie or dead) and runs no more code."""

ACTIVE_STATES: Final = frozenset(RunState) - TERMINAL_STATES
"""The states a server-side terminal write may replace once the worker is confirmed dead."""

CONFIRM_POLL_S: Final = 0.01
"""How often :func:`kill_and_reap` checks whether a killed process has gone."""

Target = tuple[int, int | None]
"""A worker's identity: pid and ``/proc`` start time (``None`` if unknown)."""

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcStat:
    """The fields of ``/proc/<pid>/stat`` that supervision needs."""

    state: str
    """One letter: ``R``, ``S``, ``D``, ``T`` (stopped, e.g. SIGSTOP), ``Z`` (zombie), ``X``."""
    start_time: int
    """Field 22: start time in clock ticks since boot."""


def proc_stat(pid: int) -> ProcStat | None:
    """Parse ``/proc/<pid>/stat`` (split after the last ``)``, since the command name may contain
    spaces). ``None`` if the process doesn't exist."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):  # ESRCH: it exited while being read
        return None
    fields = text[text.rindex(")") + 1 :].split()  # fields 3, 4, ...: state is field 3
    return ProcStat(state=fields[0], start_time=int(fields[19]))


def is_alive(pid: int, start_time: int | None) -> bool:
    """True if ``pid`` exists, isn't a zombie or dead, and (when given) has ``start_time``."""
    stat = proc_stat(pid)
    return stat is not None and stat.state not in DEAD_STATES and _same(stat, start_time)


def kill_and_reap(
    pid: int,
    start_time: int | None,
    confirm_s: float,
    child: subprocess.Popen[Any] | None = None,
) -> bool:
    """SIGKILL the worker's process group, reap it if it is our child, and confirm it is gone.

    ``child`` is our ``Popen`` handle for ``pid`` if we spawned it: the process is then gone
    only once the handle has reaped it. Without one a zombie counts as gone, since only its
    parent or init can reap it. Does nothing to a pid whose start time doesn't match (pid
    reuse), or to a child already reaped, and returns True. Returns False if the process is
    still present after ``confirm_s``. A zombie isn't signalled. This process and its own group
    are never signalled, so a status naming the server itself is never confirmed dead.
    ``start_time=None`` skips the identity check: pass it only for an unreaped child of ours
    (or without ``/proc``).
    """
    if child is not None and child.poll() is not None:
        return True
    stat = proc_stat(pid)
    if stat is None or not _same(stat, start_time):
        return True
    if stat.state not in DEAD_STATES:
        _signal_group(pid, signal.SIGKILL)
    deadline = time.monotonic() + confirm_s
    while not _gone(pid, start_time, child):
        if time.monotonic() >= deadline:
            return False
        time.sleep(CONFIRM_POLL_S)
    return True


def _same(stat: ProcStat, start_time: int | None) -> bool:
    return start_time is None or stat.start_time == start_time


def _gone(pid: int, start_time: int | None, child: subprocess.Popen[Any] | None) -> bool:
    """True once our ``child`` is reaped (polling reaps it as soon as the kernel allows: a
    zombie leader waits for its last thread). Without a handle: once ``pid`` is no longer the
    process that started at ``start_time``, or is a zombie or dead."""
    if child is not None:
        return child.poll() is not None
    stat = proc_stat(pid)
    return stat is None or not _same(stat, start_time) or stat.state in DEAD_STATES


def _signal_group(pid: int, signum: signal.Signals) -> None:
    """Send ``signum`` to the process group led by ``pid``; to ``pid`` alone if it leads no
    group (a worker run by hand). Never to pid 0 or 1, this process, or its group."""
    if pid <= 1 or pid in (os.getpid(), os.getpgrp()):
        return
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        with suppress(ProcessLookupError):
            os.kill(pid, signum)


def _process_problem(target: Target, at: str) -> str | None:
    """Why the recorded worker process can't be running the run any more, if it can't."""
    pid, start_time = target
    stat = proc_stat(pid)
    if stat is None or stat.state in DEAD_STATES:
        return (
            f"The worker process (pid {pid}) ended without recording an outcome{at}. It "
            "probably crashed inside the FMU; check log_tail, then start a new run."
        )
    if not _same(stat, start_time):
        return (
            f"The worker process (pid {pid}) is gone and its pid now belongs to another "
            f"process, which was left alone. The run ended without an outcome{at}; check "
            "log_tail, then start a new run."
        )
    return None


def _seconds_since(moment: datetime) -> float:
    return (utc_now() - moment).total_seconds()


def heartbeat_age(status: RunStatus) -> float:
    """Seconds since the worker's last heartbeat: ``time.monotonic() - heartbeat_mono`` when the
    worker wrote it (one machine's CLOCK_MONOTONIC: a wall-clock jump or a host that slept
    can't make a healthy worker look stale), else the wall-clock age of ``heartbeat_at`` (or
    ``updated_at``) for older status files without it."""
    if status.heartbeat_mono is not None:
        return time.monotonic() - status.heartbeat_mono
    return _seconds_since(status.heartbeat_at or status.updated_at)


def _clip(message: str, limit: int) -> str:
    return message if len(message) <= limit else message[: limit - 3] + "..."


@dataclass(frozen=True)
class _Child:
    """A worker this server spawned: its handle (to reap it) and its start time at spawn."""

    process: subprocess.Popen[bytes]
    start_time: int | None


class Supervisor:
    """Owns the worker processes this server spawned. One per MCP server, created in the
    lifespan; holds the ``Popen`` handles so children can be reaped. Stateless about runs
    themselves: the run store is the truth, so two servers on one store cooperate.

    Thread-safe: sync MCP tools call it from worker threads.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._children: dict[str, _Child] = {}
        self._lock = threading.Lock()
        self.admission = threading.Lock()
        """Held by ``runs.start_run`` across the capacity check and the run-ID claim, so two
        threads of this server can't both take the last worker slot. Separate from the lock
        that guards the child table, which :meth:`active_run_ids` takes while it is held."""

    def spawn(self, run_dir: Path) -> int:
        """Start the worker for a run whose ``spec.json`` and ``queued`` status exist; return
        its pid. Raises ``OSError`` if the process can't be started."""
        run_path = Path(run_dir).resolve()
        self._reap_exited()
        with (run_path / runstore.LOG_FILE).open("ab") as log:
            process = subprocess.Popen(
                [*WORKER_COMMAND, str(run_path)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        stat = proc_stat(process.pid)  # unreaped, so /proc/<pid> exists even if it exited
        with self._lock:
            self._children[run_path.name] = _Child(process, stat.start_time if stat else None)
        return process.pid

    def reconcile(self, run_id: str) -> RunStatus:
        """Read the run's status, apply the takeover rules, update the index entry if the state
        changed, and return the (possibly new) status. Terminal states are returned as is.

        The status stays non-terminal if a worker that must be killed is still present after
        ``Timing.kill_confirm_s`` (a later call retries), or if the run left the state the
        takeover was decided on, as when the worker wrote ``running`` meanwhile (a later call
        re-evaluates it).

        Raises:
            UserError: ``run_id`` is malformed or unknown.
            ProtocolError: ``status.json`` (or the index) is unreadable.
        """
        self._reap_exited()
        run_path = runstore.run_dir(self.config.store_dir, run_id)
        status = runstore.read_status(run_path)
        if status.state not in TERMINAL_STATES:
            problem = self._problem(run_id, status)
            if problem is not None:
                status = self._take_over(run_id, run_path, status, problem)
        return self._sync_index(run_id, run_path, status)

    def reconcile_all(self) -> list[RunStatus]:
        """Reconcile every non-terminal run in the index. Called once at server startup.

        A run whose files can't be read is skipped with a warning, so one damaged run doesn't
        keep the server from starting.
        """
        self._reap_exited()  # finished workers would otherwise linger as zombies of the server
        statuses: list[RunStatus] = []
        for run_id in self._open_run_ids():
            try:
                statuses.append(self.reconcile(run_id))
            except (ProtocolError, UserError) as error:
                _log.warning("afmi: can't reconcile run %s: %s", run_id, error)
        return statuses

    def active_run_ids(self) -> list[str]:
        """Runs that are ``queued`` or ``running`` after reconciliation (for ``max_workers``)."""
        return [s.run_id for s in self.reconcile_all() if s.state not in TERMINAL_STATES]

    def peek_active_run_ids(self) -> list[str]:
        """Like :meth:`active_run_ids`, but writes nothing and signals nothing: a run counts if it
        is ``queued`` or ``running`` and its worker still looks fine, so the runs that
        :meth:`reconcile` would take over are left out. For read-only reports. A run whose files
        can't be read is not counted."""
        active: list[str] = []
        for run_id in self._open_run_ids():
            try:
                status = runstore.read_status(runstore.run_dir(self.config.store_dir, run_id))
            except (ProtocolError, UserError):
                continue
            if status.state not in TERMINAL_STATES and self._problem(run_id, status) is None:
                active.append(run_id)
        return active

    def stop(self, run_id: str) -> RunStatus:
        """Stop a run: SIGTERM the process group, wait up to ``Timing.stop_grace_s`` for the
        worker to write ``stopped``, else kill and reap it and write ``stopped`` (with
        ``finished_at``) yourself. A terminal run is returned unchanged.

        The run is reconciled first, so a worker that had already died gives
        ``failed{worker_lost}``. A queued run without a known process is stopped after the
        grace; if its worker starts just then, that worker is stopped in turn. Returns a
        non-terminal status only if the kill couldn't be confirmed.
        """
        status = self.reconcile(run_id)
        if status.state in TERMINAL_STATES:
            return status
        run_path = runstore.run_dir(self.config.store_dir, run_id)
        status, target = self._request_stop(run_id, run_path)
        if status.state not in TERMINAL_STATES:
            status = self._kill_and_end(run_id, run_path, status, target)
            if target is None and status.state not in TERMINAL_STATES:
                return self.stop(run_id)  # the worker won the race to leave queued: stop it
        return self._sync_index(run_id, run_path, status)

    def _problem(self, run_id: str, status: RunStatus) -> str | None:
        """Why a non-terminal run must be taken over, or ``None`` if its worker is fine."""
        if status.state is RunState.QUEUED:
            return self._queued_problem(run_id, status)
        at = f" at t={status.t:g}" if status.t is not None else ""
        target = self._target(run_id, status)
        gone = _process_problem(target, at) if target is not None else None
        return gone or self._heartbeat_problem(status, target, at)

    def _heartbeat_problem(self, status: RunStatus, target: Target | None, at: str) -> str | None:
        age = heartbeat_age(status)
        limit = self.config.timing.heartbeat_stale_s
        if age <= limit:
            return None
        who, killed = (f" (pid {target[0]})", ", so it was killed") if target else ("", "")
        return (
            f"The worker{who} sent no heartbeat for {age:.1f} s (limit {limit:g} s){at}"
            f"{killed}. It was hung or suspended; check log_tail, then start a new run."
        )

    def _queued_problem(self, run_id: str, status: RunStatus) -> str | None:
        child = self._child(run_id)
        code = child.process.poll() if child is not None else None
        if code is not None:
            return (
                f"The worker never started: its process exited with code {code} while the run "
                "was still queued. Check log_tail, then start a new run."
            )
        age = _seconds_since(status.updated_at)
        limit = self.config.timing.spawn_timeout_s
        if age > limit:
            return (
                f"The worker never started: the run stayed queued for {age:.0f} s (limit "
                f"{limit:g} s). Check log_tail, then start a new run."
            )
        return None

    def _take_over(self, run_id: str, run_path: Path, status: RunStatus, message: str) -> RunStatus:
        """Kill and reap the worker, then record ``failed{worker_lost}`` with the log tail if the
        run is still in the state the takeover was decided on."""
        target = self._target(run_id, status)
        if not self._confirm_dead(run_id, target):
            return status
        limits = self.config.limits
        error = RunError(
            code=ErrorCode.WORKER_LOST,
            message=_clip(message, limits.message_max_chars),
            log_tail=runstore.tail_log(run_path, limits.log_tail_bytes) or None,
        )
        return self._end(
            run_path,
            frozenset({status.state}),
            target is not None,
            {"state": RunState.FAILED, "error": error},
            EventType.FAILED,
            f"{ErrorCode.WORKER_LOST.value}: {error.message}",
        )

    def _request_stop(self, run_id: str, run_path: Path) -> tuple[RunStatus, Target | None]:
        """SIGTERM the worker (again if a new one shows up) and poll for a terminal state for
        up to the stop grace. Returns the last status and the worker to kill, if any."""
        timing = self.config.timing
        deadline = time.monotonic() + timing.stop_grace_s
        signalled: Target | None = None
        while True:
            status = runstore.read_status(run_path)
            if status.state in TERMINAL_STATES:
                return status, None
            target = self._target(run_id, status)
            if target is not None and target != signalled and is_alive(*target):
                _signal_group(target[0], signal.SIGTERM)
                signalled = target
            if target is not None and not is_alive(*target):
                return status, target  # died at once (e.g. before installing its handler)
            if time.monotonic() >= deadline:
                return status, target
            time.sleep(timing.poll_interval_s)

    def _kill_and_end(
        self, run_id: str, run_path: Path, status: RunStatus, target: Target | None
    ) -> RunStatus:
        """The stop fallback: kill and reap, write ``stopped``, remove ``fmu/`` and ``work/``
        (any partial result the worker wrote stays). With the worker dead nothing else can
        change the state, so any active one is stopped; without a known worker, only the state
        the grace ended on (a worker may be leaving ``queued`` right now)."""
        if not self._confirm_dead(run_id, target):
            return status
        expected = ACTIVE_STATES if target is not None else frozenset({status.state})
        at = f" at t={status.t:g}" if status.t is not None else ""
        message = (
            f"Stopped by the server{at}: the worker didn't record the stop itself within the "
            f"{self.config.timing.stop_grace_s:g} s grace (for example during a long native "
            "step), so it was killed."
            if target is not None
            else "Stopped by the server before a worker took the run over."
        )
        stopped = {"state": RunState.STOPPED}
        return self._end(
            run_path, expected, target is not None, stopped, EventType.STOPPED, message
        )

    def _confirm_dead(self, run_id: str, target: Target | None) -> bool:
        """:func:`kill_and_reap` the worker (if any is known), through our handle if it is our
        child: it then counts as gone only once reaped."""
        if target is None:
            return True
        child = self._child(run_id)
        handle = child.process if child is not None and child.process.pid == target[0] else None
        confirmed = kill_and_reap(*target, self.config.timing.kill_confirm_s, handle)
        if not confirmed:
            _log.warning("afmi: run %s: worker pid %d survived SIGKILL", run_id, target[0])
        return confirmed

    def _end(
        self,
        run_path: Path,
        expected: frozenset[RunState],
        worker_dead: bool,
        fields: dict[str, Any],
        kind: EventType,
        message: str,
    ) -> RunStatus:
        """Compare-and-set a terminal state under the store lock: write it only if the run is
        still in one of the ``expected`` (active) states, not if the worker's last write,
        another server's takeover or the worker's ``queued`` -> ``running`` came first. Then
        remove ``fmu/`` and ``work/`` if this call wrote or the worker is confirmed dead
        (``worker_dead``), and append the event if it wrote. Returns the status now in
        ``status.json``."""
        now = utc_now()
        ended: RunStatus | None = None
        with locked(self.config.store_dir / runstore.LOCK_FILE):
            current = runstore.read_status(run_path)
            if current.state in expected:
                ended = current.model_copy(update={**fields, "finished_at": now, "updated_at": now})
                runstore.write_status(run_path, ended)
        if ended is not None or worker_dead:  # nothing can be using fmu/ or work/ any more
            for scratch in runstore.SCRATCH_DIRS:
                shutil.rmtree(run_path / scratch, ignore_errors=True)
        if ended is None:
            return current
        self._event(ended.run_id, kind, message)
        return ended

    def _event(self, run_id: str, kind: EventType, message: str) -> None:
        """Append a server event; a failed append is logged, never fatal (status is written)."""
        text = _clip(message, self.config.limits.message_max_chars)
        event = Event(at=utc_now(), run_id=run_id, type=kind, source="server", message=text)
        try:
            runstore.append_event(self.config.store_dir, event)
        except (OSError, ValueError) as error:
            _log.warning("afmi: can't append the %s event of %s: %s", kind.value, run_id, error)

    def _sync_index(self, run_id: str, run_path: Path, status: RunStatus) -> RunStatus:
        """Bring the run's cached index state in line with ``status.json`` (workers never write
        the index) and return ``status``. Writes only when the state differs or the entry is
        missing."""
        store_dir = self.config.store_dir
        entries = runstore.read_index(store_dir).runs
        entry = next((e for e in entries if e.run_id == run_id), None)
        if entry is not None and entry.state == status.state:
            return status
        if entry is None:
            entry = runstore.index_entry(runstore.read_job_spec(run_path), status)
        else:
            changed = {"state": status.state, "updated_at": status.updated_at}
            entry = entry.model_copy(update=changed)
        runstore.upsert_index(store_dir, entry)
        return status

    def _open_run_ids(self) -> list[str]:
        index = runstore.read_index(self.config.store_dir)
        return [e.run_id for e in index.runs if e.state not in TERMINAL_STATES]

    def _target(self, run_id: str, status: RunStatus) -> Target | None:
        """The worker to watch or kill: the pid in ``status.json``, else our own child. A reaped
        child's pid may belong to someone else by now, so it is only named with its start
        time, which tells the two apart."""
        child = self._child(run_id)
        if status.pid is not None:
            start_time = status.pid_start_time
            if start_time is None and child is not None and child.process.pid == status.pid:
                start_time = child.start_time
            return status.pid, start_time
        if child is not None and (child.start_time is not None or child.process.poll() is None):
            return child.process.pid, child.start_time
        return None

    def _child(self, run_id: str) -> _Child | None:
        with self._lock:
            return self._children.get(run_id)

    def _reap_exited(self) -> None:
        """Collect every child of ours that has exited, so none lingers as a zombie, and forget
        those whose run has ended. An exited child of a run that is still queued is kept: it
        shows that the worker never started."""
        with self._lock:
            children = list(self._children.items())
        for run_id, child in children:
            if child.process.poll() is not None and self._has_ended(run_id):
                with self._lock:
                    if self._children.get(run_id) is child:
                        del self._children[run_id]

    def _has_ended(self, run_id: str) -> bool:
        """True if the run is terminal, or gone or unreadable (nothing left to supervise)."""
        try:
            status = runstore.read_status(self.config.store_dir / runstore.RUNS_DIR / run_id)
        except ProtocolError:
            return True
        return status.state in TERMINAL_STATES
