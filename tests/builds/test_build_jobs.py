"""``fmu_build`` end to end with real workers and real ``pythonfmu`` builds: success and its
provenance, failures and their diagnostics, overwrite, one name built by two threads at once,
a commit whose write fails, a job that outlives ``wait_s`` (status,
list, stop), the timeout, and running the built FMU.

Collaborators: ``agentic_fmi.runs`` (``start_build``, ``get_run``, ``list_runs``, ``stop_run``,
``start_run``, ``get_results``), ``agentic_fmi.supervisor.Supervisor`` (spawns the workers),
``agentic_fmi.store.runstore`` (documents), ``agentic_fmi.budget`` (result size) and the
fixtures in ``tests/builds/conftest.py``. A build takes about a second (worker start plus
pythonfmu); the stop and timeout tests use a slave that blocks while it is imported.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from build_support import SLOW_SLAVE, event_lines, plant_build, processes_naming, wait_until

from agentic_fmi import budget, build, runs
from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import ErrorCode, RunState
from agentic_fmi.supervisor import Supervisor

WriteSource = Callable[..., Path]

SYNTAX_ERROR = """from pythonfmu import Fmi2Slave


class AfmiBad(Fmi2Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k = (2.0 +
"""
NAME_ERROR = """import warnings

from pythonfmu import Fmi2Slave

warnings.warn("gain is experimental")
LIMIT = undefined_limit * 2


class AfmiBad(Fmi2Slave):
    def do_step(self, current_time, step_size):
        return True
"""
INIT_ERROR = """from pythonfmu import Fmi2Slave


def helper(value):
    return value / 0


class AfmiBad(Fmi2Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k = helper(1.0)

    def do_step(self, current_time, step_size):
        return True
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _built_events(config: Config, run_id: str) -> list[dict[str, object]]:
    events = [json.loads(line) for line in event_lines(config)]
    return [e for e in events if e["type"] == "fmu_built" and e["run_id"] == run_id]


def _gone(run_path: Path) -> bool:
    return not processes_naming(run_path)


def test_a_build_writes_the_fmu_and_its_provenance_and_nothing_else(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    source = write_source("models/afmi_gain.py")
    before = (source.read_bytes(), sorted(os.listdir(source.parent)))
    view = runs.start_build(config, supervisor, "models/afmi_gain.py")

    assert view.state is RunState.FINISHED, view
    assert view.kind == "build" and view.progress == 1.0
    assert view.fmu == "generated/afmi_gain/afmi_gain.fmu"
    output = config.generated_dir / "afmi_gain"
    assert sorted(os.listdir(output)) == ["afmi_gain.fmu", "build.json"]
    fmu = output / "afmi_gain.fmu"
    record = runstore.read_build_record(output)
    assert record is not None and record.fmu.sha256 == _sha256(fmu)
    assert record.fmu.model_identifier == "AfmiGain"  # pythonfmu names its output after the class
    assert record.fmu.interfaces == ["CoSimulation"]
    assert record.source.path == "models/afmi_gain.py"
    assert record.source.sha256 == hashlib.sha256(before[0]).hexdigest()
    assert record.versions.pythonfmu == "0.7.0" and record.replaced_sha256 is None
    assert record.summary.outputs == ["y"] and record.summary.inputs == ["u"]
    assert record.summary.default_experiment is not None
    assert record.summary.default_experiment.stop_time == 1.0
    assert view.build is not None and view.build["sha256"] == record.fmu.sha256
    assert budget.json_size(view) <= config.limits.build_result_max_bytes
    # The source is untouched and nothing (no __pycache__) appeared next to it.
    assert (source.read_bytes(), sorted(os.listdir(source.parent))) == before
    run_path = config.store_dir / "runs" / view.run_id
    assert runstore.read_build_record(run_path) == record
    assert not (run_path / runstore.WORK_DIR).exists()
    assert [e["fmu_sha256"] for e in _built_events(config, view.run_id)] == [record.fmu.sha256]
    assert "pythonfmu" not in sys.modules
    wait_until(lambda: _gone(run_path), 10, "the worker's exit")

    # The built FMU runs, pinned by the same hash.
    run = runs.start_run(config, supervisor, view.fmu, wait_s=60)
    assert run.state is RunState.FINISHED, run
    spec = runstore.read_spec(config.store_dir / "runs" / run.run_id)
    assert spec.fmu.sha256 == record.fmu.sha256
    assert run.summary is not None
    assert {v.name: v.final for v in run.summary.variables} == {"y": 2.0}
    with pytest.raises(UserError, match="fmu_build job"):
        runs.get_results(config, view.run_id)


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        (
            SYNTAX_ERROR,
            ["models/afmi_bad.py:7:18: error: SyntaxError: '(' was never closed"],
        ),
        (
            NAME_ERROR,
            [
                "models/afmi_bad.py:6: error: NameError: name 'undefined_limit' is not defined",
                "models/afmi_bad.py:5: warning: UserWarning: gain is experimental",
            ],
        ),
        (
            INIT_ERROR,
            ["models/afmi_bad.py:5: error: ZeroDivisionError: float division by zero"],
        ),
    ],
    ids=["syntax", "name", "init"],
)
def test_a_failed_build_raises_its_diagnostics_and_writes_nothing(
    config: Config,
    supervisor: Supervisor,
    write_source: WriteSource,
    text: str,
    lines: list[str],
) -> None:
    write_source("models/afmi_bad.py", text)
    with pytest.raises(UserError) as caught:
        runs.start_build(config, supervisor, "models/afmi_bad.py")
    message = str(caught.value)
    run_id = runstore.list_run_ids(config.store_dir)[-1]
    count = "1 error" + (", 1 warning" if len(lines) > 1 else "")
    assert message.splitlines() == [
        f"Build failed (pythonfmu): {count}.",
        *lines,
        f"Full log: .agentic-fmi/runs/{run_id}/build.log",
        "Fix the source and call fmu_build again.",
    ]
    for internal in ("/work/", "/tmp", "site-packages", "importlib", "<frozen", "\x1b"):
        assert internal not in message
    assert len(message) <= config.limits.build_diagnostics_max_chars
    assert not config.generated_dir.exists()
    assert _built_events(config, run_id) == []
    view = runs.get_run(config, supervisor, run_id)
    assert view.state is RunState.FAILED and view.kind == "build"
    assert view.error is not None and view.error.code is ErrorCode.BUILD_FAILED
    assert view.error.message == message and view.error.log_tail is None
    assert "fmu_build again" in (view.hint or "")


def test_overwrite_replaces_only_an_earlier_build_and_reports_its_hash(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source("models/afmi_gain.py", k=2.0)
    first = runs.start_build(config, supervisor, "models/afmi_gain.py", name="Gain")
    assert first.state is RunState.FINISHED and first.build is not None
    write_source("models/afmi_gain.py", k=3.0)
    with pytest.raises(UserError, match="overwrite=true"):
        runs.start_build(config, supervisor, "models/afmi_gain.py", name="Gain")
    second = runs.start_build(
        config, supervisor, "models/afmi_gain.py", name="Gain", overwrite=True
    )
    assert second.state is RunState.FINISHED and second.build is not None
    assert second.build["replaced_sha256"] == first.build["sha256"]
    assert second.build["sha256"] != first.build["sha256"]
    output = config.generated_dir / "Gain"
    assert sorted(os.listdir(output)) == ["Gain.fmu", "build.json"]
    assert _sha256(output / "Gain.fmu") == second.build["sha256"]


def test_a_long_build_returns_its_job_then_status_list_and_stop_serve_it(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    write_source("models/afmi_slow.py", SLOW_SLAVE)
    view = runs.start_build(config, supervisor, "models/afmi_slow.py", wait_s=0)
    assert view.state in (RunState.QUEUED, RunState.RUNNING) and view.kind == "build"
    assert "run_stop" in (view.hint or "")
    run_path = config.store_dir / "runs" / view.run_id
    tool = str(run_path / runstore.WORK_DIR / "src" / "afmi_slow.py")
    wait_until(lambda: processes_naming(Path(tool)), 30, "the pythonfmu process")

    status = runs.get_run(config, supervisor, view.run_id)
    assert status.state is RunState.RUNNING and status.progress == 0.0
    assert status.t is None and status.t_stop is None and status.fmi_type is None
    (item,) = runs.list_runs(config, supervisor).items
    assert (item.kind, item.fmu_name, item.state) == ("build", "afmi_slow", RunState.RUNNING)
    with pytest.raises(UserError, match="already queued or running"):
        runs.start_build(config, supervisor, "models/afmi_slow.py")

    stopped = runs.stop_run(config, supervisor, view.run_id)
    assert stopped.state is RunState.STOPPED, stopped
    assert "nothing was written" in (stopped.hint or "")
    wait_until(lambda: _gone(run_path), 10, "the end of the worker and pythonfmu")
    assert not config.generated_dir.exists()
    assert not (run_path / runstore.WORK_DIR).exists()
    assert _built_events(config, view.run_id) == []


def test_a_build_past_its_time_limit_is_killed_with_its_tool(
    config: Config, supervisor: Supervisor, write_source: WriteSource
) -> None:
    quick = replace(config, timing=replace(config.timing, build_timeout_s=2.0))  # in the spec
    write_source("models/afmi_slow.py", SLOW_SLAVE)
    view = runs.start_build(quick, supervisor, "models/afmi_slow.py", wait_s=0)
    run_path = config.store_dir / "runs" / view.run_id

    def ended() -> runs.RunView | None:
        status = runs.get_run(config, supervisor, view.run_id)
        return None if status.state in (RunState.QUEUED, RunState.RUNNING) else status

    status = wait_until(ended, 30, "the timeout")
    assert status.state is RunState.FAILED
    assert status.error is not None and status.error.code is ErrorCode.TIMEOUT
    assert "time limit of 2 s" in status.error.message
    wait_until(lambda: _gone(run_path), 10, "the end of the worker and pythonfmu")
    assert not config.generated_dir.exists()
    assert not (run_path / runstore.WORK_DIR).exists()
    assert _built_events(config, view.run_id) == []


def test_two_threads_cannot_both_start_a_build_of_one_name(
    config: Config,
    supervisor: Supervisor,
    write_source: WriteSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_source("models/afmi_slow.py", SLOW_SLAVE)
    gate = threading.Barrier(2, timeout=30)
    validate = build.check_request

    def validated_together(*args: Any, **kwargs: Any) -> build.BuildRequest:
        request = validate(*args, **kwargs)
        gate.wait()  # both threads have passed validation before either one claims an ID
        return request

    monkeypatch.setattr(build, "check_request", validated_together)
    outcomes: list[runs.RunView | UserError] = []

    def start() -> None:
        try:
            outcomes.append(runs.start_build(config, supervisor, "models/afmi_slow.py", wait_s=0))
        except UserError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    started = [o for o in outcomes if isinstance(o, runs.RunView)]
    refused = [o for o in outcomes if isinstance(o, UserError)]
    assert len(started) == 1 and len(refused) == 1, outcomes
    assert "A build of 'afmi_slow' is already queued or running" in str(refused[0])
    assert [item.fmu_name for item in runs.list_runs(config, supervisor).items] == ["afmi_slow"]
    assert sorted(p.name for p in (config.store_dir / "runs").iterdir()) == [started[0].run_id]
    runs.stop_run(config, supervisor, started[0].run_id)
    run_path = config.store_dir / "runs" / started[0].run_id
    wait_until(lambda: _gone(run_path), 10, "the end of the worker and pythonfmu")


def _build_in_process(config: Config, write_source: WriteSource, overwrite: bool = False) -> Path:
    """Run a gain build's worker side (``execute_build``) in this process and return the
    run directory. The tool is still a subprocess; the test can break the commit's writes."""
    write_source("models/afmi_gain.py")
    request = build.check_request(config, "models/afmi_gain.py", overwrite=overwrite)
    run_id, run_path = runstore.claim_run_id(config.store_dir)
    runstore.write_build_source(run_path, request.source.name, request.data)
    spec = build.build_spec(config, request, run_id)
    outcome = build.execute_build(
        spec,
        run_path,
        config.limits,
        config.timing,
        should_stop=lambda: False,
        before_commit=lambda: True,
    )
    assert outcome.record is not None, outcome
    return run_path


def _disk_full(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize(
    ("owner", "attribute"), [(build, "_copy_hashed"), (runstore, "write_build_record")]
)
def test_a_failed_first_write_leaves_the_name_free(
    config: Config,
    write_source: WriteSource,
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    attribute: str,
) -> None:
    monkeypatch.setattr(owner, attribute, _disk_full)
    with pytest.raises(OSError, match="No space left"):
        _build_in_process(config, write_source)
    # The directory this commit created is gone, so it can't block the name as "not made by
    # fmu_build".
    assert not (config.generated_dir / "afmi_gain").exists()
    monkeypatch.undo()
    run_path = _build_in_process(config, write_source)
    assert sorted(os.listdir(config.generated_dir / "afmi_gain")) == ["afmi_gain.fmu", "build.json"]
    assert runstore.read_build_record(run_path) is not None


def test_a_failed_overwrite_leaves_the_earlier_build_alone(
    config: Config, write_source: WriteSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    earlier = plant_build(config, "afmi_gain")
    record = runstore.read_build_record(earlier)
    monkeypatch.setattr(build, "_copy_hashed", _disk_full)
    with pytest.raises(OSError, match="No space left"):
        _build_in_process(config, write_source, overwrite=True)
    assert sorted(os.listdir(earlier)) == ["afmi_gain.fmu", "build.json"]
    assert (earlier / "afmi_gain.fmu").read_bytes() == b"old"
    assert runstore.read_build_record(earlier) == record
