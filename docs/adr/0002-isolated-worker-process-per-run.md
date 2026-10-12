# 0002. Isolated worker process per run or session

- Status: Accepted
- Date: 2026-10-09

## Context

FMU binaries are native code from third parties. They can segfault, hang, leak, call `exit()`, keep global state or print to stdout, which would corrupt the MCP stdio channel. By default FMPy runs FMUs in the calling process. Live sessions need to pause for arbitrary periods, but FMPy's `simulate_fmu(timeout=...)` counts wall time, including the time spent paused inside the `step_finished` callback. Using it would kill paused sessions. The feasibility spikes confirmed this ([spikes.md](../spikes.md)): a paused run was silently truncated at t = 0.5 with no exception, and the timeout did not stop a hung solve, because FMPy checks it only between steps. A Roberts CS run with `timeout=5` was killed at the 60 s probe limit, and an exploratory run outside the script was still running after 400 s.

## Decision

- Every run and every live session executes in its own detached worker process, `afmi worker`. Its stdout and stderr are redirected to `runs/<id>/log.txt`.
- The MCP server never loads FMU binaries. It parses `modelDescription.xml` and reads result files only.
- Each worker writes a heartbeat to `status.json`. A dedicated worker thread writes it, separate from the simulation progress `t`, because a single `doStep` can block for minutes (Roberts CS).
- The worker runs its own watchdog, which counts only non-paused simulation time (paused time is excluded) and enforces the run timeout. We never pass FMPy's `timeout`. Timeouts are enforced by killing the worker process, the only thing that stops a hung native step.
- The server supervises liveness. If the pid is gone or the heartbeat is stale, the server first sends SIGKILL to the worker's process group, reaps the worker and confirms it is gone. Only then does it write `failed` to `status.json` (single-writer rule) and attach the tail of `log.txt`. A zombie still passes `os.kill(pid, 0)`, so a bare liveness check isn't proof of death. `run_stop` sends SIGTERM, then SIGKILL after a grace period. Live runs can also be ended with a `stop` command through the command inbox (see [ADR 0007](0007-run-store-location-ids-and-supervision.md)).
- Workers and the server communicate only through the run store ([ADR 0003](0003-file-based-run-store-protocol.md)).

## Consequences

- (+) A crash, hang or stray print in an FMU can't take down or corrupt the MCP server.
- (+) Parallel sweeps are simply N workers, bounded by `max_workers`.
- (+) Runs outlive the call that started them, so long runs are polled.
- (-) Each run pays a process start and import cost (roughly 100s of ms). Results flow through files, not memory.
- (-) Orphans need handling: the server reconciles stale runs at startup. The container's `init: true` reaps zombies.
- (-) Timeout and heartbeat values must be configured in one place (`config.py`) and tested with a deliberately hanging and a crashing worker.
