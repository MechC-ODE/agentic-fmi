# 0007. Run-store location, run IDs and worker supervision

**Status:** Accepted · **Date:** 2026-10-09

## Problem

[0002](0002-isolated-worker-process-per-run.md) and [0003](0003-file-based-run-store-protocol.md) left four questions open:

| Question | Why it's hard |
|---|---|
| Where does the store live? | On the Windows bind mount each poll costs about 8 ms |
| How are run IDs allocated? | Two servers can share one store, and tools run on threads |
| When is a worker dead? | Pids get reused, zombies look alive, a hung worker is alive but useless |
| Who writes `status.json`? | Exactly one party at a time, even while a worker starts |

## Decision

- **Location:** `<workspace>/.agentic-fmi/`, or `AFMI_STORE_DIR`; demos run in `/lab`, a Linux volume.
- **Run IDs:** `r1`, `r2`, ... A server claims one by creating its folder with `os.mkdir`, which is atomic, and tries the next number if it exists.
- **Start:** the worker switches `queued` to `running` only by compare-and-set under the store lock, and exits without running if the run moved on.
- **Supervision:** on every status or list call, and at startup, the server checks active runs and takes over dead ones (diagram). It writes `failed` only after the worker is confirmed gone.
- **Stop:** SIGTERM, so the worker can save its partial result; SIGKILL after a 2 s grace period.

![Takeover: on a status or list call, or at server start, a run queued for more than 15 s is marked failed: worker_lost. For a running or paused run the server checks that the pid exists, isn't a zombie, has the same start time and a heartbeat under 5 s old. If not, it kills the process group, confirms it is gone, and writes failed: worker_lost with a compare-and-set under the store lock.](../diagrams/adr-0007-takeover.svg)

| Timing (`config.Timing`) | Value |
|---|---|
| Heartbeat period | 0.5 s |
| Heartbeat counts as stale after | 5 s |
| A `queued` run counts as never started after | 15 s |
| Grace between SIGTERM and SIGKILL on stop | 2 s |

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **IDs claimed with `mkdir`** | Chosen | Atomic without a lock; short, readable IDs |
| Counter file under a lock, or random IDs | Rejected | More machinery, or IDs that read badly in chat |
| **Supervision by the server, on every call** | Chosen | No extra process |
| A supervisor daemon | Rejected | One more process to manage |
| Linux pidfds | Rejected | Don't survive a server restart |

## Trade-offs

**Gains**
- Unique, sortable IDs across threads and servers.
- Every failure (crash, hang, timeout, reused pid) ends in exactly one terminal state, written after the worker is provably gone.

**Costs, and how we handle them**
- Linux-only (`/proc`, `fcntl`, process groups), like the container.
- Deleting runs could reuse an ID; a cleanup tool must keep a high-water mark.
- A worker still starting after 15 s is declared lost, and exits without running.
- Staleness compares two clocks; [0010](0010-live-runs-and-the-pane.md) adds a monotonic heartbeat.

<details>
<summary><b>Details:</b> spawning, the takeover rules and stop, precisely</summary>

- **Location.** Only `config.py` reads `AFMI_STORE_DIR` (a relative value resolves against the workspace). The server gets the workspace explicitly: `afmi mcp --workspace ${CLAUDE_PROJECT_DIR}`.
- **IDs and index.** IDs match `^r[1-9]\d*$`, starting one above the highest existing number. `index.json` is a cache that servers rewrite under an exclusive `flock` on `<store>/.lock`; it can be rebuilt from `runs/`.
- **Spawn.** The server writes `spec.json`, then `queued`, then runs `[sys.executable, "-m", "agentic_fmi", "worker", <run_dir>]` with `start_new_session=True` (the worker's pid is its process-group id), stdin from `/dev/null`, and stdout and stderr appended to `log.txt`. The worker records `pid` and `pid_start_time` (field 22 of `/proc/<pid>/stat`). The store lock covers only the read, check and write of `status.json`, never the simulation.
- **Watchdog.** The heartbeat thread keeps beating through a blocked `doStep` (ctypes releases the GIL during FMU calls). Past `timeout_s` of non-paused time it writes `failed: timeout`, removes `fmu/` and SIGKILLs its own process group.
- **When a worker counts as gone.** Its pid is gone, a zombie or has another start time, or its heartbeat is older than 5 s. The server's own child counts as gone only once reaped through its `Popen` handle; a process it didn't spawn counts as gone once `/proc/<pid>` is gone or a zombie. A `queued` run whose worker already exited fails at once.
- **Signals.** Only to a live, non-zombie process with the recorded start time, and never to the server itself or its own process group.
- **Compare-and-set.** Every terminal write by the server re-reads `status.json` under the store lock and writes only if the run is still in the state the decision was based on. So the worker's own outcome, a worker that just started and a second server's takeover are never overwritten, and two servers record a lost worker once. `fmu/` is removed only after that write succeeds or once the worker is confirmed dead.
- **Stop.** The worker's SIGTERM handler makes the step gate return `False`; the worker writes `stopped` with its partial result. After the grace period the server kills, reaps and writes `stopped` itself, keeping any result the worker wrote. `run_stop` reconciles first, so an already dead worker gives `failed: worker_lost`.

</details>
