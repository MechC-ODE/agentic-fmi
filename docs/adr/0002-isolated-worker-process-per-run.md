# 0002. Isolated worker process per run

**Status:** Accepted · **Date:** 2026-10-09 · **Detailed by** [0007](0007-run-store-location-ids-and-supervision.md)

## Problem

FMUs are third-party native code: they can segfault, hang, call `exit()` or print to stdout, and one stray print corrupts the MCP connection. FMPy runs FMUs in the calling process, and its `timeout` counts paused time and fires only between steps. In the spikes it cut a paused run off at t = 0.5 s and missed a solve that hung for over 400 s.

## Decision

Every run gets its own worker process, `afmi worker`. The server never loads an FMU binary.

- The worker's stdout and stderr go to `log.txt`.
- A worker thread writes a heartbeat every 0.5 s and enforces the timeout on non-paused time by killing its own process group. FMPy's `timeout` is never used.
- The server kills a worker that is gone or silent, confirms it is dead, then marks the run `failed` ([0007](0007-run-store-location-ids-and-supervision.md)).
- Server and workers share only the run store ([0003](0003-file-based-run-store-protocol.md)).

![A crash, hang or stray print stays inside the worker process. The worker writes status and results to the run store; the server reads them, and spawns, supervises and can kill the worker's process group.](../diagrams/adr-0002-isolation.svg)

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **A worker process per run** | Chosen | A crash stays contained; a kill stops any hang |
| FMUs inside the server | Rejected | A segfault or print breaks Claude's tools |
| FMPy's `timeout` | Rejected | Truncates paused runs, misses hangs |

## Trade-offs

**Gains**
- An FMU can't take down or corrupt the server.
- Parallel runs are just more workers, capped by `max_workers`.
- Runs outlive the tool call that started them.

**Costs, and how we handle them**
- A few hundred milliseconds to start each run, and results travel through files.
- Orphans: the server reconciles stale runs at startup, and the container's `init` reaps zombies.
- All timeouts live in `config.py` and are tested with hanging and crashing workers.
