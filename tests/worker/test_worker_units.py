"""In-process tests of ``worker.py`` for paths that load no FMU binary: the pause-aware clock,
the status-file guard, the pid start time, the early exits of ``main`` (not queued, unusable
store, unreadable spec, changed or binary-less FMU), the watchdog's last-moment guard and its
timeout advice.

Collaborators: ``agentic_fmi.worker`` (under test), ``tests/worker/conftest.py`` (``new_run``),
``agentic_fmi.store.runstore``. Real processes, signals and timeouts are tested in
``test_worker_runs.py`` and ``test_worker_supervision.py``.
"""

from __future__ import annotations

import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fmi import worker
from agentic_fmi.config import Limits, Timing
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState, utc_now

FmuFactory = Callable[[str, str], Path]
RunBuilder = Callable[..., Path]
PROC_STAT = Path("/proc/self/stat")


class _FakeTime:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _HardExit(Exception):
    """Raised instead of ending the test process."""


def _without_linux_binaries(source: Path, target: Path) -> Path:
    """A copy of ``source`` whose Linux binaries are gone (other platforms stay)."""
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as copy:
        for item in original.infolist():
            if "linux" not in item.filename:
                copy.writestr(item, original.read(item))
    return target


def test_active_clock_excludes_paused_time() -> None:
    fake = _FakeTime()
    clock = worker.ActiveClock(now=fake)
    fake.now += 2.0
    clock.pause()
    fake.now += 10.0
    clock.pause()  # already paused: no effect
    assert clock.elapsed() == pytest.approx(2.0)
    clock.resume()
    clock.resume()  # already running: no effect
    fake.now += 3.0
    assert clock.elapsed() == pytest.approx(5.0)


def test_status_file_refuses_writes_after_a_terminal_state(
    fmu: FmuFactory, new_run: RunBuilder
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    status = worker.StatusFile(run_path, runstore.read_status(run_path))

    written: list[bool] = []

    def beat() -> None:
        written.extend(status.update(t=0.5, heartbeat_at=utc_now()) for _ in range(25))

    threads = [threading.Thread(target=beat) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert written == [True] * 100
    assert runstore.read_status(run_path).t == 0.5
    assert status.update(state=RunState.FINISHED, finished_at=utc_now())
    final = (run_path / runstore.STATUS_FILE).read_bytes()
    assert not status.update(state=RunState.RUNNING, heartbeat_at=utc_now())
    assert (run_path / runstore.STATUS_FILE).read_bytes() == final
    assert status.current.state is RunState.FINISHED


@pytest.mark.skipif(not PROC_STAT.is_file(), reason="needs /proc (Linux)")
def test_own_start_time_is_field_22_of_proc_stat() -> None:
    assert worker._own_start_time() == int(PROC_STAT.read_text().split()[21])


def test_main_leaves_a_run_that_is_not_queued_alone(fmu: FmuFactory, new_run: RunBuilder) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    runstore.write_status(
        run_path, runstore.read_status(run_path).model_copy(update={"state": RunState.STOPPED})
    )
    before = (run_path / runstore.STATUS_FILE).read_bytes()
    assert worker.main(run_path) == worker.EXIT_OK
    assert (run_path / runstore.STATUS_FILE).read_bytes() == before


def test_main_reports_an_unusable_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert worker.main(tmp_path / "runs" / "r1") == worker.EXIT_STORE_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unusable" in captured.err


def test_main_fails_a_run_whose_spec_is_unreadable(fmu: FmuFactory, new_run: RunBuilder) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"))
    (run_path / runstore.SPEC_FILE).write_text("{not json", encoding="utf-8")
    assert worker.main(run_path) == worker.EXIT_OK
    status = runstore.read_status(run_path)
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.INTERNAL_ERROR
    assert "spec.json" in status.error.message


def test_main_fails_an_fmu_without_a_binary_for_this_platform(
    fmu: FmuFactory, new_run: RunBuilder, workspace: Path
) -> None:
    path = _without_linux_binaries(fmu("3.0", "BouncingBall"), workspace / "NoLinux.fmu")
    run_path = new_run(path)
    assert worker.main(run_path) == worker.EXIT_OK
    status = runstore.read_status(run_path)
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.NOT_RUNNABLE
    assert "no binary for" in status.error.message
    assert not (run_path / runstore.FMU_DIR).exists()


def test_main_fails_a_run_whose_fmu_is_gone(fmu: FmuFactory, new_run: RunBuilder) -> None:
    path = fmu("2.0", "BouncingBall")
    run_path = new_run(path)
    path.unlink()
    assert worker.main(run_path) == worker.EXIT_OK
    status = runstore.read_status(run_path)
    assert status.error is not None and status.error.code is ErrorCode.FMU_CHANGED
    assert "can no longer be read" in status.error.message


def test_watchdog_records_timeout_then_exits(
    fmu: FmuFactory, new_run: RunBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"), timeout_s=1e-6)
    monkeypatch.setattr(worker, "_hard_exit", _raise_hard_exit)
    run = worker._Worker(run_path, runstore.read_status(run_path), Timing(), Limits())
    run._take_over(runstore.read_spec(run_path))
    (run_path / runstore.FMU_DIR).mkdir()
    with pytest.raises(_HardExit):
        run._tick()
    status = runstore.read_status(run_path)
    assert status.error is not None and status.error.code is ErrorCode.TIMEOUT
    assert not (run_path / runstore.FMU_DIR).exists()


def test_watchdog_never_kills_a_run_that_already_ended(
    fmu: FmuFactory, new_run: RunBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_path = new_run(fmu("2.0", "BouncingBall"), timeout_s=1e-6)
    monkeypatch.setattr(worker, "_hard_exit", _raise_hard_exit)
    run = worker._Worker(run_path, runstore.read_status(run_path), Timing(), Limits())
    run._take_over(runstore.read_spec(run_path))
    run.status.update(state=RunState.FINISHED, finished_at=utc_now())
    run._tick()  # past the timeout, but the run is over: no exit, no failed status
    assert runstore.read_status(run_path).state is RunState.FINISHED


def test_the_timeout_message_of_a_live_run_offers_no_model_exchange(
    fmu: FmuFactory, new_run: RunBuilder
) -> None:
    """A live run is Co-Simulation only and refuses Model Exchange, so the advice must not
    suggest it; a batch FMU run still gets it."""
    messages: dict[bool, str] = {}
    for live in (False, True):
        run_path = new_run(
            fmu("2.0", "BouncingBall"), live=live, idle_limit_s=60.0 if live else None
        )
        run = worker._Worker(run_path, runstore.read_status(run_path), Timing(), Limits())
        assert run._take_over(runstore.read_spec(run_path))
        messages[live] = run._timeout_message()
    assert "fmi_type='ModelExchange'" in messages[False]
    live_message = messages[True]
    assert "ModelExchange" not in live_message
    assert "larger timeout_s" in live_message and "larger output_interval" in live_message


def _raise_hard_exit() -> None:
    raise _HardExit
