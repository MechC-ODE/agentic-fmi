"""The worker process (``afmi worker <run_dir>``): executes one run or build job in isolation.

It is the only place, besides ``engines/``, where FMU native code runs (ADR 0002), and the only
place a build tool runs (ADR 0008). The server spawns it with
``sys.executable -m agentic_fmi worker <run_dir>`` in a new session, with stdout and stderr
appended to ``runs/<id>/log.txt``, so nothing an FMU or a build tool prints can reach MCP stdio.
The worker itself writes only diagnostics (tracebacks) to stderr, never protocol data to stdout.
It dispatches on the spec's ``kind``: a simulation run (``engines/fmpy_engine.py``), a build
job (``build.execute_build``) or a system run (``engines/oms_engine.py``, ADR 0009), with the
same status, heartbeat, watchdog and stop handling. Both engines share the seam of
``engines/base.py``, so results are written the same way.

Collaborators: ``store/runstore.py`` (spec, status, summary, result, events, log tail),
``store/atomic.locked`` (the store lock its ``queued`` -> ``running`` compare-and-set shares
with the server's terminal writes), ``engines/`` (simulation), ``oms_pin.py`` (verifies the
OMSimulator install before ``engines.oms_engine`` is imported, lazily), ``build.py`` (build
jobs), ``analysis/summary.py`` (the summary), ``live.py`` (the gate and ``live.json`` buffer of
live runs, ADR 0010), ``config.Timing`` and ``config.Limits``.
It reads its own pid start time with ``supervisor.proc_stat``, the parser the server checks it
with (that module imports no server-only code).
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import IO, Any, NoReturn

import fmpy

from agentic_fmi import build, oms_pin
from agentic_fmi.analysis.summary import summarize
from agentic_fmi.config import Limits, Timing
from agentic_fmi.engines import fmpy_engine
from agentic_fmi.engines.base import EngineResult, Live, LiveGate, ended_early
from agentic_fmi.errors import ProtocolError
from agentic_fmi.live import SINGLE_FMU, LiveBuffer, LiveControl, LiveView, live_columns
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import locked
from agentic_fmi.store.protocol import (
    TERMINAL_STATES,
    BuildEvent,
    BuildRecord,
    BuildSpec,
    ErrorCode,
    Event,
    EventType,
    FmuRef,
    RunError,
    RunSpec,
    RunState,
    RunStatus,
    RunSummary,
    SystemSpec,
    utc_now,
)
from agentic_fmi.supervisor import proc_stat

EXIT_OK = 0
"""The run was executed or recorded as failed, or another party already owns it."""
EXIT_STORE_UNUSABLE = 2
"""``status.json`` couldn't be read or written; nothing reliable was recorded."""

PROGRESS_INTERVAL_S = 0.2
"""Heartbeat thread tick: ``t`` and ``progress`` reach ``status.json`` at most 5 times a second."""

MODEL_DESCRIPTION = "modelDescription.xml"
COPY_CHUNK_BYTES = 1 << 20
"""Read size while a system run copies and hashes a component's FMU."""
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
"""Signals that stop the run cleanly through the gate (SIGINT for a worker run by hand)."""


def main(run_dir: Path, timing: Timing | None = None) -> int:
    """Run the simulation in ``run_dir`` and return the process exit code (0 even for a failed
    run that was recorded; non-zero only if the store itself is unusable). A run that times out
    never returns: the watchdog records ``failed{timeout}`` and the process dies by SIGKILL
    (exit status -9 to the server), or by ``os._exit(0)`` when run by hand (:func:`_hard_exit`).

    Sequence (ADR 0002, ADR 0007):

    1. Read ``status.json``. If the state is not ``queued``, exit 0 without writing: the server
       has already taken the run over. Read ``spec.json`` (``runstore.read_job_spec``); if it
       is unreadable, write ``failed{internal_error}``. A build job (``kind: "build"``) then
       runs steps 2, 3 and 9 around :meth:`_Worker._build` instead of steps 4 to 8; a system
       run (``kind: "system"``) replaces steps 4 to 6 with :meth:`_Worker._execute_system`
       (pin check, hashed FMU copies, the OMSimulator engine).
    2. Install a SIGTERM (and SIGINT) handler that sets a stop flag, then write ``running`` with
       ``pid``, ``pid_start_time`` (field 22 of ``/proc/self/stat``), ``started_at``,
       ``heartbeat_at``, ``t = t_start``; append a ``started`` event. This write is a
       compare-and-set under the store lock (:class:`StatusFile`): if a server ended the run
       since step 1, exit 0 having written nothing and run nothing.
    3. Start the heartbeat thread. Every :data:`PROGRESS_INTERVAL_S` it writes the current ``t``
       and ``progress`` if ``t`` moved, and at least every ``timing.heartbeat_interval_s`` it
       writes ``heartbeat_at`` (one lock serializes all status writes; nothing is written after
       a terminal state). It is also the watchdog: it accumulates non-paused wall time
       (:class:`ActiveClock`, which leaves out paused time) and, past ``spec.timeout_s``, writes
       ``failed{timeout, log_tail}``, removes the scratch directories (``fmu/``, ``work/``),
       appends a ``failed`` event and ends the process at once (:func:`_hard_exit`, whose group
       kill also ends a build tool). ctypes releases the GIL during FMU calls, so this works
       while a native ``doStep`` blocks (Roberts CS).
    4. Re-hash the FMU (``hashlib.file_digest``); on mismatch with ``spec.fmu.sha256``, or if it
       can't be read, write ``failed{fmu_changed}``. No binary for this platform gives
       ``failed{not_runnable}``.
    5. Extract the FMU from the same open handle into ``run_dir/fmu`` with ``fmpy.extract``
       (only ``modelDescription.xml``, ``resources/`` and ``binaries/<platform>/``).
    6. Call ``fmpy_engine.simulate(spec, run_dir / "fmu", gate)``; the gate records ``t`` and
       returns ``not stop_requested``. The watchdog is disarmed when it returns or raises.
    7. On success write ``result.csv``, ``summary.json``, then ``finished`` (or ``stopped`` if
       the gate stopped it) with ``finished_at``; append the event.
    8. On an exception write ``failed{simulation_error}`` with the first line of the message
       (at most ``Limits.message_max_chars``) and the log tail; never a traceback in status
       (the traceback goes to stderr, that is ``log.txt``, after the status is written). A bug
       elsewhere gives ``failed{internal_error}`` the same way.
    9. ``finally``: stop the heartbeat thread, restore the signal handlers and remove the
       scratch directories (``runstore.SCRATCH_DIRS``).

    A build job's ``failed`` message is clipped to ``Limits.build_diagnostics_max_chars``
    instead of ``message_max_chars``, and ``build_failed`` carries no log tail (its message is
    the diagnostics). A successful build appends one ``fmu_built`` event, then writes
    ``finished`` with progress 1; a stopped one writes ``stopped``.

    Live runs (ADR 0010):

    - Every heartbeat write also carries ``heartbeat_mono = time.monotonic()``.
    - The batch gate is ``_gate(t, live)`` (it ignores ``live``); batch runs never poll the
      inbox and never write ``live.json``.
    - A live spec (``spec.live``, either kind) gets :meth:`_Worker._live_control` before the
      engine runs: the model descriptions (``read_model_description`` on ``fmu/`` or on each
      ``work/fmus/<component>.fmu``, XML only), a ``live.LiveBuffer`` and a ``live.LiveControl``
      whose ``on_wait`` pauses and resumes :attr:`_Worker.clock`. The engine gets
      ``control.gate``. The heartbeat thread then also writes ``state``, ``speed`` and
      ``last_cmd`` from ``control.view()`` when they change, and ``live.json`` from
      ``buffer.snapshot()`` (at most every :data:`PROGRESS_INTERVAL_S`, only when new, and once
      more as it stops, after the main thread sampled the last row).
    - :meth:`_Worker._finish` writes ``status.reason``: ``control.reason`` for a live run the
      gate stopped (the idle limit), or "ended early at t=... of t_stop" when
      ``engines.base.ended_early`` holds (batch runs too); before that ``control.finish(t_end)``
      sweeps the inbox (also after a failure, before the terminal status is written; not on a
      timeout, which the heartbeat thread records).
    """
    run_path = Path(run_dir).resolve()
    try:
        status = runstore.read_status(run_path)
        if status.state != RunState.QUEUED:
            return EXIT_OK
        return _Worker(run_path, status, timing or Timing(), Limits()).run()
    except (OSError, ProtocolError) as error:
        _complain(f"afmi worker: the run store at {run_path} is unusable: {error}")
        return EXIT_STORE_UNUSABLE


class ActiveClock:
    """Wall time that excludes paused intervals: what ``timeout_s`` limits (ADR 0002).

    Batch runs and builds never pause; the live gate (simulation thread) calls :meth:`pause`
    and :meth:`resume` while the watchdog (heartbeat thread) reads :meth:`elapsed`, so one lock
    keeps the two fields consistent.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._started = now()
        self._paused_total = 0.0
        self._paused_since: float | None = None

    def pause(self) -> None:
        """Stop counting; a second call while paused changes nothing."""
        with self._lock:
            if self._paused_since is None:
                self._paused_since = self._now()

    def resume(self) -> None:
        """Count again; a call while running changes nothing."""
        with self._lock:
            if self._paused_since is not None:
                self._paused_total += self._now() - self._paused_since
                self._paused_since = None

    def elapsed(self) -> float:
        """Seconds counted since creation, without the paused intervals."""
        with self._lock:
            now = self._now()
            paused = self._paused_total
            if self._paused_since is not None:
                paused += now - self._paused_since
            return now - self._started - paused


class StatusFile:
    """The worker's handle on ``status.json`` while it owns it (single-writer rule, ADR 0007).

    One lock serializes the main thread and the heartbeat thread, and once a terminal state is
    written every later update is refused, so a late heartbeat can't revive a finished run.
    While the run is ``queued`` a server may still end it (its takeover or ``run_stop``), so a
    write from ``queued`` is a compare-and-set under the store lock the server holds for those
    terminal writes; the lock covers only that read-check-write.
    """

    def __init__(self, run_path: Path, status: RunStatus) -> None:
        self._run_path = run_path
        self._store_lock = run_path.parent.parent / runstore.LOCK_FILE
        self._status = status
        self._lock = threading.Lock()

    @property
    def current(self) -> RunStatus:
        """The status last written (or read at start)."""
        return self._status

    def update(self, **fields: Any) -> bool:
        """Write the status with ``fields`` changed and ``updated_at`` set to now.

        Returns:
            False, without writing, if a terminal state was already written, or if the run was
            ``queued`` and ``status.json`` no longer is (a server ended it first).
        """
        with self._lock:
            if self._status.state in TERMINAL_STATES:
                return False
            if self._status.state is not RunState.QUEUED:
                self._write(fields)
                return True
            with locked(self._store_lock):
                if runstore.read_status(self._run_path).state is not RunState.QUEUED:
                    return False
                self._write(fields)
                return True

    def _write(self, fields: dict[str, Any]) -> None:
        status = self._status.model_copy(update={**fields, "updated_at": utc_now()})
        runstore.write_status(self._run_path, status)
        self._status = status


class _Ticker(threading.Thread):
    """A daemon thread that calls ``tick`` every ``interval_s`` until :meth:`halt`, then
    ``last`` once (in this thread: a live run's final ``live.json``)."""

    def __init__(
        self, interval_s: float, tick: Callable[[], None], last: Callable[[], None]
    ) -> None:
        super().__init__(name="afmi-heartbeat", daemon=True)
        self._interval_s = interval_s
        self._tick = tick
        self._last = last
        self._halted = threading.Event()

    def run(self) -> None:
        while not self._halted.wait(self._interval_s):
            self._call(self._tick)
        self._call(self._last)

    @staticmethod
    def _call(action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as error:  # keep beating: the next tick retries the write
            _complain(f"afmi worker: heartbeat write failed: {error!r}")

    def halt(self) -> None:
        """Stop ticking and wait for a tick in progress to finish."""
        self._halted.set()
        if self.is_alive():
            self.join()


class _Worker:
    """One run, from ``queued`` to a terminal state. Built and driven by :func:`main`."""

    def __init__(self, run_path: Path, status: RunStatus, timing: Timing, limits: Limits) -> None:
        self.run_path = run_path
        self.store_dir = run_path.parent.parent
        self.fmu_dir = run_path / runstore.FMU_DIR
        self.timing = timing
        self.limits = limits
        self.status = StatusFile(run_path, status)
        self.clock = ActiveClock()
        self.t = status.t_start
        self.stop_requested = False
        self.is_build = False
        self.is_system = False
        self.is_live = False
        self._timeout_s = math.inf
        self._armed = False
        self._watchdog_lock = threading.Lock()
        self._written_t = status.t_start
        self._written_at = time.monotonic()
        self.control: LiveControl | None = None
        self.buffer: LiveBuffer | None = None
        self._written_view: LiveView | None = None

    def run(self) -> int:
        """Execute the run; see :func:`main` for the sequence."""
        try:
            spec = runstore.read_job_spec(self.run_path)
        except ProtocolError as error:
            self._fail(ErrorCode.INTERNAL_ERROR, f"spec.json is unusable: {error}")
            return EXIT_OK
        self.is_build = isinstance(spec, BuildSpec)
        self.is_system = isinstance(spec, SystemSpec)
        previous = _install_stop_handlers(self._request_stop)
        interval = min(PROGRESS_INTERVAL_S, self.timing.heartbeat_interval_s)
        ticker = _Ticker(interval, self._tick, self._write_live)
        try:
            if not self._take_over(spec):
                return EXIT_OK  # a server ended the run first: it isn't ours to run
            ticker.start()
            self._execute_or_record(spec)
        finally:
            ticker.halt()
            _restore_handlers(previous)
            self._remove_scratch()
        return EXIT_OK

    def _take_over(self, spec: runstore.JobSpec) -> bool:
        """Write ``running`` (compare-and-set from ``queued``) and arm the watchdog. Returns
        False, having written nothing, if ``status.json`` is no longer ``queued``."""
        now = utc_now()
        started = self.status.update(
            state=RunState.RUNNING,
            pid=os.getpid(),
            pid_start_time=_own_start_time(),
            started_at=now,
            heartbeat_at=now,
            heartbeat_mono=time.monotonic(),
            t=self.t,
            progress=0.0,
        )
        if not started:
            return False
        self._written_at = time.monotonic()
        self._event(EventType.STARTED, f"worker pid {os.getpid()}")
        self.clock = ActiveClock()
        self._timeout_s = spec.timeout_s
        self.is_live = isinstance(spec, RunSpec | SystemSpec) and spec.live
        self._armed = True
        return True

    def _execute_or_record(self, spec: runstore.JobSpec) -> None:
        """Run the steps after take-over; a bug in them is recorded as ``internal_error``."""
        try:
            if isinstance(spec, BuildSpec):
                self._build(spec)
            elif isinstance(spec, SystemSpec):
                self._execute_system(spec)
            else:
                self._execute(spec)
        except Exception as error:
            self._disarm()
            self._fail_with_traceback(ErrorCode.INTERNAL_ERROR, _first_line(error))

    def _build(self, spec: BuildSpec) -> None:
        """Run the build job and record its outcome: ``finished`` after the commit (with one
        ``fmu_built`` event), ``failed{build_failed}`` or ``stopped``."""
        outcome = build.execute_build(
            spec,
            self.run_path,
            self.limits,
            self.timing,
            should_stop=lambda: self.stop_requested,
            before_commit=self._before_commit,
        )
        self._disarm()
        if outcome.record is not None:
            self._built(outcome.record)
        elif outcome.failure is not None:
            self._fail(ErrorCode.BUILD_FAILED, outcome.failure)
        else:
            self._end_build(RunState.STOPPED, "Build cancelled; nothing was written to generated/.")

    def _before_commit(self) -> bool:
        """Disarm the watchdog before the build writes to ``generated/``; False, so nothing is
        written, if a stop was requested."""
        self._disarm()
        return not self.stop_requested

    def _built(self, record: BuildRecord) -> None:
        """Append the ``fmu_built`` event, then write ``finished``."""
        event = BuildEvent(
            at=utc_now(),
            run_id=record.run_id,
            fmu_path=record.fmu.path,
            fmu_sha256=record.fmu.sha256,
            message=f"{record.name} ({record.backend}, {record.duration_s:.2f} s)",
        )
        try:
            runstore.append_event(self.store_dir, event)
        except (OSError, ValueError) as error:
            _complain(f"afmi worker: can't append the fmu_built event: {error}")
        sha = record.fmu.sha256[:12]
        self._end_build(RunState.FINISHED, f"{record.fmu.path} (sha256 {sha}…)", progress=1.0)

    def _end_build(self, state: RunState, message: str, progress: float = 0.0) -> None:
        now = utc_now()
        written = self.status.update(
            state=state,
            progress=progress,
            finished_at=now,
            heartbeat_at=now,
            heartbeat_mono=time.monotonic(),
        )
        if written:
            self._event(EventType(state.value), message)

    def _execute(self, spec: RunSpec) -> None:
        problem = self._prepare_fmu(spec)
        if problem is not None:
            self._disarm()
            self._fail(*problem)
            return
        started = time.monotonic()
        try:
            gate = self._live_control(spec) if spec.live else self._gate
            outcome = fmpy_engine.simulate(spec, self.fmu_dir, gate)
        except Exception as error:
            self._disarm()
            self._end_live(self.t)
            self._fail_with_traceback(ErrorCode.SIMULATION_ERROR, _simulation_message(error))
            return
        self._disarm()
        self._finish(spec, outcome, time.monotonic() - started)

    def _execute_system(self, spec: SystemSpec) -> None:
        """Run a system run (ADR 0009), mirroring :meth:`_execute`:

        1. ``oms_pin.pin_problem(oms_pin.installed(), ...)`` against ``spec.omsimulator`` and
           against ``oms_pin.pinned()``: on a mismatch, ``failed{oms_changed}`` and return,
           before anything imports OMSimulator.
        2. Create ``work/fmus`` and ``work/tmp``; copy each component's FMU to
           ``work/fmus/<component>.fmu`` from one open handle while hashing it
           (``fmu_changed`` on a mismatch or an unreadable file; ``not_runnable`` without a
           binary for this platform).
        3. Set ``os.environ["TMPDIR"]`` and ``tempfile.tempdir`` to ``work/tmp`` (restored
           afterwards), then import ``agentic_fmi.engines.oms_engine`` here (lazily: never at
           module import, so FMU runs and build jobs never load OMSimulator; the engine itself
           imports OMSimulator only inside ``simulate``).
        4. ``oms_engine.simulate(spec, work, gate)``, with ``gate`` the live control for a
           live spec and ``self._gate`` otherwise; an exception is ``simulation_error``
           (after ending the live control); then :meth:`_finish` as for a run. ``work/`` goes
           with the scratch directories in :meth:`run`'s ``finally`` (and in the watchdog),
           which also removes the temp directory OMSimulator leaks per ``instantiate()``.
        """
        problem = self._prepare_system(spec)
        if problem is not None:
            self._disarm()
            self._fail(*problem)
            return
        work = self.run_path / runstore.WORK_DIR
        started = time.monotonic()
        try:
            gate = self._live_control(spec) if spec.live else self._gate
            with _temp_dir(work / runstore.WORK_TMP_DIR):
                from agentic_fmi.engines import oms_engine

                outcome = oms_engine.simulate(spec, work, gate)
        except Exception as error:
            self._disarm()
            self._end_live(self.t)
            self._fail_with_traceback(ErrorCode.SIMULATION_ERROR, _system_message(error))
            return
        self._disarm()
        self._finish(spec, outcome, time.monotonic() - started)

    def _prepare_system(self, spec: SystemSpec) -> tuple[ErrorCode, str] | None:
        """Verify the OMSimulator install against the spec's identity and against the current
        pin, then copy every component's FMU to ``work/fmus/`` while hashing it. Returns the
        failure to record, if any."""
        found = oms_pin.installed()
        for expected in (spec.omsimulator, oms_pin.pinned()):
            mismatch = oms_pin.pin_problem(found, expected)
            if mismatch is not None:
                return ErrorCode.OMS_CHANGED, mismatch
        work = self.run_path / runstore.WORK_DIR
        shutil.rmtree(work, ignore_errors=True)
        fmus = work / runstore.SYSTEM_FMUS_DIR
        fmus.mkdir(parents=True)
        (work / runstore.WORK_TMP_DIR).mkdir()
        for name, component in spec.components.items():
            problem = _copy_component(name, component.fmu, fmus / f"{name}.fmu")
            if problem is not None:
                return problem
        return None

    def _prepare_fmu(self, spec: RunSpec) -> tuple[ErrorCode, str] | None:
        """Re-hash, check the platform and extract, all from one open handle, so the bytes
        extracted are the bytes hashed. Returns the failure to record, if any."""
        path = Path(spec.fmu.path)
        try:
            handle = path.open("rb")
        except OSError as error:
            return ErrorCode.FMU_CHANGED, (
                f"The FMU {path} can no longer be read ({error.strerror or error}). Restore it "
                "or start a new run with the FMU's current path."
            )
        with handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != spec.fmu.sha256:
                return _changed(f"The FMU {path.name}", digest, spec.fmu.sha256)
            problem = _platform_problem(path.name, handle)
            if problem is not None:
                return problem
            self._extract(handle)
        return None

    def _extract(self, handle: IO[bytes]) -> None:
        """Unpack what FMPy needs into ``fmu/``; documentation and sources stay in the zip."""
        shutil.rmtree(self.fmu_dir, ignore_errors=True)
        self.fmu_dir.mkdir()
        handle.seek(0)
        fmpy.extract(handle, self.fmu_dir, include=_needed_member)

    def _gate(self, t: float, _live: Live) -> bool:
        """The batch gate: publish ``t`` for the heartbeat and honor a stop request; it never
        reads the inbox or the FMU."""
        self.t = t
        return not self.stop_requested

    def _live_control(self, spec: RunSpec | SystemSpec) -> LiveGate:
        """The gate of a live run (ADR 0010). Builds, and keeps as :attr:`control` and
        :attr:`buffer`: the model descriptions from ``fmu/`` or ``work/fmus/`` (XML only),
        ``live.live_columns``, a ``LiveBuffer`` and a ``LiveControl`` with ``stop_requested``
        reading :attr:`stop_requested`, ``on_wait`` pausing and resuming :attr:`clock`, and the
        worker's ``Timing``/``Limits``. The gate it returns also publishes ``t`` as
        :meth:`_gate` does."""
        if isinstance(spec, SystemSpec):
            fmus = self.run_path / runstore.WORK_DIR / runstore.SYSTEM_FMUS_DIR
            paths = {name: fmus / f"{name}.fmu" for name in spec.components}
        else:
            paths = {SINGLE_FMU: self.fmu_dir}
        descriptions = {
            key: fmpy.read_model_description(str(path), validate=False)
            for key, path in paths.items()
        }
        columns = live_columns(spec, descriptions, self.limits)
        buffer = LiveBuffer(spec.run_id, columns, self.limits, PROGRESS_INTERVAL_S)
        control = LiveControl(
            self.run_path,
            spec,
            descriptions,
            buffer,
            stop_requested=lambda: self.stop_requested,
            on_wait=self._on_wait,
            timing=self.timing,
            limits=self.limits,
        )
        self.control, self.buffer = control, buffer

        def gate(t: float, live: Live) -> bool:
            self.t = t
            return control.gate(t, live)

        return gate

    def _on_wait(self, waiting: bool) -> None:
        """The live gate waits (paused or pacing): the watchdog's clock stops meanwhile."""
        if waiting:
            self.clock.pause()
        else:
            self.clock.resume()

    def _end_live(self, t: float) -> None:
        """The inbox's final sweep of a live run, before its terminal status (main thread only:
        the watchdog's timeout path skips it, so a command pending then gets no outcome)."""
        if self.control is None:
            return
        try:
            self.control.finish(t)
        except Exception as error:  # the terminal status matters more than the sweep
            _complain(f"afmi worker: the final inbox sweep failed: {error!r}")

    def _request_stop(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop_requested = True

    def _finish(self, spec: RunSpec | SystemSpec, outcome: EngineResult, wall_s: float) -> None:
        """Write ``result.csv``, ``summary.json``, then ``finished`` or ``stopped``."""
        result = outcome.result
        state = RunState.STOPPED if outcome.stopped else RunState.FINISHED
        t_end = float(result["time"][-1]) if len(result) else spec.experiment.start_time
        runstore.write_result(self.run_path, result)
        runstore.write_summary(
            self.run_path,
            RunSummary(
                run_id=self.status.current.run_id,
                state=state,
                t_end=t_end,
                n_points=len(result),
                wall_time_s=round(wall_s, 3),
                columns=list(result.dtype.names or ())[1:],
                variables=summarize(result, outcome.units, self.limits.significant_digits),
            ),
        )
        progress = 1.0 if state is RunState.FINISHED else self._progress(t_end)
        reason = self._finish_live(result, t_end)
        experiment = spec.experiment
        if reason is None and ended_early(
            t_end, experiment.stop_time, outcome.stopped, experiment.output_interval
        ):
            reason = (
                f"ended early at t={t_end:g} of t_stop={experiment.stop_time:g}: the FMU "
                "ended the simulation itself (its message is in log.txt)"
            )
        now = utc_now()
        written = self.status.update(
            state=state,
            t=t_end,
            progress=progress,
            finished_at=now,
            heartbeat_at=now,
            heartbeat_mono=time.monotonic(),
            reason=reason,
            **self._view_fields(),
        )
        if written:
            message = f"t_end {t_end:g}, {len(result)} points, {wall_s:.2f} s"
            self._event(EventType(state.value), message + (f"; {reason}" if reason else ""))

    def _finish_live(self, result: Any, t_end: float) -> str | None:
        """For a live run: sample every ``live.json`` column at ``t_end`` from the result's last
        row (the heartbeat thread writes it once more as it stops), sweep the inbox, and return
        the gate's stop reason (the idle limit), if any."""
        if self.control is None or self.buffer is None:
            return None
        if len(result):
            recorded = set(result.dtype.names or ())
            values = [
                float(result[name][-1]) if name in recorded else None for name in self.buffer.names
            ]
            self.buffer.record(t_end, values, time.monotonic())
        self._end_live(t_end)
        return self.control.reason

    def _fail(self, code: ErrorCode, message: str) -> bool:
        """Write ``failed{code}`` with ``message`` and the log tail (none for ``build_failed``,
        whose message is the diagnostics); append the event."""
        _flush_output()
        limits = self.limits
        limit = limits.build_diagnostics_max_chars if self.is_build else limits.message_max_chars
        message = _clip(message, limit)
        tail = None if code is ErrorCode.BUILD_FAILED else self._log_tail()
        now = utc_now()
        error = RunError(code=code, message=message, log_tail=tail)
        written = self.status.update(
            state=RunState.FAILED,
            t=self.t,
            progress=self._progress(self.t),
            finished_at=now,
            heartbeat_at=now,
            heartbeat_mono=time.monotonic(),
            error=error,
            **self._view_fields(),
        )
        if written:
            self._event(EventType.FAILED, f"{code.value}: {message}")
        return written

    def _fail_with_traceback(self, code: ErrorCode, message: str) -> None:
        """:meth:`_fail`, then the traceback to stderr, so the tail in status has none."""
        self._fail(code, message)
        traceback.print_exc()
        _flush_output()

    def _tick(self) -> None:
        """Heartbeat thread: enforce the timeout, then publish progress and the heartbeat (with
        a live run's state, speed and last command when they change), then ``live.json``."""
        with self._watchdog_lock:
            if self._armed and self.clock.elapsed() > self._timeout_s:
                self._expire()
        t, now = self.t, time.monotonic()
        view = self.control.view() if self.control is not None else None
        due = now - self._written_at >= self.timing.heartbeat_interval_s
        if (t != self._written_t or due or view != self._written_view) and math.isfinite(t):
            live = (
                {"state": view.state, "speed": view.speed, "last_cmd": view.last_cmd}
                if view is not None
                else {}
            )
            self.status.update(
                t=t, progress=self._progress(t), heartbeat_at=utc_now(), heartbeat_mono=now, **live
            )
            self._written_t, self._written_at, self._written_view = t, now, view
        self._write_live()

    def _view_fields(self) -> dict[str, Any]:
        """A live run's final ``speed`` and ``last_cmd`` for its terminal status (the heartbeat
        thread may not have written the last change yet); nothing for other runs."""
        if self.control is None:
            return {}
        view = self.control.view()
        return {"speed": view.speed, "last_cmd": view.last_cmd}

    def _write_live(self) -> None:
        """Heartbeat thread only: write ``live.json`` if the buffer has news (live runs)."""
        snapshot = self.buffer.snapshot() if self.buffer is not None else None
        if snapshot is not None:
            runstore.write_live(self.run_path, snapshot)

    def _expire(self) -> None:
        """The watchdog: record the timeout and end the process, unless the run is already
        terminal. Called with the watchdog lock held, so the main thread can't disarm or move
        on to writing results meanwhile."""
        try:
            if not self._fail(ErrorCode.TIMEOUT, self._timeout_message()):
                self._armed = False  # the run ended meanwhile; nothing to kill
                return
        except Exception as error:  # kill anyway: the server then reports worker_lost
            _complain(f"afmi worker: can't record the timeout ({error!r}); exiting anyway")
        self._remove_scratch()
        _hard_exit()

    def _timeout_message(self) -> str:
        if self.is_build:
            log = f"runs/{self.status.current.run_id}/{runstore.BUILD_LOG_FILE}"
            return (
                f"The build exceeded its time limit of {self._timeout_s:g} s and was killed; "
                f"nothing was written to generated/. Check {log} in the run store (for example "
                "for a slave that blocks or loops while it is imported), fix the source, then "
                "call fmu_build again."
            )
        # A system, and a live run (Co-Simulation only), can't switch to Model Exchange.
        advice = (
            "a larger output_interval (the communication step)"
            if self.is_system or self.is_live
            else "fmi_type='ModelExchange' if a Co-Simulation step is very fine"
        )
        return (
            f"The run exceeded its timeout of {self._timeout_s:g} s of wall time at "
            f"t={self.t:g} and was killed. Pass a larger timeout_s, shorten stop_time, or "
            f"use {advice}."
        )

    def _remove_scratch(self) -> None:
        """Remove ``fmu/`` and ``work/`` (best effort: nothing else may need them now)."""
        for name in runstore.SCRATCH_DIRS:
            shutil.rmtree(self.run_path / name, ignore_errors=True)

    def _disarm(self) -> None:
        """Stop the watchdog; waits if it is recording a timeout right now (then never returns)."""
        with self._watchdog_lock:
            self._armed = False

    def _progress(self, t: float) -> float:
        """The fraction of ``t_start..t_stop`` reached; 0 for a build job (``t_start = t_stop``),
        whose progress becomes 1 only when it finishes."""
        status = self.status.current
        span = status.t_stop - status.t_start
        fraction = (t - status.t_start) / span if span > 0 else 0.0
        return min(max(fraction, 0.0), 1.0) if math.isfinite(fraction) else 0.0

    def _log_tail(self) -> str | None:
        return runstore.tail_log(self.run_path, self.limits.log_tail_bytes) or None

    def _event(self, kind: EventType, message: str) -> None:
        """Append a worker event; a failed append is reported to stderr, never fatal."""
        event = Event(
            at=utc_now(),
            run_id=self.status.current.run_id,
            type=kind,
            source="worker",
            message=message,
        )
        try:
            runstore.append_event(self.store_dir, event)
        except (OSError, ValueError) as error:
            _complain(f"afmi worker: can't append the {kind.value} event: {error}")


def _changed(label: str, digest: str, expected: str) -> tuple[ErrorCode, str]:
    return ErrorCode.FMU_CHANGED, (
        f"{label} changed after the run was created (sha256 {digest[:12]}..., expected "
        f"{expected[:12]}...). Start a new run to simulate the current file."
    )


def _platform_problem(label: str, fmu: IO[bytes] | str) -> tuple[ErrorCode, str] | None:
    """``not_runnable`` if the FMU (an open archive or a path) has no binary for this
    platform."""
    platforms = list(fmpy.supported_platforms(fmu))
    if fmpy.platform in platforms:
        return None
    return ErrorCode.NOT_RUNNABLE, (
        f"{label} has no binary for {fmpy.platform} (it ships: "
        f"{', '.join(platforms) or 'nothing'}). Use an FMU built for {fmpy.platform}."
    )


def _copy_component(name: str, ref: FmuRef, target: Path) -> tuple[ErrorCode, str] | None:
    """Copy a component's FMU to ``target`` from one open handle while hashing it, so the bytes
    OMSimulator loads are the bytes hashed; then check its platform. Returns the failure to
    record, if any."""
    source = Path(ref.path)
    try:
        handle = source.open("rb")
    except OSError as error:
        return ErrorCode.FMU_CHANGED, (
            f"The FMU {source} of component {name} can no longer be read "
            f"({error.strerror or error}). Restore it or start a new run with the FMU's "
            "current path in the system file."
        )
    digest = hashlib.sha256()
    with handle, target.open("wb") as copy:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
            copy.write(chunk)
    found = digest.hexdigest()
    if found != ref.sha256:
        return _changed(f"The FMU {source.name} of component {name}", found, ref.sha256)
    return _platform_problem(f"{source.name} (component {name})", str(target))


@contextmanager
def _temp_dir(path: Path) -> Iterator[None]:
    """Point ``TMPDIR`` and ``tempfile.tempdir`` at ``path`` while OMSimulator runs (it leaves
    one ``mkdtemp()`` directory per ``instantiate()``), then restore both."""
    saved_env, saved_tempdir = os.environ.get("TMPDIR"), tempfile.tempdir
    os.environ["TMPDIR"] = tempfile.tempdir = str(path)
    try:
        yield
    finally:
        tempfile.tempdir = saved_tempdir
        if saved_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = saved_env


def _needed_member(name: str) -> bool:
    """Archive members FMPy needs to run the FMU on this platform."""
    binaries = (f"binaries/{fmpy.platform}/", f"binaries/{fmpy.platform_tuple}/")
    return name == MODEL_DESCRIPTION or name.startswith(("resources/", *binaries))


def _own_start_time() -> int | None:
    """Field 22 of this process's ``/proc`` stat (clock ticks since boot), parsed by
    :func:`supervisor.proc_stat` exactly as the server will; ``None`` without ``/proc``."""
    stat = proc_stat(os.getpid())
    return stat.start_time if stat is not None else None


def _install_stop_handlers(handler: Callable[[int, FrameType | None], None]) -> list[Any]:
    """Route :data:`STOP_SIGNALS` to ``handler``; return the previous handlers."""
    previous = [signal.getsignal(signum) for signum in STOP_SIGNALS]
    for signum in STOP_SIGNALS:
        signal.signal(signum, handler)
    return previous


def _restore_handlers(previous: list[Any]) -> None:
    for signum, handler in zip(STOP_SIGNALS, previous, strict=True):
        signal.signal(signum, handler)


def _first_line(error: BaseException) -> str:
    """``Type: first line of the message`` (no traceback)."""
    lines = str(error).strip().splitlines()
    name = type(error).__name__
    return f"{name}: {lines[0].strip()}" if lines else name


def _simulation_message(error: BaseException) -> str:
    detail = _first_line(error).rstrip(".")
    return (
        f"The simulation failed: {detail}. Check log_tail for the FMU's own messages, then fix "
        "start_values, inputs or the experiment and start a new run."
    )


def _system_message(error: BaseException) -> str:
    detail = _first_line(error).rstrip(".")
    return (
        f"The system simulation failed: {detail}. Check log_tail for OMSimulator's and the "
        "FMUs' messages, then fix the system file, the parameters or the step and start a new "
        "run."
    )


def _clip(message: str, limit: int) -> str:
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _flush_output() -> None:
    """Flush Python's stdout and stderr into ``log.txt`` before reading its tail or exiting."""
    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.flush()


def _complain(text: str) -> None:
    """A diagnostic line on stderr (``log.txt``); stdout carries only FMU output."""
    print(text, file=sys.stderr, flush=True)


def _hard_exit() -> NoReturn:
    """End the process now, even while the main thread is blocked in a native FMU call.

    Spawned by the server (``start_new_session``), the worker leads its process group, so it
    SIGKILLs the group and takes any process the FMU or a build tool started with it (ADR 0007,
    ADR 0008). Run by hand under a shell or ``uv run``, it isn't the leader and only
    ``os._exit``s itself (a build tool it started then outlives it).
    """
    _flush_output()
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpid(), signal.SIGKILL)
    os._exit(EXIT_OK)
