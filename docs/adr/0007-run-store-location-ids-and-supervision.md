# 0007. Run-store location, run IDs and worker supervision

- Status: Accepted
- Date: 2026-10-09

## Context

ADR 0002 and ADR 0003 fixed isolated workers and a file-based store, but left four things open:
- **Location.** The default `<workspace>/.agentic-fmi/` lands on the Windows bind mount when the workspace is the repo, where each poll costs about 8 ms (spike (b)).
- **IDs.** Two MCP servers can share one store (two Claude Code sessions in one project), and sync tools run on threads, so run IDs need a race-free allocator.
- **Supervision.** Pids get reused, a zombie passes `os.kill(pid, 0)`, and a SIGSTOPped or hung worker is alive but useless. Only a process kill stops a hung native step (spike (a)).
- **Ownership.** `status.json` must have one writer at a time, including while a worker starts.

Options considered: a counter file under a lock or random IDs (rejected: short sequential IDs read well in chat, and `mkdir` is already atomic); a supervisor daemon (rejected: one more process to manage); pidfd (rejected: doesn't survive a server restart).

## Decision

**Location.** The store is `<workspace>/.agentic-fmi/`, unless `AFMI_STORE_DIR` is set (a relative value is resolved against the workspace). `config.py` is the only place that reads it. Demos run in a workspace under `/lab` (a container volume), so the default lands on a Linux filesystem. The server passes the workspace explicitly (`afmi mcp --workspace ${CLAUDE_PROJECT_DIR}`).

**Run IDs** match `^r[1-9]\d*$`. A server claims one by `os.mkdir(runs/r<n>)`, starting one above the highest existing number and incrementing on `FileExistsError`. The kind prefixes `s` (session) and `st` (study) are reserved for later phases. `index.json` is a cache that MCP servers rewrite under an exclusive `flock` on `<store>/.lock`. It can be rebuilt from `runs/`.

**Spawn.** `[sys.executable, "-m", "agentic_fmi", "worker", <run_dir>]` with `start_new_session=True` (the worker's pid is its process-group id), stdin from `/dev/null`, and stdout and stderr appended to `log.txt`. The server writes `spec.json`, then `queued`, then spawns. The worker writes `running` with its `pid` and `pid_start_time` (field 22 of `/proc/<pid>/stat`). That write is a compare-and-set: under the store lock (`<store>/.lock`, the lock of the server's terminal writes) the worker re-reads `status.json` and writes `running` only if the run is still `queued`. A worker that finds any other state, at start or at this write, exits 0 without writing, extracting or simulating. The lock covers only that read-check-write, never the simulation.

**Worker self-supervision.** A heartbeat thread rewrites `status.json` every 0.5 s, independent of the simulation time `t`. ctypes releases the GIL during FMU calls, so it keeps beating through a blocked `doStep` (verified on Roberts CS). The same thread is the watchdog. It counts non-paused wall time only. Past `timeout_s` it writes `failed{timeout}`, removes `fmu/`, and SIGKILLs its own process group. FMPy's `timeout` is never passed.

**Server takeover.** Reconciliation runs at server startup and on every status or list call:
- A `running` run whose pid is gone, a zombie, or has a different start time, or whose heartbeat is older than 5 s, is taken over. The server SIGKILLs the process group (only if the start time matches). Its own child counts as gone only once reaped through its `Popen` handle (a zombie leader can't be reaped while its threads are still exiting); a process it didn't spawn counts as gone once `/proc/<pid>` is gone or a zombie. Only then does it write `failed{worker_lost}` with the log tail.
- A run that is still `queued` 15 s after creation becomes `failed{worker_lost}` ("worker never started"). So does a `queued` run whose worker this server spawned and which has already exited, at once.
- The server signals only a live, non-zombie process with the recorded start time, and never itself or its own process group.
- Every server-side terminal write is a compare-and-set under the store lock (`<store>/.lock`): it re-reads `status.json` and writes only if the run is still in the state the decision was based on (a stop accepts any active state once the worker is confirmed dead). The worker's own last outcome, a worker that has just started, and a second server's takeover are never overwritten, and two servers record a lost worker once. `fmu/` is removed only after that write succeeds or once the worker is confirmed dead. A takeover that loses the write returns the newer status, and the next call re-evaluates it.

**Stop.** `run_stop` sends SIGTERM to the process group. The worker's handler makes the step gate return `False`, and the worker writes `stopped` with the partial result. After a 2 s grace, the server kills, reaps and writes `stopped` itself, keeping any result the worker wrote. `run_stop` reconciles first, so a worker that had already died gives `failed{worker_lost}`. A `queued` run with no known worker process is stopped after the grace.

All timings live in `config.Timing`.

## Consequences

- (+) IDs are unique across threads and servers without a lock. They are short, sortable and readable.
- (+) A crash, hang, SIGSTOP, timeout or pid reuse each ends in a terminal state written by exactly one party, after the worker is provably gone.
- (+) The store can move off the bind mount without code changes.
- (-) Linux-only (`/proc`, `fcntl`, process groups), which matches the container. A Windows-native server would need a different supervisor.
- (-) Deleting old runs (a later cleanup tool) could let max+1 reuse an ID. The cleanup tool must keep a tombstone or a high-water mark.
- (-) A worker that is still starting after 15 s is declared lost, and a second server can end a `queued` run it didn't spawn without a process to kill. The compare-and-set start makes such a worker exit without running, so the terminal state stands, but the run is lost rather than late.
- (-) Server and worker clocks are compared for heartbeat staleness, which is only safe on one machine.
